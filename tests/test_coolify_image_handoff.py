from __future__ import annotations

import unittest

from scripts.validate_coolify_image_handoff import (
    parse_image_ref,
    reconstruct_v412_dockerimage_ref,
)


GOOD_DIGEST = "4ccb1d8b60c3e42471c39870cfb24856b27280a7940b9a1316e8134a2d32e5cf"
GOOD_REF = f"ghcr.io/example-user/hello-app@sha256:{GOOD_DIGEST}"


class CoolifyImageHandoffContractTests(unittest.TestCase):
    def test_current_owner_digest_is_accepted_and_preferred_ui_fields_are_safe(self) -> None:
        handoff = parse_image_ref(GOOD_REF)
        self.assertEqual(handoff.image_ref, GOOD_REF)
        self.assertEqual(handoff.repository, "ghcr.io/example-user/hello-app")
        self.assertEqual(handoff.digest, GOOD_DIGEST)
        self.assertEqual(handoff.preferred_ui_image_name, "ghcr.io/example-user/hello-app")
        self.assertEqual(handoff.preferred_ui_image_tag, f"sha256-{GOOD_DIGEST}")
        self.assertEqual(handoff.preferred_ui_effective_ref, GOOD_REF)

    def test_api_representation_reconstructs_the_same_digest(self) -> None:
        handoff = parse_image_ref(GOOD_REF)
        self.assertEqual(handoff.api_image_name, "ghcr.io/example-user/hello-app@sha256")
        self.assertEqual(handoff.api_image_tag, GOOD_DIGEST)
        self.assertEqual(handoff.api_effective_ref, GOOD_REF)

    def test_v412_creation_ui_autoparse_reproduces_double_sha256_bug(self) -> None:
        handoff = parse_image_ref(GOOD_REF)
        self.assertEqual(
            handoff.v412_creation_ui_bug_effective_ref,
            f"ghcr.io/example-user/hello-app@sha256@sha256:{GOOD_DIGEST}",
        )

    def test_v412_reconstruction_uses_sha256_dash_tag_as_digest_signal(self) -> None:
        self.assertEqual(
            reconstruct_v412_dockerimage_ref(
                "ghcr.io/example-user/hello-app", f"sha256-{GOOD_DIGEST}"
            ),
            GOOD_REF,
        )

    def test_latest_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_ref("ghcr.io/example-user/hello-app:latest")

    def test_full_sha_tag_is_not_a_deployment_identity(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_ref("ghcr.io/example-user/hello-app:sha-0123456789abcdef0123456789abcdef01234567")

    def test_short_digest_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_ref("ghcr.io/example-user/hello-app@sha256:deadbeef")

    def test_uppercase_digest_is_rejected_as_noncanonical(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_ref(f"ghcr.io/example-user/hello-app@sha256:{GOOD_DIGEST.upper()}")

    def test_non_ghcr_registry_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_ref(f"docker.io/example-user/hello-app@sha256:{GOOD_DIGEST}")

    def test_whitespace_or_shell_suffix_is_rejected(self) -> None:
        for bad in (
            f" {GOOD_REF}",
            f"{GOOD_REF} ",
            f"{GOOD_REF};id",
            f"{GOOD_REF}\n",
        ):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                parse_image_ref(bad)


if __name__ == "__main__":
    unittest.main()
