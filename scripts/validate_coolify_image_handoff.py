#!/usr/bin/env python3
"""Validate an immutable GHCR image reference before a Coolify Docker Image handoff.

This helper is intentionally offline. It never contacts GitHub, GHCR, Coolify, or a
server and never reads credentials. It validates the narrow M11 artifact identity
contract and models the pinned Coolify v4.1.2 Docker Image deployment reconstruction.

Pinned v4.1.2 has a UI creation bug for pasted @sha256 references: the creation form
stores both an ``@sha256`` suffix in docker_registry_image_name and a ``sha256-``
prefix in docker_registry_image_tag. The deployment job then adds ``@sha256:`` again,
producing an invalid ``...@sha256@sha256:<digest>`` reference. The safe manual UI
configuration is the plain repository name plus ``sha256-<digest>`` in the tag/hash
field. The API creation path uses a different, also-valid representation: repository
``@sha256`` plus the raw 64-hex digest.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

_SEGMENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_IMAGE_REF_RE = re.compile(
    rf"\A(?P<repository>ghcr\.io/{_SEGMENT}(?:/{_SEGMENT})+)@sha256:(?P<digest>[0-9a-f]{{64}})\Z"
)
_SHA256_TAG_PREFIX = "sha256-"


def reconstruct_v412_dockerimage_ref(image_name: str, image_tag: str) -> str:
    """Model ApplicationDeploymentJob::generate_image_names() for dockerimage."""
    if image_tag.startswith(_SHA256_TAG_PREFIX):
        digest = image_tag.removeprefix(_SHA256_TAG_PREFIX)
        return f"{image_name}@sha256:{digest}"
    return f"{image_name}:{image_tag or 'latest'}"


@dataclass(frozen=True)
class CoolifyImageHandoff:
    image_ref: str
    repository: str
    digest: str
    preferred_ui_image_name: str
    preferred_ui_image_tag: str
    api_image_name: str
    api_image_tag: str
    v412_creation_ui_bug_image_name: str
    v412_creation_ui_bug_image_tag: str

    @property
    def preferred_ui_effective_ref(self) -> str:
        return reconstruct_v412_dockerimage_ref(
            self.preferred_ui_image_name, self.preferred_ui_image_tag
        )

    @property
    def api_effective_ref(self) -> str:
        return reconstruct_v412_dockerimage_ref(self.api_image_name, self.api_image_tag)

    @property
    def v412_creation_ui_bug_effective_ref(self) -> str:
        return reconstruct_v412_dockerimage_ref(
            self.v412_creation_ui_bug_image_name, self.v412_creation_ui_bug_image_tag
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "image_ref": self.image_ref,
            "repository": self.repository,
            "digest": f"sha256:{self.digest}",
            "preferred_ui_image_name": self.preferred_ui_image_name,
            "preferred_ui_image_tag": self.preferred_ui_image_tag,
            "preferred_ui_effective_ref": self.preferred_ui_effective_ref,
            "api_image_name": self.api_image_name,
            "api_image_tag": self.api_image_tag,
            "api_effective_ref": self.api_effective_ref,
            "v412_creation_ui_bug_image_name": self.v412_creation_ui_bug_image_name,
            "v412_creation_ui_bug_image_tag": self.v412_creation_ui_bug_image_tag,
            "v412_creation_ui_bug_effective_ref": self.v412_creation_ui_bug_effective_ref,
        }


def parse_image_ref(image_ref: str) -> CoolifyImageHandoff:
    if image_ref != image_ref.strip():
        raise ValueError("image reference must not contain leading or trailing whitespace")

    match = _IMAGE_REF_RE.fullmatch(image_ref)
    if match is None:
        raise ValueError(
            "expected canonical immutable GHCR reference: "
            "ghcr.io/<owner>/<image>@sha256:<64 lowercase hex>"
        )

    repository = match.group("repository")
    digest = match.group("digest")
    handoff = CoolifyImageHandoff(
        image_ref=image_ref,
        repository=repository,
        digest=digest,
        # Preferred manual configuration in the General form. This matches the
        # deployment job's sha256-* branch without duplicating the @sha256 marker.
        preferred_ui_image_name=repository,
        preferred_ui_image_tag=f"sha256-{digest}",
        # The v4.1.2 create-Docker-Image API normalizes to these two fields. Its
        # deployment path reconstructs repository@sha256:<digest> via ':' joining.
        api_image_name=f"{repository}@sha256",
        api_image_tag=digest,
        # The v4.1.2 creation UI auto-parser currently stores this broken pair.
        v412_creation_ui_bug_image_name=f"{repository}@sha256",
        v412_creation_ui_bug_image_tag=f"sha256-{digest}",
    )

    if handoff.preferred_ui_effective_ref != image_ref:
        raise AssertionError("preferred UI field reconstruction drifted from immutable image_ref")
    if handoff.api_effective_ref != image_ref:
        raise AssertionError("API field reconstruction drifted from immutable image_ref")
    if "@sha256@sha256:" not in handoff.v412_creation_ui_bug_effective_ref:
        raise AssertionError("v4.1.2 creation-UI bug model no longer reproduces the double marker")

    return handoff


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an immutable GHCR image reference for the M11 Coolify handoff."
    )
    parser.add_argument("image_ref", help="Exact ghcr.io/...@sha256:<64hex> image reference")
    parser.add_argument("--json", action="store_true", help="Print normalized evidence as JSON")
    args = parser.parse_args()

    try:
        handoff = parse_image_ref(args.image_ref)
    except ValueError as exc:
        print(f"ERROR Coolify image handoff contract: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(handoff.as_dict(), indent=2, sort_keys=True))
    else:
        print("PASS Coolify image handoff: immutable GHCR digest reference")
        print(f"  image_ref: {handoff.image_ref}")
        print("  preferred_v4.1.2_ui_fields:")
        print(f"    Docker Image: {handoff.preferred_ui_image_name}")
        print(f"    Docker Image Tag or Hash: {handoff.preferred_ui_image_tag}")
        print(f"    effective_ref: {handoff.preferred_ui_effective_ref}")
        print("  v4.1.2_creation_ui_known_bad_autoparse:")
        print(f"    Docker Image: {handoff.v412_creation_ui_bug_image_name}")
        print(f"    Docker Image Tag or Hash: {handoff.v412_creation_ui_bug_image_tag}")
        print(f"    invalid_effective_ref: {handoff.v412_creation_ui_bug_effective_ref}")
        print("  action: after creating the Docker Image resource, correct the General fields")
        print("          to the preferred values above before the first deploy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
