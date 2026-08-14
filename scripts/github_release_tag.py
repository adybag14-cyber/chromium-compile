#!/usr/bin/env python3
"""Ensure an exact Chromium release tag points at the validated build commit."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(r"^chromium-[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-linux-i686$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TagStateError(RuntimeError):
    """Raised when exact release-tag provenance cannot be established safely."""


def _request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    body: dict[str, str] | None = None,
    timeout: int = 60,
):
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "chromium-i686-release-provenance/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=timeout)


def _validate_inputs(repository: str, tag: str, sha: str, token: str) -> None:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError(f"Invalid GitHub repository: {repository!r}")
    if not TAG_RE.fullmatch(tag):
        raise ValueError(f"Invalid Chromium i686 release tag: {tag!r}")
    if not SHA_RE.fullmatch(sha):
        raise ValueError(f"Invalid build SHA: {sha!r}")
    if not token:
        raise TagStateError("GitHub token is required for release-tag provenance")


def resolve_tag_commit(
    repository: str, tag: str, token: str, *, timeout: int = 60
) -> str | None:
    encoded = urllib.parse.quote(tag, safe="")
    url = f"{API_ROOT}/repos/{repository}/git/ref/tags/{encoded}"
    try:
        with _request(url, token, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise TagStateError(f"GitHub tag-ref lookup failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise TagStateError(f"GitHub tag-ref lookup failed: {exc}") from exc

    obj = payload.get("object") or {}
    object_type = str(obj.get("type", ""))
    object_sha = str(obj.get("sha", ""))
    if not SHA_RE.fullmatch(object_sha):
        raise TagStateError(f"GitHub returned an invalid object SHA for tag {tag}: {object_sha!r}")
    if object_type == "commit":
        return object_sha
    if object_type != "tag":
        raise TagStateError(f"Git tag {tag} targets unsupported object type {object_type!r}")

    # Dereference annotated (or nested annotated) tag objects to the underlying commit.
    for _depth in range(5):
        tag_url = f"{API_ROOT}/repos/{repository}/git/tags/{object_sha}"
        try:
            with _request(tag_url, token, timeout=timeout) as response:
                tag_payload = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TagStateError(f"Could not dereference annotated tag {tag}: {exc}") from exc
        obj = tag_payload.get("object") or {}
        object_type = str(obj.get("type", ""))
        object_sha = str(obj.get("sha", ""))
        if not SHA_RE.fullmatch(object_sha):
            raise TagStateError(
                f"GitHub returned an invalid dereferenced object SHA for tag {tag}: {object_sha!r}"
            )
        if object_type == "commit":
            return object_sha
        if object_type != "tag":
            raise TagStateError(
                f"Annotated tag {tag} ultimately targets unsupported object type {object_type!r}"
            )
    raise TagStateError(f"Annotated tag {tag} exceeded maximum dereference depth")


def _confirm_tag(
    repository: str,
    tag: str,
    sha: str,
    token: str,
    *,
    attempts: int = 5,
    delay: float = 2.0,
) -> bool:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            resolved = resolve_tag_commit(repository, tag, token)
        except TagStateError as exc:
            last_error = exc
        else:
            if resolved is None:
                last_error = TagStateError(f"Tag {tag} is still absent after create attempt")
            elif resolved != sha:
                raise TagStateError(
                    f"Git tag {tag} resolves to {resolved}, not validated build {sha}"
                )
            else:
                return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    if last_error:
        raise TagStateError(f"Could not confirm release tag {tag}: {last_error}") from last_error
    return False


def ensure_exact_tag(repository: str, tag: str, sha: str, token: str) -> str:
    _validate_inputs(repository, tag, sha, token)
    existing = resolve_tag_commit(repository, tag, token)
    if existing is not None:
        if existing != sha:
            raise TagStateError(
                f"Git tag {tag} already resolves to {existing}, not validated build {sha}"
            )
        return "already-exact"

    url = f"{API_ROOT}/repos/{repository}/git/refs"
    try:
        with _request(
            url,
            token,
            method="POST",
            body={"ref": f"refs/tags/{tag}", "sha": sha},
        ) as response:
            status = int(getattr(response, "status", 201))
            if status not in {200, 201}:
                raise TagStateError(f"Unexpected tag-create HTTP status: {status}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        # The write may have reached GitHub even if the client saw an error. Never
        # retry the write blindly; confirm the exact server-side ref instead.
        _confirm_tag(repository, tag, sha, token)
        return "created-after-client-error"

    _confirm_tag(repository, tag, sha, token)
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    result = ensure_exact_tag(args.repository, args.tag, args.sha, token)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
