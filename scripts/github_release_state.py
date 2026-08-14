#!/usr/bin/env python3
"""Read exact GitHub release-tag state without conflating 404 with API failure."""
from __future__ import annotations

import argparse
import os
import re
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
API_ROOT = "https://api.github.com"


class ReleaseStateError(RuntimeError):
    """Raised when release existence cannot be established safely."""


def release_exists(repository: str, tag: str, token: str, *, timeout: int = 60) -> bool:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError(f"Invalid GitHub repository: {repository!r}")
    if not tag or "\n" in tag or "\r" in tag:
        raise ValueError("Release tag must be a non-empty single-line value")
    if not token:
        raise ReleaseStateError("GitHub token is required for release-state lookup")
    encoded_tag = urllib.parse.quote(tag, safe="")
    url = f"{API_ROOT}/repos/{repository}/releases/tags/{encoded_tag}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "chromium-i686-control-plane/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            if status != 200:
                raise ReleaseStateError(f"Unexpected GitHub release lookup status: {status}")
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise ReleaseStateError(f"GitHub release lookup failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReleaseStateError(f"GitHub release lookup failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    print("exists" if release_exists(args.repository, args.tag, token) else "missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
