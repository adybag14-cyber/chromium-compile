#!/usr/bin/env python3
"""Validate Chromium source tar paths before extraction."""
from __future__ import annotations

import argparse
import posixpath
import tarfile
from pathlib import Path, PurePosixPath


def _safe_member_path(name: str, expected_root: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"Unsafe source archive member path: {name}")
    normalized = posixpath.normpath(path.as_posix())
    if normalized != expected_root and not normalized.startswith(expected_root + "/"):
        raise ValueError(
            f"Source archive member is outside expected root {expected_root!r}: {name}"
        )
    return normalized


def _safe_link_target(member: tarfile.TarInfo, expected_root: str) -> None:
    target = member.linkname
    if not target:
        return
    if PurePosixPath(target).is_absolute():
        raise ValueError(f"Absolute source archive link target: {member.name} -> {target}")
    if member.issym():
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), target))
    else:
        # Tar hard-link targets name another archive member from archive root.
        resolved = posixpath.normpath(target)
    if resolved != expected_root and not resolved.startswith(expected_root + "/"):
        raise ValueError(
            f"Source archive link escapes expected root: {member.name} -> {target} ({resolved})"
        )


def validate_source_archive(path: Path, version: str) -> None:
    expected_root = f"chromium-{version}"
    names: set[str] = set()
    with tarfile.open(path, mode="r:xz") as archive:
        for member in archive:
            normalized = _safe_member_path(member.name.rstrip("/"), expected_root)
            if normalized in names:
                raise ValueError(f"Duplicate source archive member path: {normalized}")
            names.add(normalized)
            if member.isdev() or member.isfifo():
                raise ValueError(f"Unsupported special source archive member: {member.name}")
            if member.issym() or member.islnk():
                _safe_link_target(member, expected_root)
    if expected_root not in names:
        raise ValueError(f"Source archive lacks expected top-level root: {expected_root}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    validate_source_archive(args.archive, args.version)
    print(f"Chromium source archive paths validated: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
