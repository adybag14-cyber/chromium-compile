#!/usr/bin/env python3
"""Validate Chromium release archive paths and required runtime contents."""
from __future__ import annotations

import argparse
import tarfile
from pathlib import Path, PurePosixPath

from chromium_linux_runtime import REQUIRED_RUNTIME


def _unsafe(value: str) -> bool:
    path = PurePosixPath(value)
    return path.is_absolute() or any(part == ".." for part in path.parts)


def validate_archive(path: Path) -> None:
    names: set[str] = set()
    with tarfile.open(path, mode="r:xz") as archive:
        for member in archive.getmembers():
            if _unsafe(member.name):
                raise ValueError(f"Unsafe archive member path: {member.name}")
            normalized = PurePosixPath(member.name).as_posix().rstrip("/")
            if normalized in names:
                raise ValueError(f"Duplicate archive member path: {normalized}")
            if member.isdev() or member.isfifo():
                raise ValueError(f"Unsupported special archive member: {member.name}")
            if member.issym() or member.islnk():
                if _unsafe(member.linkname):
                    raise ValueError(f"Unsafe archive link target: {member.name} -> {member.linkname}")
            names.add(normalized)
    missing: list[str] = []
    for required in sorted(REQUIRED_RUNTIME):
        if required == "locales":
            if not any(name == "locales" or name.startswith("locales/") for name in names):
                missing.append(required)
        elif required not in names:
            missing.append(required)
    if missing:
        raise ValueError("Release archive is missing required runtime paths: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    validate_archive(args.archive)
    print(f"Release archive validated: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
