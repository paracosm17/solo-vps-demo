"""Regression coverage for the immutable deployment transaction."""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from scripts.coolify_deploy_api import (
    CoolifyApiClient,
    CoolifyDeployError,
    DeploymentRollbackError,
    APPLY_CONFIRMATION,
    apply_deployment,
    build_plan,
    normalize_base_url,
    recover_known_good,
    require_apply_confirmation,
    validate_application_state,
)

DIGEST = "4ccb1d8b60c3e42471c39870cfb24856b27280a7940b9a1316e8134a2d32e5cf"
IMAGE_REF = f"ghcr.io/example-user/hello-app@sha256:{DIGEST}"
RESOURCE_UUID = "example123resource456"


def app_state(**overrides):
    value = {
        "uuid": RESOURCE_UUID,
        "name": "hello-app-ghcr",
        "build_pack": "dockerimage",
        "ports_exposes": "8080",
        "ports_mappings": None,
        "fqdn": None,
        "docker_registry_image_name": "ghcr.io/example-user/hello-app",
        "docker_registry_image_tag": f"sha256-{DIGEST}",
        "status": "running:healthy",
    }
    value.update(overrides)
    return value


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


class CoolifyDeployApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = build_plan(
            base_url="http://127.0.0.1:8000/api/v1",
            resource_uuid=RESOURCE_UUID,
            image_ref=IMAGE_REF,
        )

    def test_plan_uses_exact_loopback_api_and_proven_digest_fields(self) -> None:
        self.assertEqual(self.plan.base_url, "http://127.0.0.1:8000/api/v1")
        self.assertEqual(
            self.plan.update_payload,
            {
                "docker_registry_image_name": "ghcr.io/example-user/hello-app",
                "docker_registry_image_tag": f"sha256-{DIGEST}",
            },
        )
        self.assertEqual(self.plan.start_url, f"http://127.0.0.1:8000/api/v1/applications/{RESOURCE_UUID}/start")
        self.assertEqual(self.plan.as_dict()["rollback_scope"], "container-image-only")
        self.assertFalse(self.plan.as_dict()["database_schema_rollback"])
        self.assertEqual(
            self.plan.as_dict()["required_token_permissions"],
            {"read_write_token": ["read", "write"], "deploy_token": ["deploy"]},
        )

    def test_direct_and_fixed_runner_tunnel_loopback_urls_are_accepted(self) -> None:
        self.assertEqual(
            normalize_base_url("http://127.0.0.1:8000/api/v1"),
            "http://127.0.0.1:8000/api/v1",
        )
        self.assertEqual(
            normalize_base_url("http://127.0.0.1:18000/api/v1"),
            "http://127.0.0.1:18000/api/v1",
        )

    def test_non_loopback_https_or_unapproved_loopback_port_is_rejected(self) -> None:
        bad_urls = (
            "https://127.0.0.1:8000/api/v1",
            "http://203.0.113.10:8000/api/v1",
            "http://localhost:8000/api/v1",
            "http://127.0.0.1:8000/api/v1/extra",
            "http://127.0.0.1:18001/api/v1",
        )
        for bad in bad_urls:
            with self.subTest(bad=bad), self.assertRaises(CoolifyDeployError):
                normalize_base_url(bad)

    def test_mutable_image_or_unsafe_uuid_is_rejected(self) -> None:
        with self.assertRaises(CoolifyDeployError):
            build_plan(
                base_url="http://127.0.0.1:8000/api/v1",
                resource_uuid=RESOURCE_UUID,
                image_ref="ghcr.io/example-user/hello-app:latest",
            )
        with self.assertRaises(CoolifyDeployError):
            build_plan(
                base_url="http://127.0.0.1:8000/api/v1",
                resource_uuid="../../etc/passwd",
                image_ref=IMAGE_REF,
            )

    def test_isolated_target_must_be_dockerimage_with_no_host_mapping_or_domain(self) -> None:
        validate_application_state(app_state(), self.plan)
        for bad in (
            app_state(build_pack="dockerfile"),
            app_state(ports_mappings="18080:8080"),
            app_state(fqdn="https://example.test"),
            app_state(ports_exposes="3000"),
        ):
            with self.subTest(bad=bad), self.assertRaises(CoolifyDeployError):
                validate_application_state(bad, self.plan)

    def test_allow_domain_requires_explicit_plan_override(self) -> None:
        plan = build_plan(
            base_url="http://127.0.0.1:8000/api/v1",
            resource_uuid=RESOURCE_UUID,
            image_ref=IMAGE_REF,
            allow_domain=True,
        )
        validate_application_state(app_state(fqdn="https://hello.example"), plan)

    def test_api_client_never_embeds_token_in_url_or_body(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = request.data
            return FakeResponse({"uuid": RESOURCE_UUID})

        client = CoolifyApiClient("123|secret-value", opener=opener)
        client.request("PATCH", self.plan.application_url, self.plan.update_payload)
        self.assertNotIn("secret-value", captured["url"])
        self.assertNotIn(b"secret-value", captured["body"])
        self.assertEqual(captured["auth"], "Bearer 123|secret-value")

    def test_apply_uses_get_patch_get_start_poll_get_sequence_and_exact_payload(self) -> None:
        calls = []
        responses = [
            app_state(),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"message": "Deployment request queued.", "deployment_uuid": "deployment123"},
            {"deployment_uuid": "deployment123", "status": "queued"},
            {"deployment_uuid": "deployment123", "status": "in_progress"},
            {"deployment_uuid": "deployment123", "status": "finished"},
            app_state(status="running:starting"),
            app_state(),
        ]

        class FakeClient:
            def request(self, method, url, payload=None):
                calls.append((method, url, payload))
                return responses.pop(0)

        clock = iter([0.0, 0.1, 0.2, 0.3, 0.4])
        evidence = apply_deployment(
            FakeClient(),
            self.plan,
            poll_interval=0,
            poll_timeout=10,
            sleeper=lambda _: None,
            monotonic=lambda: next(clock),
        )
        self.assertEqual(evidence["deployment_status"], "finished")
        self.assertEqual(evidence["image_ref"], IMAGE_REF)
        self.assertEqual(evidence["previous_image_ref"], IMAGE_REF)
        self.assertFalse(evidence["rollback_attempted"])
        self.assertEqual(evidence["rollback_scope"], "container-image-only")
        self.assertFalse(evidence["database_schema_rollback"])
        self.assertEqual([call[0] for call in calls], ["GET", "PATCH", "GET", "POST", "GET", "GET", "GET", "GET", "GET"])
        self.assertEqual(calls[1][2], self.plan.update_payload)
        self.assertIsNone(calls[3][2])

    def test_apply_uses_deploy_only_client_only_for_start(self) -> None:
        read_write_calls = []
        deploy_calls = []
        read_write_responses = [
            app_state(),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"deployment_uuid": "deployment123", "status": "finished"},
            app_state(),
        ]
        deploy_responses = [{"deployment_uuid": "deployment123"}]

        class ReadWriteClient:
            def request(self, method, url, payload=None):
                read_write_calls.append((method, url, payload))
                return read_write_responses.pop(0)

        class DeployClient:
            def request(self, method, url, payload=None):
                deploy_calls.append((method, url, payload))
                return deploy_responses.pop(0)

        evidence = apply_deployment(
            ReadWriteClient(),
            self.plan,
            deploy_client=DeployClient(),
            poll_interval=0,
            poll_timeout=10,
            sleeper=lambda _: None,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(evidence["deployment_status"], "finished")
        self.assertEqual([call[0] for call in read_write_calls], ["GET", "PATCH", "GET", "GET", "GET"])
        self.assertEqual([call[0] for call in deploy_calls], ["POST"])
        self.assertEqual(deploy_calls[0][1], self.plan.start_url)

    def test_apply_fails_closed_on_bad_persisted_digest(self) -> None:
        previous_digest = "6" * 64
        responses = [
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"uuid": RESOURCE_UUID},
            app_state(docker_registry_image_tag="latest"),
            {"uuid": RESOURCE_UUID},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"deployment_uuid": "rollback123"},
            {"deployment_uuid": "rollback123", "status": "finished"},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
        ]

        class FakeClient:
            def request(self, method, url, payload=None):
                return responses.pop(0)

        with self.assertRaises(DeploymentRollbackError) as caught:
            apply_deployment(FakeClient(), self.plan, sleeper=lambda _: None)
        self.assertEqual(caught.exception.outcome, "DEPLOY_FAILED_ROLLBACK_OK")
        self.assertIn("did not persist the expected immutable", caught.exception.deployment_error)

    def test_apply_failed_deployment_restores_previous_digest_and_health(self) -> None:
        previous_digest = "1" * 64
        responses = [
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"deployment_uuid": "deployment123"},
            {"deployment_uuid": "deployment123", "status": "failed"},
            {"uuid": RESOURCE_UUID},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"deployment_uuid": "rollback123"},
            {"deployment_uuid": "rollback123", "status": "finished"},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
        ]
        calls = []

        class FakeClient:
            def request(self, method, url, payload=None):
                calls.append((method, url, payload))
                return responses.pop(0)

        with self.assertRaises(DeploymentRollbackError) as caught:
            apply_deployment(FakeClient(), self.plan, sleeper=lambda _: None)
        self.assertEqual(caught.exception.outcome, "DEPLOY_FAILED_ROLLBACK_OK")
        self.assertEqual(caught.exception.candidate_image_ref, IMAGE_REF)
        self.assertEqual(
            caught.exception.previous_image_ref,
            f"ghcr.io/example-user/hello-app@sha256:{previous_digest}",
        )
        self.assertEqual(
            calls[5][2],
            {
                "docker_registry_image_name": "ghcr.io/example-user/hello-app",
                "docker_registry_image_tag": f"sha256-{previous_digest}",
            },
        )
        self.assertEqual([call[0] for call in calls], ["GET", "PATCH", "GET", "POST", "GET", "PATCH", "GET", "POST", "GET", "GET"])

    def test_apply_unhealthy_after_finished_rolls_back(self) -> None:
        previous_digest = "2" * 64
        responses = [
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"deployment_uuid": "deployment123"},
            {"deployment_uuid": "deployment123", "status": "finished"},
            app_state(status="running:unhealthy"),
            {"uuid": RESOURCE_UUID},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"deployment_uuid": "rollback123"},
            {"deployment_uuid": "rollback123", "status": "finished"},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
        ]

        class FakeClient:
            def request(self, method, url, payload=None):
                return responses.pop(0)

        with self.assertRaises(DeploymentRollbackError) as caught:
            apply_deployment(FakeClient(), self.plan, poll_interval=0, poll_timeout=10, sleeper=lambda _: None)
        self.assertEqual(caught.exception.outcome, "DEPLOY_FAILED_ROLLBACK_OK")
        self.assertIn("did not become healthy", caught.exception.deployment_error)

    def test_apply_timeout_does_not_start_overlapping_rollback(self) -> None:
        previous_digest = "3" * 64
        responses = [
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"deployment_uuid": "deployment123"},
            {"deployment_uuid": "deployment123", "status": "in_progress"},
        ]
        calls = []

        class FakeClient:
            def request(self, method, url, payload=None):
                calls.append(method)
                return responses.pop(0)

        clock = iter([0.0, 2.0])
        with self.assertRaises(DeploymentRollbackError) as caught:
            apply_deployment(
                FakeClient(), self.plan, poll_interval=0, poll_timeout=1,
                sleeper=lambda _: None, monotonic=lambda: next(clock),
            )
        self.assertEqual(caught.exception.outcome, "DEPLOY_STATE_UNKNOWN")
        self.assertIn("timed out waiting for candidate deployment", caught.exception.deployment_error)
        self.assertEqual(calls.count("POST"), 1)
        self.assertEqual(calls.count("PATCH"), 1)
        self.assertIn(f"sha256:{previous_digest}", caught.exception.recovery_command)

    def test_unhealthy_previous_application_is_rejected_before_mutation(self) -> None:
        client = mock.Mock()
        client.request.return_value = app_state(status="running:unhealthy")
        with self.assertRaises(CoolifyDeployError):
            apply_deployment(client, self.plan)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args[0], "GET")

    def test_rollback_failure_has_distinct_outcome_and_exact_known_good_recovery_command(self) -> None:
        previous_digest = "4" * 64
        responses = [
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"deployment_uuid": "deployment123"},
            {"deployment_uuid": "deployment123", "status": "failed"},
            {"uuid": RESOURCE_UUID},
            app_state(docker_registry_image_tag=f"sha256-{previous_digest}"),
            {"deployment_uuid": "rollback123"},
            {"deployment_uuid": "rollback123", "status": "failed"},
        ]

        class FakeClient:
            def request(self, method, url, payload=None):
                return responses.pop(0)

        with self.assertRaises(DeploymentRollbackError) as caught:
            apply_deployment(FakeClient(), self.plan, sleeper=lambda _: None)
        error = caught.exception
        self.assertEqual(error.outcome, "DEPLOY_FAILED_ROLLBACK_FAILED")
        self.assertIn("rollback deployment rollback123 ended with status=failed", error.rollback_error)
        self.assertIn("--recover-known-good", error.recovery_command)
        self.assertIn("http://127.0.0.1:8000/api/v1", error.recovery_command)
        self.assertIn(f"sha256:{previous_digest}", error.recovery_command)
        self.assertNotIn("COOLIFY_API_TOKEN=", error.recovery_command)
        self.assertEqual(error.as_dict()["rollback_scope"], "container-image-only")

    def test_apply_refuses_mutation_without_immutable_previous_digest(self) -> None:
        responses = [app_state(docker_registry_image_tag="latest")]
        calls = []

        class FakeClient:
            def request(self, method, url, payload=None):
                calls.append((method, url, payload))
                return responses.pop(0)

        with self.assertRaisesRegex(CoolifyDeployError, "current desired image must use Coolify sha256"):
            apply_deployment(FakeClient(), self.plan, sleeper=lambda _: None)
        self.assertEqual([call[0] for call in calls], ["GET"])

    def test_known_good_recovery_does_not_nest_rollback(self) -> None:
        responses = [
            app_state(docker_registry_image_tag=f"sha256-{'5' * 64}"),
            {"uuid": RESOURCE_UUID},
            app_state(),
            {"deployment_uuid": "recovery123"},
            {"deployment_uuid": "recovery123", "status": "finished"},
            app_state(),
        ]

        class FakeClient:
            def request(self, method, url, payload=None):
                return responses.pop(0)

        evidence = recover_known_good(FakeClient(), self.plan, poll_interval=0, sleeper=lambda _: None)
        self.assertEqual(evidence["image_ref"], IMAGE_REF)
        self.assertEqual(evidence["recovery_mode"], "known-good-no-nested-rollback")
        self.assertEqual(evidence["rollback_scope"], "container-image-only")


    def test_apply_requires_explicit_environment_confirmation(self) -> None:
        with mock.patch.dict("scripts.coolify_deploy_api.os.environ", {}, clear=True):
            with self.assertRaisesRegex(CoolifyDeployError, "COOLIFY_API_DEPLOY_CONFIRM"):
                require_apply_confirmation()
        with mock.patch.dict(
            "scripts.coolify_deploy_api.os.environ",
            {"COOLIFY_API_DEPLOY_CONFIRM": APPLY_CONFIRMATION},
            clear=True,
        ):
            require_apply_confirmation()

    def test_cli_plan_does_not_require_token_or_contact_network(self) -> None:
        from scripts import coolify_deploy_api

        argv = [
            "coolify_deploy_api.py",
            "--resource-uuid",
            RESOURCE_UUID,
            "--image-ref",
            IMAGE_REF,
        ]
        with mock.patch.object(coolify_deploy_api.sys, "argv", argv), mock.patch.dict(
            coolify_deploy_api.os.environ, {}, clear=True
        ), mock.patch("scripts.coolify_deploy_api.urlopen", side_effect=AssertionError("network must not be used")):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                self.assertEqual(coolify_deploy_api.main(), 0)
            self.assertIn("restores the previous immutable desired digest", out.getvalue())
            self.assertIn("mutation: disabled unless --apply or --recover-known-good is supplied", out.getvalue())


if __name__ == "__main__":
    unittest.main()
