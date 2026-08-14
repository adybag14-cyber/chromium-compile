#!/usr/bin/env python3
"""Validate a Chromium Ninja checkpoint tar.zst before any extraction occurs."""
from __future__ import annotations

import argparse
import posixpath
import subprocess
import tarfile
from pathlib import PurePosixPath

ROOT = "Release_x86"
REQUIRED_REGULAR = {f"{ROOT}/build.ninja", f"{ROOT}/args.gn"}


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


def validate_checkpoint(path: Path) -> None:
    seen: set[str] = set()
    regular: set[str] = set()
    links: list[tuple[str, str]] = []
    proc = subprocess.Popen(
        ["zstd", "-q", "-d", "-c", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    validate_checkpoint(args.archive)
    print(f"Checkpoint archive safety validated: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
