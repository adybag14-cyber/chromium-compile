#!/usr/bin/env python3
"""Read exact GitHub release-tag state without conflating 404 with API failure."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(r"^chromium-[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-linux-i686$")

class ReleaseStateError(RuntimeError):
    pass


def _run_gh(args: list[str], token: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy(); env["GH_TOKEN"] = token
    try:
        return subprocess.run(["gh", *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ReleaseStateError(f"GitHub release lookup failed: {exc}") from exc


def _is_404(result: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{result.stderr}\n{result.stdout}"
    return bool(re.search(r"(?:HTTP\s+404|status\s*404|Not Found\s*\(HTTP 404\))", detail, re.I))


def release_exists(repository: str, tag: str, token: str, *, timeout: int = 60) -> bool:
    if not REPOSITORY_RE.fullmatch(repository): raise ValueError(f"Invalid GitHub repository: {repository!r}")
    if not TAG_RE.fullmatch(tag): raise ValueError(f"Invalid Chromium i686 release tag: {tag!r}")
    if not token: raise ReleaseStateError("GitHub token is required for release-state lookup")
    result = _run_gh(["api", f"repos/{repository}/releases/tags/{tag}"], token, timeout=timeout)
    if result.returncode == 0:
        try: json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc: raise ReleaseStateError("GitHub release lookup returned invalid JSON") from exc
        return True
    if _is_404(result): return False
    detail = (result.stderr or result.stdout or "gh api failed").strip()
    raise ReleaseStateError(f"GitHub release lookup failed: {detail}")


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--repository",required=True); parser.add_argument("--tag",required=True); args=parser.parse_args()
    token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    print("exists" if release_exists(args.repository,args.tag,token) else "missing"); return 0

if __name__ == "__main__": raise SystemExit(main())
