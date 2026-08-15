#!/usr/bin/env python3
"""Stream-validate Chromium source tar paths and extraction resource bounds."""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import tarfile
from pathlib import Path, PurePosixPath

DEFAULT_MAX_MEMBERS = 2_000_000
DEFAULT_MAX_UNPACKED_GIB = 80
HARD_MAX_MEMBERS = 4_000_000
HARD_MAX_UNPACKED_GIB = 160
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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
        raise ValueError(f"Empty source archive link target: {member.name}")
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


def _positive_limit(value: int, name: str, hard_max: int) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if value > hard_max:
        raise ValueError(f"{name} must not exceed hard maximum {hard_max}")
    return value


def _env_limit(name: str, default: int, hard_max: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    return _positive_limit(value, name, hard_max)


def validate_source_archive(
    path: Path,
    version: str,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_GIB * 1024**3,
) -> dict[str, int]:
    expected_root = f"chromium-{version}"
    max_members = _positive_limit(max_members, "max_members", HARD_MAX_MEMBERS)
    max_unpacked_bytes = _positive_limit(max_unpacked_bytes, "max_unpacked_bytes", HARD_MAX_UNPACKED_GIB * 1024**3)
    names: set[str] = set()
    member_count = 0
    unpacked_bytes = 0

    # Stream mode avoids TarFile retaining every TarInfo while the explicit names
    # set remains bounded by max_members for deterministic duplicate detection.
    with tarfile.open(path, mode="r|xz") as archive:
        for member in archive:
            member_count += 1
            if member_count > max_members:
                raise ValueError(
                    f"Source archive exceeds member limit {max_members}: {member_count}"
                )
            normalized = _safe_member_path(member.name.rstrip("/"), expected_root)
            if normalized in names:
                raise ValueError(f"Duplicate source archive member path: {normalized}")
            names.add(normalized)
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f"Unsupported special source archive member: {member.name}")
            if member.issym() or member.islnk():
                _safe_link_target(member, expected_root)
            if member.isfile():
                if member.size < 0:
                    raise ValueError(f"Source archive member has negative size: {member.name}")
                if member.size > max_unpacked_bytes - unpacked_bytes:
                    raise ValueError(
                        "Source archive exceeds unpacked-byte limit "
                        f"{max_unpacked_bytes}: {unpacked_bytes + member.size}"
                    )
                unpacked_bytes += member.size

    if expected_root not in names:
        raise ValueError(f"Source archive lacks expected top-level root: {expected_root}")
    return {"member_count": member_count, "unpacked_bytes": unpacked_bytes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha256")
    parser.add_argument("--stats-file", type=Path)
    parser.add_argument("--max-members", type=int)
    parser.add_argument("--max-unpacked-bytes", type=int)
    args = parser.parse_args()

    max_members = args.max_members
    if max_members is None:
        max_members = _env_limit("CHROMIUM_I686_MAX_SOURCE_MEMBERS", DEFAULT_MAX_MEMBERS, HARD_MAX_MEMBERS)
    max_unpacked_bytes = args.max_unpacked_bytes
    if max_unpacked_bytes is None:
        max_gib = _env_limit(
            "CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB", DEFAULT_MAX_UNPACKED_GIB, HARD_MAX_UNPACKED_GIB
        )
        max_unpacked_bytes = max_gib * 1024**3

    stats = validate_source_archive(
        args.archive,
        args.version,
        max_members=max_members,
        max_unpacked_bytes=max_unpacked_bytes,
    )
    payload: dict[str, object] = {"version": args.version, **stats}
    if args.source_sha256 is not None:
        if not SHA256_RE.fullmatch(args.source_sha256):
            raise ValueError("--source-sha256 must be a 64-character hexadecimal SHA-256")
        payload["source_sha256"] = args.source_sha256.lower()
    if args.stats_file is not None:
        if "source_sha256" not in payload:
            raise ValueError("--stats-file requires --source-sha256 so cached stats are byte-bound")
        args.stats_file.parent.mkdir(parents=True, exist_ok=True)
        args.stats_file.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Chromium source archive validated: {args.archive}; "
        f"members={stats['member_count']}; unpacked_bytes={stats['unpacked_bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
