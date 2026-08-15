#!/usr/bin/env python3
"""Validate a Chromium Ninja checkpoint tar.zst before any extraction occurs."""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

ROOT = "Release_x86"
REQUIRED_REGULAR = {f"{ROOT}/build.ninja", f"{ROOT}/args.gn"}
DEFAULT_MAX_UNPACKED_GIB = 40
DEFAULT_MAX_MEMBERS = 2_000_000
HARD_MAX_UNPACKED_GIB = 80
HARD_MAX_MEMBERS = 4_000_000


def _normalise_member(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"Unsafe checkpoint member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise ValueError(f"Absolute checkpoint member path: {name!r}")
    normal = posixpath.normpath(name)
    if normal in {".", ""}:
        raise ValueError(f"Empty checkpoint member path: {name!r}")
    parts = PurePosixPath(normal).parts
    if not parts or parts[0] != ROOT or ".." in parts:
        raise ValueError(f"Checkpoint member escapes {ROOT}: {name!r}")
    return normal


def _normalise_link(member_name: str, link_name: str, *, symlink: bool) -> str:
    if not link_name or "\x00" in link_name or "\\" in link_name:
        raise ValueError(f"Unsafe checkpoint link target: {member_name!r} -> {link_name!r}")
    target = PurePosixPath(link_name)
    if target.is_absolute():
        raise ValueError(f"Absolute checkpoint link target: {member_name!r} -> {link_name!r}")
    base = posixpath.dirname(member_name) if symlink else ""
    normal = posixpath.normpath(posixpath.join(base, link_name))
    parts = PurePosixPath(normal).parts
    if not parts or parts[0] != ROOT or ".." in parts:
        raise ValueError(f"Checkpoint link escapes {ROOT}: {member_name!r} -> {link_name!r}")
    return normal


def _positive_int_env(name: str, default: int, hard_max: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive, got {value}")
    if value > hard_max:
        raise ValueError(f"{name} must not exceed hard maximum {hard_max}")
    return value


def validate_checkpoint(path: Path) -> dict[str, int]:
    seen: set[str] = set()
    regular: set[str] = set()
    links: list[tuple[str, str]] = []
    if "CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_BYTES" in os.environ:
        max_unpacked = _positive_int_env("CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_BYTES", DEFAULT_MAX_UNPACKED_GIB * 1024**3, HARD_MAX_UNPACKED_GIB * 1024**3)
    else:
        max_unpacked = _positive_int_env("CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_GIB", DEFAULT_MAX_UNPACKED_GIB, HARD_MAX_UNPACKED_GIB) * 1024**3
    max_members = _positive_int_env("CHROMIUM_I686_MAX_CHECKPOINT_MEMBERS", DEFAULT_MAX_MEMBERS, HARD_MAX_MEMBERS)
    unpacked_bytes = 0
    member_count = 0
    proc = subprocess.Popen(
        ["zstd", "-q", "-d", "-c", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                member_count += 1
                if member_count > max_members:
                    raise ValueError(
                        f"Checkpoint archive exceeds configured member limit {max_members}"
                    )
                if member.size < 0:
                    raise ValueError(f"Checkpoint member has negative size: {member.name!r}")
                unpacked_bytes += member.size
                if unpacked_bytes > max_unpacked:
                    raise ValueError(
                        "Checkpoint archive declares more than the configured "
                        f"{max_unpacked // 1024**3} GiB unpacked limit"
                    )
                name = _normalise_member(member.name)
                if name in seen:
                    raise ValueError(f"Duplicate checkpoint archive member: {name}")
                seen.add(name)
                if member.isreg():
                    regular.add(name)
                elif member.isdir():
                    pass
                elif member.issym():
                    links.append((name, _normalise_link(name, member.linkname, symlink=True)))
                elif member.islnk():
                    links.append((name, _normalise_link(name, member.linkname, symlink=False)))
                else:
                    raise ValueError(
                        f"Unsupported special checkpoint member {name!r} (type {member.type!r})"
                    )
    except Exception:
        proc.kill()
        proc.wait(timeout=10)
        raise
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    status = proc.wait(timeout=30)
    if status != 0:
        raise ValueError(f"zstd failed while validating checkpoint archive: {stderr.strip()}")
    missing = sorted(REQUIRED_REGULAR - regular)
    if missing:
        raise ValueError(f"Checkpoint archive lacks required regular files: {', '.join(missing)}")
    # A contained link still must resolve to an archive member. This rejects broken
    # links and prevents an extraction-time link from targeting an ambient filesystem path.
    for name, target in links:
        if target not in seen:
            raise ValueError(f"Checkpoint link target is absent from archive: {name!r} -> {target!r}")
    return {"member_count": member_count, "unpacked_bytes": unpacked_bytes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--stats-file", type=Path)
    args = parser.parse_args()
    stats = validate_checkpoint(args.archive)
    if args.stats_file:
        args.stats_file.write_text(json.dumps(stats, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Checkpoint archive safety validated: {args.archive}; "
        f"members={stats['member_count']}; unpacked_bytes={stats['unpacked_bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
