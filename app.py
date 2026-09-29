#!/usr/bin/env python3
"""Minimal dependency-free HTTP application used by Solo VPS integration tests."""

from __future__ import annotations

import json
import os
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

APP_HOST: Final = "0.0.0.0"
APP_PORT: Final = 8080
APP_VERSION: Final = "1"


class HelloHandler(BaseHTTPRequestHandler):
    server_version = "solo-vps-hello"
    sys_version = ""

    def _write_json(self, status: HTTPStatus, payload: dict[str, str]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # BaseHTTPRequestHandler API
        if self.path == "/":
            self._write_json(
                HTTPStatus.OK,
                {
                    "service": "solo-vps-hello",
                    "status": "ok",
                    "version": APP_VERSION,
                    "message": os.environ.get("APP_MESSAGE", "Hello from Solo VPS"),
                },
            )
            return
        if self.path == "/healthz":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

    def do_HEAD(self) -> None:  # BaseHTTPRequestHandler API
        self.do_GET()

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep the standard request log shape without exposing headers or request bodies.
        super().log_message(fmt, *args)


def create_server(host: str = APP_HOST, port: int = APP_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), HelloHandler)


def main() -> None:
    server = create_server()

    # shutdown() must run outside the serve_forever thread.
    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    print(f"solo-vps-hello listening on http://{APP_HOST}:{APP_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
