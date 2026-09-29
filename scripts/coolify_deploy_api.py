#!/usr/bin/env python3
"""Safely drive one immutable Docker Image deployment through loopback Coolify API.

The script is intentionally constrained to the Solo VPS management contract:
- Coolify API must remain loopback-only; direct target access is 127.0.0.1:8000;
- the only alternate client endpoint is the fixed runner-side SSH tunnel at 127.0.0.1:18000;
- the target must already be a Docker Image application;
- the image identity must be an immutable public GHCR sha256 digest;
- mutation requires an existing immutable desired digest so failed/unhealthy candidates can be restored automatically;
- mutation requires an explicit --apply flag, confirmation environment value, and bearer token supplied out of band.

This is an M11 control-plane proof helper, not a general Coolify client.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from scripts.validate_coolify_image_handoff import CoolifyImageHandoff, parse_image_ref
except ModuleNotFoundError:  # Direct execution as scripts/coolify_deploy_api.py
    from validate_coolify_image_handoff import CoolifyImageHandoff, parse_image_ref

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
SSH_TUNNEL_BASE_URL = "http://127.0.0.1:18000/api/v1"
APPLY_CONFIRMATION = "I_HAVE_REVIEWED_THE_LOOPBACK_COOLIFY_API_DEPLOYMENT"
_ALLOWED_BASE_URLS = {DEFAULT_BASE_URL, SSH_TUNNEL_BASE_URL}
_RESOURCE_UUID_RE = re.compile(r"^[a-z0-9]{8,64}$")
_COOLIFY_DIGEST_TAG_RE = re.compile(r"^sha256-([0-9a-f]{64})$")
_TERMINAL_DEPLOYMENT_STATUSES = {"finished", "failed", "cancelled-by-user"}
ROLLBACK_SCOPE = "container-image-only"


def rollback_boundary_evidence() -> dict[str, Any]:
    return {
        "rollback_scope": ROLLBACK_SCOPE,
        "database_schema_rollback": False,
        "application_data_rollback": False,
        "external_side_effect_rollback": False,
    }


class CoolifyDeployError(RuntimeError):
    """Fail-closed error for an unsafe or unsuccessful deployment operation."""


class DeploymentOutcomeUnknown(CoolifyDeployError):
    """The server may still be deploying; a second deployment must not be started."""


class DeploymentNotStarted(CoolifyDeployError):
    """The desired-state PATCH was rejected before a mutation could be confirmed."""

    outcome = "DEPLOY_NOT_STARTED"
    mutation_started = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ERROR",
            "outcome": self.outcome,
            "mutation_started": self.mutation_started,
            "error": str(self),
        }


@dataclass(frozen=True)
class ImmutableImageState:
    image_name: str
    image_tag: str

    @property
    def image_ref(self) -> str:
        match = _COOLIFY_DIGEST_TAG_RE.fullmatch(self.image_tag)
        if match is None:
            raise CoolifyDeployError("Coolify image state is not an immutable sha256 digest")
        return f"{self.image_name}@sha256:{match.group(1)}"

    @property
    def update_payload(self) -> dict[str, str]:
        return {
            "docker_registry_image_name": self.image_name,
            "docker_registry_image_tag": self.image_tag,
        }


class DeploymentRollbackError(CoolifyDeployError):
    """Deployment failed after mutation, with explicit recovery/outcome evidence."""

    def __init__(
        self,
        *,
        outcome: str,
        candidate_image_ref: str,
        previous_image_ref: str,
        deployment_error: str,
        rollback_error: str = "",
        recovery_command: str = "",
    ) -> None:
        self.outcome = outcome
        self.candidate_image_ref = candidate_image_ref
        self.previous_image_ref = previous_image_ref
        self.deployment_error = deployment_error
        self.rollback_error = rollback_error
        self.recovery_command = recovery_command
        message = (
            f"{outcome}: candidate={candidate_image_ref}; previous={previous_image_ref}; "
            f"deploy_error={deployment_error}"
        )
        if rollback_error:
            message += f"; rollback_error={rollback_error}"
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": "ERROR",
            "outcome": self.outcome,
            "candidate_image_ref": self.candidate_image_ref,
            "previous_image_ref": self.previous_image_ref,
            "deployment_error": self.deployment_error,
            **rollback_boundary_evidence(),
        }
        if self.rollback_error:
            value["rollback_error"] = self.rollback_error
        if self.recovery_command:
            value["recovery_command"] = self.recovery_command
        return value


@dataclass(frozen=True)
class DeploymentPlan:
    base_url: str
    resource_uuid: str
    handoff: CoolifyImageHandoff
    expected_port: str
    allow_domain: bool

    @property
    def application_url(self) -> str:
        return f"{self.base_url}/applications/{quote(self.resource_uuid, safe='')}"

    @property
    def start_url(self) -> str:
        return f"{self.application_url}/start"

    @property
    def update_payload(self) -> dict[str, str]:
        # The supported Coolify 4.3.21 lifecycle keeps Docker Image desired state
        # as repository plus the sha256-* tag/hash field used by the API.
        return {
            "docker_registry_image_name": self.handoff.preferred_ui_image_name,
            "docker_registry_image_tag": self.handoff.preferred_ui_image_tag,
        }

    def deployment_url(self, deployment_uuid: str) -> str:
        return f"{self.base_url}/deployments/{quote(deployment_uuid, safe='')}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "resource_uuid": self.resource_uuid,
            "image_ref": self.handoff.image_ref,
            "expected_port": self.expected_port,
            "allow_domain": self.allow_domain,
            "application_url": self.application_url,
            "start_url": self.start_url,
            "update_payload": self.update_payload,
            "required_token_permissions": {
                "read_write_token": ["read", "write"],
                "deploy_token": ["deploy"],
            },
            **rollback_boundary_evidence(),
        }


def normalize_base_url(base_url: str) -> str:
    if base_url != base_url.strip():
        raise CoolifyDeployError("Coolify API base URL must not contain surrounding whitespace")
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise CoolifyDeployError(
            "Coolify API proof must stay on loopback HTTP; never expose or call the management API externally"
        )
    if parsed.path != "/api/v1" or parsed.params or parsed.query or parsed.fragment:
        raise CoolifyDeployError("Coolify API base URL must end exactly at /api/v1")
    if normalized not in _ALLOWED_BASE_URLS:
        raise CoolifyDeployError(
            "Coolify API base URL must be exactly the direct 127.0.0.1:8000 endpoint "
            "or the fixed runner-side 127.0.0.1:18000 SSH-tunnel endpoint"
        )
    return normalized


def build_plan(
    *,
    base_url: str,
    resource_uuid: str,
    image_ref: str,
    expected_port: str = "8080",
    allow_domain: bool = False,
) -> DeploymentPlan:
    if not _RESOURCE_UUID_RE.fullmatch(resource_uuid):
        raise CoolifyDeployError("resource UUID must contain only lowercase letters/digits and be 8-64 characters")
    if not expected_port.isdigit() or not 1 <= int(expected_port) <= 65535:
        raise CoolifyDeployError("expected port must be an integer from 1 to 65535")
    try:
        handoff = parse_image_ref(image_ref)
    except ValueError as exc:
        raise CoolifyDeployError(str(exc)) from exc
    return DeploymentPlan(
        base_url=normalize_base_url(base_url),
        resource_uuid=resource_uuid,
        handoff=handoff,
        expected_port=expected_port,
        allow_domain=allow_domain,
    )


def _clean_optional(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def validate_application_state(application: dict[str, Any], plan: DeploymentPlan) -> None:
    if _clean_optional(application.get("uuid")) != plan.resource_uuid:
        raise CoolifyDeployError("Coolify API returned a different application UUID")
    if _clean_optional(application.get("build_pack")) != "dockerimage":
        raise CoolifyDeployError("target application must already use build_pack=dockerimage")

    ports = [part.strip() for part in _clean_optional(application.get("ports_exposes")).split(",") if part.strip()]
    if plan.expected_port not in ports:
        raise CoolifyDeployError(f"target application must expose internal port {plan.expected_port}")

    if _clean_optional(application.get("ports_mappings")):
        raise CoolifyDeployError("target application must not have host port mappings")

    if not plan.allow_domain and _clean_optional(application.get("fqdn")):
        raise CoolifyDeployError("isolated API proof requires an application with no public domain")

    current_name = _clean_optional(application.get("docker_registry_image_name"))
    if current_name and current_name != plan.handoff.preferred_ui_image_name:
        raise CoolifyDeployError(
            "target Docker Image repository differs from the requested immutable artifact repository"
        )


def candidate_image_state(plan: DeploymentPlan) -> ImmutableImageState:
    return ImmutableImageState(
        image_name=plan.handoff.preferred_ui_image_name,
        image_tag=plan.handoff.preferred_ui_image_tag,
    )


def capture_previous_image_state(application: dict[str, Any], plan: DeploymentPlan) -> ImmutableImageState:
    validate_application_state(application, plan)
    if _clean_optional(application.get("status")) != "running:healthy":
        raise CoolifyDeployError("automatic deployment requires a healthy previous application; inspect it before starting another release")
    image_name = _clean_optional(application.get("docker_registry_image_name"))
    image_tag = _clean_optional(application.get("docker_registry_image_tag"))
    if not image_name or not image_tag:
        raise CoolifyDeployError(
            "target application must already have a known-good immutable Docker Image before automatic rollback is safe"
        )
    if _COOLIFY_DIGEST_TAG_RE.fullmatch(image_tag) is None:
        raise CoolifyDeployError(
            "target application's current desired image must use Coolify sha256-<digest> form before mutation; "
            "mutable previous state cannot be used as an automatic rollback target"
        )
    return ImmutableImageState(image_name=image_name, image_tag=image_tag)


def validate_persisted_image_state(
    application: dict[str, Any],
    plan: DeploymentPlan,
    expected: ImmutableImageState | None = None,
) -> None:
    validate_application_state(application, plan)
    state = expected or candidate_image_state(plan)
    expected_name = state.image_name
    expected_tag = state.image_tag
    if _clean_optional(application.get("docker_registry_image_name")) != expected_name:
        raise CoolifyDeployError("Coolify did not persist the expected Docker Image repository")
    if _clean_optional(application.get("docker_registry_image_tag")) != expected_tag:
        raise CoolifyDeployError("Coolify did not persist the expected immutable sha256 tag/hash field")


class CoolifyApiClient:
    def __init__(self, token: str, *, timeout: float = 10.0, opener: Callable[..., Any] = urlopen):
        if not token or token != token.strip():
            raise CoolifyDeployError("Coolify API token is empty or contains surrounding whitespace")
        self._token = token
        self._timeout = timeout
        self._opener = opener

    def request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            # Validation/error bodies can contain application settings or echoed
            # credentials. Inspect detailed errors through the authenticated UI.
            raise CoolifyDeployError(f"Coolify API {method} failed with HTTP {exc.code}; inspect Coolify logs") from exc
        except URLError as exc:
            raise CoolifyDeployError(f"Coolify API {method} failed: {exc.reason}") from exc
        except OSError as exc:
            raise CoolifyDeployError(f"Coolify API {method} failed: {exc}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoolifyDeployError(f"Coolify API {method} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise CoolifyDeployError(f"Coolify API {method} returned a non-object response")
        return parsed


def require_apply_confirmation() -> None:
    confirmation = os.environ.get("COOLIFY_API_DEPLOY_CONFIRM", "")
    if confirmation != APPLY_CONFIRMATION:
        raise CoolifyDeployError(
            "--apply requires COOLIFY_API_DEPLOY_CONFIRM=" + APPLY_CONFIRMATION
        )


def _load_token(name: str) -> str:
    token = os.environ.get(name, "")
    if not token:
        raise CoolifyDeployError(
            f"{name} is required; supply it through the environment and do not put it on the command line"
        )
    return token


def load_read_write_token_from_environment() -> str:
    return _load_token("COOLIFY_API_TOKEN_RW")


def load_deploy_token_from_environment() -> str:
    return _load_token("COOLIFY_API_TOKEN_DEPLOY")


def check_application(client: CoolifyApiClient, plan: DeploymentPlan) -> dict[str, Any]:
    application = client.request("GET", plan.application_url)
    validate_application_state(application, plan)
    return application


def _deploy_exact_image(
    client: CoolifyApiClient,
    plan: DeploymentPlan,
    desired: ImmutableImageState,
    *,
    deploy_client: CoolifyApiClient | None = None,
    operation: str,
    poll_interval: float,
    poll_timeout: float,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    deploy_client = deploy_client or client
    try:
        update_response = client.request("PATCH", plan.application_url, desired.update_payload)
    except CoolifyDeployError as exc:
        raise DeploymentNotStarted(
            f"Coolify {operation} desired-state PATCH was rejected before mutation could be confirmed: {exc}"
        ) from exc
    if _clean_optional(update_response.get("uuid")) != plan.resource_uuid:
        raise CoolifyDeployError(f"Coolify {operation} update response did not confirm the target application UUID")

    persisted = client.request("GET", plan.application_url)
    validate_persisted_image_state(persisted, plan, desired)

    try:
        start_response = deploy_client.request("POST", plan.start_url)
    except CoolifyDeployError as exc:
        raise DeploymentOutcomeUnknown(f"{operation} start outcome is unknown; inspect Coolify before recovery") from exc
    deployment_uuid = _clean_optional(start_response.get("deployment_uuid"))
    if not deployment_uuid or not _RESOURCE_UUID_RE.fullmatch(deployment_uuid):
        raise DeploymentOutcomeUnknown(f"Coolify {operation} start response did not include a valid deployment_uuid")

    deadline = monotonic() + poll_timeout
    deployment: dict[str, Any] = {}
    while True:
        try:
            deployment = client.request("GET", plan.deployment_url(deployment_uuid))
        except CoolifyDeployError as exc:
            raise DeploymentOutcomeUnknown(f"cannot determine {operation} deployment {deployment_uuid} status; inspect it before recovery") from exc
        status = _clean_optional(deployment.get("status"))
        if status in _TERMINAL_DEPLOYMENT_STATUSES:
            break
        if status not in {"queued", "in_progress"}:
            raise DeploymentOutcomeUnknown(f"unexpected Coolify {operation} deployment status: {status or '<empty>'}")
        if monotonic() >= deadline:
            raise DeploymentOutcomeUnknown(
                f"timed out waiting for {operation} deployment {deployment_uuid}; last status={status}"
            )
        sleeper(poll_interval)

    if _clean_optional(deployment.get("status")) != "finished":
        raise CoolifyDeployError(
            f"Coolify {operation} deployment {deployment_uuid} ended with "
            f"status={_clean_optional(deployment.get('status'))}"
        )

    health_deadline = monotonic() + poll_timeout
    app_status = ""
    while True:
        after = client.request("GET", plan.application_url)
        validate_persisted_image_state(after, plan, desired)
        app_status = _clean_optional(after.get("status"))
        if app_status == "running:healthy":
            break
        if app_status.startswith(("exited", "stopped", "dead", "failed")) or app_status == "running:unhealthy":
            raise CoolifyDeployError(
                f"application did not become healthy after {operation} deployment: status={app_status}"
            )
        if monotonic() >= health_deadline:
            raise CoolifyDeployError(
                f"timed out waiting for application health after {operation} deployment {deployment_uuid}; "
                f"last status={app_status or '<empty>'}"
            )
        sleeper(poll_interval)

    return {
        "deployment_uuid": deployment_uuid,
        "deployment_status": "finished",
        "application_status": app_status or "not-reported",
    }


def _recovery_command(
    plan: DeploymentPlan,
    previous: ImmutableImageState,
    *,
    poll_interval: float,
    poll_timeout: float,
) -> str:
    # Incident recovery is intentionally rendered for execution *on the VPS* so it
    # does not depend on a GitHub runner's transient 127.0.0.1:18000 SSH tunnel.
    args = [
        "python3",
        "scripts/coolify_deploy_api.py",
        "--base-url",
        DEFAULT_BASE_URL,
        "--resource-uuid",
        plan.resource_uuid,
        "--image-ref",
        previous.image_ref,
        "--expected-port",
        plan.expected_port,
        "--recover-known-good",
        "--poll-timeout",
        str(poll_timeout),
        "--poll-interval",
        str(poll_interval),
    ]
    if plan.allow_domain:
        args.append("--allow-domain")
    command = " ".join(shlex.quote(part) for part in args)
    return f"COOLIFY_API_DEPLOY_CONFIRM={APPLY_CONFIRMATION} {command}"


def apply_deployment(
    client: CoolifyApiClient,
    plan: DeploymentPlan,
    *,
    deploy_client: CoolifyApiClient | None = None,
    poll_interval: float = 2.0,
    poll_timeout: float = 180.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    before = check_application(client, plan)
    previous = capture_previous_image_state(before, plan)
    candidate = candidate_image_state(plan)

    try:
        candidate_result = _deploy_exact_image(
            client,
            plan,
            candidate,
            operation="candidate",
            deploy_client=deploy_client,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            sleeper=sleeper,
            monotonic=monotonic,
        )
    except DeploymentNotStarted:
        # The candidate PATCH itself was rejected. No candidate desired-state
        # mutation was confirmed, so attempting a rollback would be both
        # misleading and unnecessary.
        raise
    except DeploymentOutcomeUnknown as deployment_error:
        raise DeploymentRollbackError(
            outcome="DEPLOY_STATE_UNKNOWN",
            candidate_image_ref=candidate.image_ref,
            previous_image_ref=previous.image_ref,
            deployment_error=str(deployment_error),
            recovery_command=_recovery_command(plan, previous, poll_interval=poll_interval, poll_timeout=poll_timeout),
        ) from deployment_error
    except CoolifyDeployError as deployment_error:
        try:
            rollback_result = _deploy_exact_image(
                client,
                plan,
                previous,
                operation="rollback",
                deploy_client=deploy_client,
                poll_interval=poll_interval,
                poll_timeout=poll_timeout,
                sleeper=sleeper,
                monotonic=monotonic,
            )
        except CoolifyDeployError as rollback_error:
            raise DeploymentRollbackError(
                outcome="DEPLOY_FAILED_ROLLBACK_FAILED",
                candidate_image_ref=candidate.image_ref,
                previous_image_ref=previous.image_ref,
                deployment_error=str(deployment_error),
                rollback_error=str(rollback_error),
                recovery_command=_recovery_command(
                    plan,
                    previous,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                ),
            ) from rollback_error

        raise DeploymentRollbackError(
            outcome="DEPLOY_FAILED_ROLLBACK_OK",
            candidate_image_ref=candidate.image_ref,
            previous_image_ref=previous.image_ref,
            deployment_error=str(deployment_error),
        ) from deployment_error

    return {
        "resource_uuid": plan.resource_uuid,
        "deployment_uuid": candidate_result["deployment_uuid"],
        "image_ref": candidate.image_ref,
        "deployment_status": candidate_result["deployment_status"],
        "application_status": candidate_result["application_status"],
        "previous_image_ref": previous.image_ref,
        "previous_image_name": previous.image_name,
        "previous_image_tag": previous.image_tag,
        "rollback_attempted": False,
        **rollback_boundary_evidence(),
    }


def recover_known_good(
    client: CoolifyApiClient,
    plan: DeploymentPlan,
    *,
    deploy_client: CoolifyApiClient | None = None,
    poll_interval: float = 2.0,
    poll_timeout: float = 180.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    # This is an explicit incident path for the exact immutable image printed by
    # DEPLOY_FAILED_ROLLBACK_FAILED. It intentionally does not roll back again to
    # the currently persisted (possibly bad) desired state if recovery itself fails.
    check_application(client, plan)
    desired = candidate_image_state(plan)
    result = _deploy_exact_image(
        client,
        plan,
        desired,
        operation="known-good recovery",
        deploy_client=deploy_client,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    return {
        "resource_uuid": plan.resource_uuid,
        "deployment_uuid": result["deployment_uuid"],
        "image_ref": desired.image_ref,
        "deployment_status": result["deployment_status"],
        "application_status": result["application_status"],
        "recovery_mode": "known-good-no-nested-rollback",
        **rollback_boundary_evidence(),
    }


def _print_plan(plan: DeploymentPlan, as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
        return
    print("PASS Coolify loopback deploy API plan")
    print(f"  base_url: {plan.base_url}")
    print(f"  resource_uuid: {plan.resource_uuid}")
    print(f"  image_ref: {plan.handoff.image_ref}")
    print(f"  expected_internal_port: {plan.expected_port}")
    print(f"  public_domain_allowed: {'yes' if plan.allow_domain else 'no'}")
    print("  required_token_permissions: read+write token; separate deploy-only token")
    print("  update_payload:")
    print(json.dumps(plan.update_payload, indent=4, sort_keys=True))
    print("  rollback: --apply requires and restores the previous immutable desired digest on failure")
    print("  rollback_scope: container-image-only")
    print("  database_schema_rollback: false")
    print("  application_data_rollback: false")
    print("  external_side_effect_rollback: false")
    print("  mutation: disabled unless --apply or --recover-known-good is supplied")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan/check/apply an immutable GHCR deployment through the loopback-only Coolify 4.3.21 API."
    )
    parser.add_argument("--resource-uuid", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--expected-port", default="8080")
    parser.add_argument(
        "--allow-domain",
        action="store_true",
        help="Permit a target with an fqdn; default fails closed for the isolated M11 proof resource.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read and validate the target application without mutation.")
    mode.add_argument(
        "--apply",
        action="store_true",
        help="PATCH exact digest, deploy, and automatically restore the previous immutable digest on failure.",
    )
    mode.add_argument(
        "--recover-known-good",
        action="store_true",
        help="Incident-only: deploy the supplied immutable image without nested rollback to current desired state.",
    )
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        if args.poll_timeout <= 0 or args.poll_interval < 0:
            raise CoolifyDeployError("poll timeout must be > 0 and poll interval must be >= 0")
        plan = build_plan(
            base_url=args.base_url,
            resource_uuid=args.resource_uuid,
            image_ref=args.image_ref,
            expected_port=args.expected_port,
            allow_domain=args.allow_domain,
        )
        if not args.check and not args.apply and not args.recover_known_good:
            _print_plan(plan, args.json)
            return 0

        if args.apply or args.recover_known_good:
            require_apply_confirmation()
        read_write_token = load_read_write_token_from_environment()
        client = CoolifyApiClient(read_write_token)
        deploy_client = None
        if args.apply or args.recover_known_good:
            deploy_client = CoolifyApiClient(load_deploy_token_from_environment())
        if args.check:
            application = check_application(client, plan)
            evidence = {
                "status": "PASS",
                "mode": "check",
                "resource_uuid": plan.resource_uuid,
                "build_pack": _clean_optional(application.get("build_pack")),
                "ports_exposes": _clean_optional(application.get("ports_exposes")),
                "ports_mappings": _clean_optional(application.get("ports_mappings")),
                "fqdn": _clean_optional(application.get("fqdn")),
                "docker_registry_image_name": _clean_optional(application.get("docker_registry_image_name")),
                "docker_registry_image_tag": _clean_optional(application.get("docker_registry_image_tag")),
                "application_status": _clean_optional(application.get("status")),
                **rollback_boundary_evidence(),
            }
        elif args.apply:
            evidence = {"status": "PASS", "mode": "apply"}
            evidence.update(
                apply_deployment(
                    client,
                    plan,
                    deploy_client=deploy_client,
                    poll_interval=args.poll_interval,
                    poll_timeout=args.poll_timeout,
                )
            )
        else:
            evidence = {"status": "PASS", "mode": "recover-known-good"}
            evidence.update(
                recover_known_good(
                    client,
                    plan,
                    deploy_client=deploy_client,
                    poll_interval=args.poll_interval,
                    poll_timeout=args.poll_timeout,
                )
            )

        if args.json:
            print(json.dumps(evidence, indent=2, sort_keys=True))
        else:
            if args.check:
                label = "check"
            elif args.recover_known_good:
                label = "known-good recovery"
            else:
                label = "deployment"
            print(f"PASS Coolify loopback deploy API {label}")
            for key, value in evidence.items():
                if key not in {"status", "mode"}:
                    print(f"  {key}: {value}")
        return 0
    except DeploymentNotStarted as exc:
        if args.json:
            print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
        else:
            print("ERROR Coolify loopback deploy API did not start", file=sys.stderr)
            for key, value in exc.as_dict().items():
                if key != "status":
                    print(f"  {key}: {value}", file=sys.stderr)
        return 2
    except DeploymentRollbackError as exc:
        if args.json:
            print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
        else:
            print("ERROR Coolify loopback deploy API transaction", file=sys.stderr)
            for key, value in exc.as_dict().items():
                if key != "status":
                    print(f"  {key}: {value}", file=sys.stderr)
            if exc.outcome == "DEPLOY_FAILED_ROLLBACK_FAILED":
                print(
                    "  recovery_note: run recovery_command on the VPS only after exporting a reviewed short-lived "
                    "COOLIFY_API_TOKEN_RW and COOLIFY_API_TOKEN_DEPLOY; do not put tokens on the command line.",
                    file=sys.stderr,
                )
        return 3 if exc.outcome == "DEPLOY_FAILED_ROLLBACK_FAILED" else 2
    except CoolifyDeployError as exc:
        print(f"ERROR Coolify loopback deploy API: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
