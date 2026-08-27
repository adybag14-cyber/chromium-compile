#!/usr/bin/env python3
"""Create or resume a draft-first, byte-verified immutable GitHub release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from github_release_tag import TagStateError, ensure_exact_tag

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ImmutableReleaseError(RuntimeError):
    pass


def _run_gh(
    args: Sequence[str],
    *,
    token: str,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    try:
        result = subprocess.run(
            ["gh", *args],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImmutableReleaseError(f"gh {' '.join(args)} could not complete: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "GitHub CLI failure").strip()
        raise ImmutableReleaseError(f"gh {' '.join(args)} failed: {detail}")
    return result


def _release_view(repository: str, tag: str, token: str) -> dict[str, object] | None:
    result = _run_gh(
        [
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "isDraft,isImmutable,targetCommitish,assets",
        ],
        token=token,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").lower()
        if "release not found" in detail or "not found" in detail:
            return None
        raise ImmutableReleaseError(f"Could not inspect release {tag}: {detail.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ImmutableReleaseError("gh release view returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ImmutableReleaseError("gh release view returned non-object JSON")
    if not isinstance(payload.get("isDraft"), bool) or not isinstance(
        payload.get("isImmutable"), bool
    ):
        raise ImmutableReleaseError(
            "GitHub CLI did not expose isDraft/isImmutable release authority fields"
        )
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ImmutableReleaseError("GitHub release response lacks assets list")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _local_assets(paths: Sequence[Path]) -> dict[str, tuple[Path, str]]:
    if not paths:
        raise ImmutableReleaseError("At least one release asset is required")
    resolved: dict[str, tuple[Path, str]] = {}
    for path in paths:
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise ImmutableReleaseError(f"Release asset is not a non-empty regular file: {path}")
        if path.name in resolved:
            raise ImmutableReleaseError(f"Duplicate local release asset name: {path.name}")
        resolved[path.name] = (path, _sha256(path))
    return resolved


def _remote_assets(payload: dict[str, object]) -> dict[str, str]:
    values = payload["assets"]
    assert isinstance(values, list)
    resolved: dict[str, str] = {}
    for raw in values:
        if not isinstance(raw, dict):
            raise ImmutableReleaseError("Release contains malformed asset metadata")
        name = str(raw.get("name", ""))
        digest = str(raw.get("digest", ""))
        state = str(raw.get("state", ""))
        size = raw.get("size")
        if not name or name in resolved:
            raise ImmutableReleaseError(f"Release contains duplicate/empty asset name: {name!r}")
        if state != "uploaded" or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ImmutableReleaseError(f"Release asset {name!r} is not a complete upload")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ImmutableReleaseError(
                f"Release asset {name!r} lacks GitHub SHA-256 digest authority"
            )
        resolved[name] = digest
    return resolved


def _validate_asset_state(
    payload: dict[str, object],
    local: dict[str, tuple[Path, str]],
    *,
    allow_missing: bool,
) -> list[Path]:
    remote = _remote_assets(payload)
    unexpected = sorted(set(remote) - set(local))
    if unexpected:
        raise ImmutableReleaseError(
            "Release contains unexpected assets and will not be mutated: "
            + ", ".join(unexpected)
        )
    missing: list[Path] = []
    for name, (path, digest) in local.items():
        remote_digest = remote.get(name)
        if remote_digest is None:
            if allow_missing:
                missing.append(path)
                continue
            raise ImmutableReleaseError(f"Release is missing required asset {name}")
        if remote_digest != digest:
            raise ImmutableReleaseError(
                f"Release asset {name} differs from validated local bytes; refusing overwrite"
            )
    return missing


def publish_immutable_release(
    *,
    repository: str,
    tag: str,
    sha: str,
    title: str,
    notes: str,
    assets: Sequence[Path],
    token: str,
) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ImmutableReleaseError(f"Invalid repository: {repository!r}")
    if not TAG_RE.fullmatch(tag):
        raise ImmutableReleaseError(f"Invalid release tag: {tag!r}")
    sha = sha.lower()
    if not SHA_RE.fullmatch(sha):
        raise ImmutableReleaseError("Release SHA must be exactly 40 lowercase hex characters")
    if not token:
        raise ImmutableReleaseError("GH_TOKEN is required")
    local = _local_assets(assets)
    try:
        tag_state = ensure_exact_tag(repository, tag, sha, token)
    except TagStateError as exc:
        raise ImmutableReleaseError(str(exc)) from exc
    print(f"Exact release tag verified: {tag} -> {sha} ({tag_state})")

    payload = _release_view(repository, tag, token)
    if payload is None:
        result = _run_gh(
            [
                "release",
                "create",
                tag,
                "--repo",
                repository,
                "--draft",
                "--title",
                title,
                "--notes",
                notes,
            ],
            token=token,
            check=False,
        )
        payload = _release_view(repository, tag, token)
        if payload is None:
            detail = (result.stderr or result.stdout or "draft create was not accepted").strip()
            raise ImmutableReleaseError(f"Could not establish draft release {tag}: {detail}")
        if payload["isDraft"] is not True:
            raise ImmutableReleaseError("A non-draft release appeared during draft creation")

    if payload["isDraft"] is False:
        if payload["isImmutable"] is not True:
            raise ImmutableReleaseError("Existing published release is mutable")
        _validate_asset_state(payload, local, allow_missing=False)
        # Tag was checked both before and after reading immutable asset state.
        ensure_exact_tag(repository, tag, sha, token)
        return "already-published-identical"

    if payload["isImmutable"] is True:
        raise ImmutableReleaseError("GitHub reported an impossible immutable draft state")
    missing = _validate_asset_state(payload, local, allow_missing=True)
    for path in missing:
        result = _run_gh(
            ["release", "upload", tag, str(path), "--repo", repository],
            token=token,
            timeout=900,
            check=False,
        )
        payload = _release_view(repository, tag, token)
        if payload is None:
            raise ImmutableReleaseError("Draft release disappeared during asset upload")
        try:
            _validate_asset_state(payload, local, allow_missing=True)
        except ImmutableReleaseError:
            raise
        if path.name not in _remote_assets(payload):
            detail = (result.stderr or result.stdout or "upload was not accepted").strip()
            raise ImmutableReleaseError(f"Could not establish uploaded asset {path.name}: {detail}")

    payload = _release_view(repository, tag, token)
    if payload is None or payload["isDraft"] is not True:
        raise ImmutableReleaseError("Draft release state changed before publication")
    _validate_asset_state(payload, local, allow_missing=False)
    ensure_exact_tag(repository, tag, sha, token)
    result = _run_gh(
        ["release", "edit", tag, "--repo", repository, "--draft=false"],
        token=token,
        check=False,
    )
    final = _release_view(repository, tag, token)
    if final is None or final["isDraft"] is not False or final["isImmutable"] is not True:
        detail = (result.stderr or result.stdout or "publish transition was not accepted").strip()
        raise ImmutableReleaseError(
            f"Release {tag} did not become GitHub-immutable: {detail}"
        )
    _validate_asset_state(final, local, allow_missing=False)
    ensure_exact_tag(repository, tag, sha, token)
    return "published-immutable"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--asset", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        result = publish_immutable_release(
            repository=args.repository,
            tag=args.tag,
            sha=args.sha,
            title=args.title,
            notes=args.notes,
            assets=args.asset,
            token=os.environ.get("GH_TOKEN", ""),
        )
    except (ImmutableReleaseError, TagStateError) as exc:
        parser.error(str(exc))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
