from __future__ import annotations

import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError

from scripts.check_release_revision import RevisionError, is_current, main


class ReleaseRevisionTests(unittest.TestCase):
    def response(self, sha):
        return io.BytesIO(json.dumps({"ref": "refs/heads/main", "object": {"type": "commit", "sha": sha}}).encode())

    def test_current_candidate_is_allowed(self):
        self.assertTrue(is_current("example/app", "a" * 40, "test-token", opener=lambda *a, **kw: self.response("a" * 40)))

    def test_slow_old_run_and_rerun_cannot_replace_new_main(self):
        self.assertFalse(is_current("example/app", "a" * 40, "test-token", opener=lambda *a, **kw: self.response("b" * 40)))

    def test_api_error_is_not_treated_as_stale_success_or_exposes_response_body(self):
        error = HTTPError("https://api.github.com/", 403, "Forbidden", {}, io.BytesIO(b"private-server-content"))
        with self.assertRaises(RevisionError) as caught:
            is_current("example/app", "a" * 40, "test-token", opener=mock.Mock(side_effect=error))
        self.assertNotIn("private-server-content", str(caught.exception))

    def test_malformed_reference_fails_closed(self):
        for data in ([], {}, {"ref": "refs/heads/other", "object": {"type": "commit", "sha": "a" * 40}}):
            with self.subTest(data=data), self.assertRaises(RevisionError):
                is_current("example/app", "a" * 40, "test-token", opener=lambda *a, **kw: io.BytesIO(json.dumps(data).encode()))

    def test_request_keeps_token_in_header_and_uses_exact_read_only_endpoint(self):
        captured = []
        def opener(request, **kwargs):
            captured.append(request)
            return self.response("a" * 40)
        is_current("example/app", "a" * 40, "test-token", opener=opener)
        self.assertEqual(captured[0].get_method(), "GET")
        self.assertEqual(captured[0].full_url, "https://api.github.com/repos/example/app/git/ref/heads/main")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer test-token")
        self.assertIsNone(captured[0].data)

    def test_cli_distinguishes_stale_from_verification_failure(self):
        with mock.patch("scripts.check_release_revision.is_current", return_value=False):
            self.assertEqual(main(), 3)
        with mock.patch("scripts.check_release_revision.is_current", side_effect=RevisionError("unavailable")):
            self.assertEqual(main(), 2)
