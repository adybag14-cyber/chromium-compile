#!/usr/bin/env python3
"""Stream-validate Chromium release archive paths, bounds, and runtime contents."""
from __future__ import annotations

import argparse
import json
import os
import tarfile
from pathlib import Path, PurePosixPath

from chromium_linux_runtime import REQUIRED_EXECUTABLE_RUNTIME, REQUIRED_RUNTIME

DEFAULT_MAX_MEMBERS = 250_000
DEFAULT_MAX_UNPACKED_GIB = 8


def _unsafe(value: str) -> bool:
    path = PurePosixPath(value)
    return path.is_absolute() or any(part == ".." for part in path.parts)


def _positive_limit(value: int, name: str) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _env_limit(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    return _positive_limit(value, name)


def validate_archive(
    path: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_GIB * 1024**3,
) -> dict[str, int]:
    max_members = _positive_limit(max_members, "max_members")
    max_unpacked_bytes = _positive_limit(max_unpacked_bytes, "max_unpacked_bytes")
    names: set[str] = set()
    nonempty_regular_files: set[str] = set()
    executable_regular_files: set[str] = set()
    member_count = 0
    unpacked_bytes = 0

    # Streaming mode avoids materializing every TarInfo object at once. The names
    # set remains bounded by max_members so duplicate detection is deterministic.
    with tarfile.open(path, mode="r|xz") as archive:
        for member in archive:
            member_count += 1
            if member_count > max_members:
                raise ValueError(
                    f"Release archive exceeds member limit {max_members}: {member_count}"
                )
            if _unsafe(member.name):
                raise ValueError(f"Unsafe archive member path: {member.name}")
            normalized = PurePosixPath(member.name).as_posix().rstrip("/")
            if not normalized or normalized == ".":
                raise ValueError(f"Release archive contains an empty/root member path: {member.name!r}")
            if normalized in names:
                raise ValueError(f"Duplicate archive member path: {normalized}")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f"Unsupported special archive member: {member.name}")
            if member.issym() or member.islnk():
                if not member.linkname or _unsafe(member.linkname):
                    raise ValueError(
                        f"Unsafe archive link target: {member.name} -> {member.linkname}"
                    )
            names.add(normalized)
            if member.isfile():
                if member.size < 0:
                    raise ValueError(f"Release archive member has negative size: {member.name}")
                if member.size > max_unpacked_bytes - unpacked_bytes:
                    raise ValueError(
                        "Release archive exceeds unpacked-byte limit "
                        f"{max_unpacked_bytes}: {unpacked_bytes + member.size}"
                    )
                unpacked_bytes += member.size
                if member.size > 0:
                    nonempty_regular_files.add(normalized)
                    if member.mode & 0o111:
                        executable_regular_files.add(normalized)

    missing: list[str] = []
    for required in sorted(REQUIRED_RUNTIME):
        if required == "locales":
            if not any(name.startswith("locales/") for name in nonempty_regular_files):
                missing.append(required)
        elif required not in nonempty_regular_files:
            missing.append(required)
    if missing:
        raise ValueError("Release archive is missing required runtime paths: " + ", ".join(missing))
    non_executable = sorted(REQUIRED_EXECUTABLE_RUNTIME - executable_regular_files)
    if non_executable:
        raise ValueError(
            "Release archive required runtime executables lack execute permission: "
            + ", ".join(non_executable)
        )
    return {"member_count": member_count, "unpacked_bytes": unpacked_bytes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--stats-file", type=Path)
    parser.add_argument("--max-members", type=int)
    parser.add_argument("--max-unpacked-bytes", type=int)
    args = parser.parse_args()

    max_members = args.max_members
    if max_members is None:
        max_members = _env_limit("CHROMIUM_I686_MAX_RELEASE_MEMBERS", DEFAULT_MAX_MEMBERS)
    max_unpacked_bytes = args.max_unpacked_bytes
    if max_unpacked_bytes is None:
        max_gib = _env_limit(
            "CHROMIUM_I686_MAX_RELEASE_UNPACKED_GIB", DEFAULT_MAX_UNPACKED_GIB
        )
        max_unpacked_bytes = max_gib * 1024**3

    stats = validate_archive(
        args.archive,
        max_members=max_members,
        max_unpacked_bytes=max_unpacked_bytes,
    )
    if args.stats_file is not None:
        args.stats_file.parent.mkdir(parents=True, exist_ok=True)
        args.stats_file.write_text(json.dumps(stats, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Release archive validated: {args.archive}; "
        f"members={stats['member_count']}; unpacked_bytes={stats['unpacked_bytes']}"
    )
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
