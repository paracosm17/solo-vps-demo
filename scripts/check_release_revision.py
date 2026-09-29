#!/usr/bin/env python3
"""Inside the deployment lock, reject workflows superseded by another main commit."""
from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RevisionError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RevisionError("GitHub API redirected the request; check the repository identity")


def is_current(repository: str, commit: str, token: str, *, opener=None) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RevisionError("GITHUB_REPOSITORY must be owner/repository")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RevisionError("GITHUB_SHA must be a full commit SHA")
    if not token or token != token.strip():
        raise RevisionError("GITHUB_TOKEN is required to verify the current main revision")
    request = Request(
        f"https://api.github.com/repos/{repository}/git/ref/heads/main",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    opener = opener or build_opener(NoRedirect()).open
    try:
        with opener(request, timeout=15) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        raise RevisionError(f"cannot verify main revision: GitHub API HTTP {exc.code}") from exc
    except (OSError, URLError, ValueError) as exc:
        raise RevisionError("cannot verify main revision; deployment was not started") from exc
    if not isinstance(result, dict) or result.get("ref") != "refs/heads/main":
        raise RevisionError("GitHub API did not return the exact main reference")
    obj = result.get("object")
    if not isinstance(obj, dict) or obj.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", str(obj.get("sha", ""))):
        raise RevisionError("GitHub API did not return a valid main commit")
    return obj["sha"] == commit


def main() -> int:
    try:
        current = is_current(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_SHA", ""),
            os.environ.get("GITHUB_TOKEN", ""),
        )
    except RevisionError as exc:
        print(f"ERROR release revision: {exc}", file=sys.stderr)
        return 2
    if not current:
        print("SKIPPED_STALE_COMMIT: main has changed; this workflow will not deploy")
        return 3
    print("PASS release revision: candidate is the current main commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
