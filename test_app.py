from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from app import APP_VERSION, create_server


class HelloAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()

    def test_root_contract(self) -> None:
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        payload = json.loads(body)
        self.assertEqual(payload["service"], "solo-vps-hello")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], APP_VERSION)
        self.assertIsInstance(payload["message"], str)

    def test_public_runtime_env_is_applied_without_exposing_other_variables(
        self,
    ) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "APP_MESSAGE": "Configured in Coolify",
                "PRIVATE_TEST_TOKEN": "never-return-this",
            },
        ):
            status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["message"], "Configured in Coolify")
        self.assertNotIn(b"never-return-this", body)

    def test_health_contract(self) -> None:
        status, _, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_head_returns_no_body(self) -> None:
        status, _, body = self.request("/healthz", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_unknown_path_is_not_found(self) -> None:
        status, _, body = self.request("/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"status": "not_found"})


if __name__ == "__main__":
    unittest.main()
