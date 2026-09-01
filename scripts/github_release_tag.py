#!/usr/bin/env python3
"""Ensure an exact Chromium release tag points at the validated build commit."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(
    r"^chromium-[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-(?:linux|windows)-i686$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

class TagStateError(RuntimeError):
    pass


def _validate_inputs(repository: str, tag: str, sha: str, token: str) -> None:
    if not REPOSITORY_RE.fullmatch(repository): raise ValueError(f"Invalid GitHub repository: {repository!r}")
    if not TAG_RE.fullmatch(tag): raise ValueError(f"Invalid Chromium i686 release tag: {tag!r}")
    if not SHA_RE.fullmatch(sha): raise ValueError(f"Invalid build SHA: {sha!r}")
    if not token: raise TagStateError("GitHub token is required for release-tag provenance")


def _run_gh(args: list[str], token: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy(); env["GH_TOKEN"] = token
    try:
        return subprocess.run(["gh", *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise TagStateError(f"GitHub tag operation failed: {exc}") from exc


def _is_404(result: subprocess.CompletedProcess[str]) -> bool:
    detail=f"{result.stderr}\n{result.stdout}"
    return bool(re.search(r"(?:HTTP\s+404|status\s*404|Not Found\s*\(HTTP 404\))", detail, re.I))


def _json_or_error(result: subprocess.CompletedProcess[str], context: str) -> dict[str, object]:
    if result.returncode != 0:
        detail=(result.stderr or result.stdout or "gh api failed").strip()
        raise TagStateError(f"{context}: {detail}")
    try:
        payload=json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise TagStateError(f"{context}: GitHub returned invalid JSON") from exc
    if not isinstance(payload, dict): raise TagStateError(f"{context}: GitHub returned non-object JSON")
    return payload


def resolve_tag_commit(repository: str, tag: str, token: str, *, timeout: int = 60) -> str | None:
    result=_run_gh(["api",f"repos/{repository}/git/ref/tags/{tag}"],token,timeout=timeout)
    if result.returncode != 0:
        if _is_404(result): return None
        detail=(result.stderr or result.stdout or "gh api failed").strip()
        raise TagStateError(f"GitHub tag-ref lookup failed: {detail}")
    payload=_json_or_error(result,"GitHub tag-ref lookup failed")
    obj=payload.get("object") or {}
    if not isinstance(obj,dict): raise TagStateError(f"Git tag {tag} returned invalid object metadata")
    object_type=str(obj.get("type","")); object_sha=str(obj.get("sha",""))
    if not SHA_RE.fullmatch(object_sha): raise TagStateError(f"GitHub returned an invalid object SHA for tag {tag}: {object_sha!r}")
    if object_type=="commit": return object_sha
    if object_type!="tag": raise TagStateError(f"Git tag {tag} targets unsupported object type {object_type!r}")
    for _depth in range(5):
        tag_payload=_json_or_error(_run_gh(["api",f"repos/{repository}/git/tags/{object_sha}"],token,timeout=timeout),f"Could not dereference annotated tag {tag}")
        obj=tag_payload.get("object") or {}
        if not isinstance(obj,dict): raise TagStateError(f"Annotated tag {tag} returned invalid object metadata")
        object_type=str(obj.get("type","")); object_sha=str(obj.get("sha",""))
        if not SHA_RE.fullmatch(object_sha): raise TagStateError(f"GitHub returned an invalid dereferenced object SHA for tag {tag}: {object_sha!r}")
        if object_type=="commit": return object_sha
        if object_type!="tag": raise TagStateError(f"Annotated tag {tag} ultimately targets unsupported object type {object_type!r}")
    raise TagStateError(f"Annotated tag {tag} exceeded maximum dereference depth")


def _confirm_tag(repository: str, tag: str, sha: str, token: str, *, attempts: int = 5, delay: float = 2.0) -> bool:
    last_error: Exception | None=None
    for attempt in range(attempts):
        try: resolved=resolve_tag_commit(repository,tag,token)
        except TagStateError as exc: last_error=exc
        else:
            if resolved is None: last_error=TagStateError(f"Tag {tag} is still absent after create attempt")
            elif resolved!=sha: raise TagStateError(f"Git tag {tag} resolves to {resolved}, not validated build {sha}")
            else: return True
        if attempt+1<attempts: time.sleep(delay)
    if last_error: raise TagStateError(f"Could not confirm release tag {tag}: {last_error}") from last_error
    return False


def ensure_exact_tag(repository: str, tag: str, sha: str, token: str) -> str:
    _validate_inputs(repository,tag,sha,token)
    existing=resolve_tag_commit(repository,tag,token)
    if existing is not None:
        if existing!=sha: raise TagStateError(f"Git tag {tag} already resolves to {existing}, not validated build {sha}")
        return "already-exact"
    try:
        result=_run_gh(["api","--method","POST",f"repos/{repository}/git/refs","-f",f"ref=refs/tags/{tag}","-f",f"sha={sha}"],token)
    except TagStateError:
        # A transport/client timeout can happen after GitHub accepted the write.
        # Never retry the create blindly; confirm the exact server-side ref.
        _confirm_tag(repository,tag,sha,token)
        return "created-after-client-error"
    if result.returncode != 0:
        _confirm_tag(repository,tag,sha,token)
        return "created-after-client-error"
    _json_or_error(result,"Git tag create failed")
    _confirm_tag(repository,tag,sha,token)
    return "created"


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--repository",required=True); parser.add_argument("--tag",required=True); parser.add_argument("--sha",required=True); args=parser.parse_args()
    token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    print(ensure_exact_tag(args.repository,args.tag,args.sha,token)); return 0

if __name__ == "__main__": raise SystemExit(main())
