#!/usr/bin/env python3
"""Validate, package, extract, and smoke-test Chromium Windows x86 runtimes."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
PE_SIGNATURE = b"PE\0\0"
PE_MACHINE_I386 = 0x014C
PE32_OPTIONAL_MAGIC = 0x010B
DEFAULT_MAX_MEMBERS = 250_000
DEFAULT_MAX_UNPACKED_GIB = 16
HARD_MAX_MEMBERS = 1_000_000
HARD_MAX_UNPACKED_GIB = 32
MAX_PE_HEADER_OFFSET = 16 * 1024 * 1024
REQUIRED_BASENAMES = frozenset(
    (
        "chrome.exe",
        "chrome.dll",
        "chrome_elf.dll",
        "icudtl.dat",
        "resources.pak",
        "mini_installer.exe",
    )
)
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class WindowsRuntimeError(RuntimeError):
    """Raised when Windows runtime bytes violate the release contract."""


def _positive_limit(value: int, name: str, hard_max: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WindowsRuntimeError(f"{name} must be a positive integer")
    if value > hard_max:
        raise WindowsRuntimeError(f"{name} must not exceed hard maximum {hard_max}")
    return value


def _env_limit(name: str, default: int, hard_max: int) -> int:
    raw = os.environ.get(name, str(default))
    if not re.fullmatch(r"[1-9][0-9]{0,9}", raw):
        raise WindowsRuntimeError(f"{name} must be a bounded positive integer, got {raw!r}")
    return _positive_limit(int(raw), name, hard_max)


def release_root(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise WindowsRuntimeError(f"Invalid Chromium version: {version!r}")
    return f"chromium-{version}-windows-i686"


def _safe_member_name(name: str, *, expected_root: str | None = None) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise WindowsRuntimeError(f"Unsafe archive member name: {name!r}")
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise WindowsRuntimeError(f"Unsafe archive member path: {name!r}")
    if re.match(r"^[A-Za-z]:", path.as_posix()):
        raise WindowsRuntimeError(f"Drive-qualified archive member path: {name!r}")
    for part in path.parts:
        if any(ord(character) < 32 for character in part):
            raise WindowsRuntimeError(f"Control character in archive member path: {name!r}")
        if ":" in part or part.endswith((" ", ".")):
            raise WindowsRuntimeError(f"Windows-ambiguous archive member path: {name!r}")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise WindowsRuntimeError(f"Windows device-name archive member path: {name!r}")
    normal = path.as_posix()
    if normal in {"", "."}:
        raise WindowsRuntimeError(f"Empty archive member path: {name!r}")
    if expected_root is not None and not (
        normal == expected_root or normal.startswith(expected_root + "/")
    ):
        raise WindowsRuntimeError(
            f"Archive member is outside expected root {expected_root!r}: {name!r}"
        )
    return normal


def _read_exact(handle: BinaryIO, size: int, context: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise WindowsRuntimeError(f"Truncated PE while reading {context}")
    return data


def validate_pe32_stream(handle: BinaryIO, label: str) -> None:
    if _read_exact(handle, 2, f"{label} DOS signature") != b"MZ":
        raise WindowsRuntimeError(f"Windows runtime binary lacks MZ signature: {label}")
    handle.seek(0x3C)
    pe_offset = struct.unpack("<I", _read_exact(handle, 4, f"{label} PE offset"))[0]
    if pe_offset < 0x40 or pe_offset > MAX_PE_HEADER_OFFSET:
        raise WindowsRuntimeError(f"Windows runtime binary has unsafe PE offset: {label}")
    handle.seek(pe_offset)
    if _read_exact(handle, 4, f"{label} PE signature") != PE_SIGNATURE:
        raise WindowsRuntimeError(f"Windows runtime binary lacks PE signature: {label}")
    machine = struct.unpack("<H", _read_exact(handle, 2, f"{label} machine"))[0]
    if machine != PE_MACHINE_I386:
        raise WindowsRuntimeError(
            f"Windows runtime binary is not Intel i386 (machine=0x{machine:04x}): {label}"
        )
    handle.seek(pe_offset + 24)
    optional_magic = struct.unpack(
        "<H", _read_exact(handle, 2, f"{label} optional-header magic")
    )[0]
    if optional_magic != PE32_OPTIONAL_MAGIC:
        raise WindowsRuntimeError(
            f"Windows runtime binary is not PE32 (magic=0x{optional_magic:04x}): {label}"
        )


def validate_pe32(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise WindowsRuntimeError(f"PE path is not a regular file: {path}")
    with path.open("rb") as handle:
        validate_pe32_stream(handle, str(path))


def _zip_member_is_link(info: zipfile.ZipInfo) -> bool:
    if info.create_system != 3:
        return False
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def validate_release_zip(
    path: Path,
    version: str,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_GIB * 1024**3,
) -> dict[str, int]:
    expected_root = release_root(version)
    max_members = _positive_limit(max_members, "max_members", HARD_MAX_MEMBERS)
    max_unpacked_bytes = _positive_limit(
        max_unpacked_bytes,
        "max_unpacked_bytes",
        HARD_MAX_UNPACKED_GIB * 1024**3,
    )
    if not path.is_file() or path.is_symlink():
        raise WindowsRuntimeError(f"Release ZIP is not a regular file: {path}")

    seen: set[str] = set()
    seen_casefold: set[str] = set()
    basenames: set[str] = set()
    locale_present = False
    pe_count = 0
    unpacked_bytes = 0
    member_count = 0
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise WindowsRuntimeError("Release ZIP failed CRC validation")
        for info in archive.infolist():
            member_count += 1
            if member_count > max_members:
                raise WindowsRuntimeError(
                    f"Release ZIP exceeds configured member limit {max_members}"
                )
            name = _safe_member_name(info.filename, expected_root=expected_root)
            if name in seen:
                raise WindowsRuntimeError(f"Duplicate release ZIP member: {name}")
            folded = name.casefold()
            if folded in seen_casefold:
                raise WindowsRuntimeError(
                    f"Case-insensitive duplicate release ZIP member: {name}"
                )
            seen.add(name)
            seen_casefold.add(folded)
            if info.flag_bits & 0x1:
                raise WindowsRuntimeError(f"Encrypted release ZIP member is forbidden: {name}")
            if _zip_member_is_link(info):
                raise WindowsRuntimeError(f"Release ZIP symbolic link is forbidden: {name}")
            if info.file_size < 0 or info.compress_size < 0:
                raise WindowsRuntimeError(f"Release ZIP has negative member size: {name}")
            if info.file_size > max_unpacked_bytes - unpacked_bytes:
                raise WindowsRuntimeError(
                    f"Release ZIP exceeds unpacked-byte limit {max_unpacked_bytes}"
                )
            unpacked_bytes += info.file_size
            if info.is_dir():
                continue
            basename = PurePosixPath(name).name.lower()
            if info.file_size > 0:
                basenames.add(basename)
            lower_name = name.lower()
            if info.file_size > 0 and "/locales/" in lower_name and basename == "en-us.pak":
                locale_present = True
            if basename.endswith((".exe", ".dll")):
                with archive.open(info, "r") as handle:
                    validate_pe32_stream(handle, name)
                pe_count += 1

    missing = sorted(REQUIRED_BASENAMES - basenames)
    if missing:
        raise WindowsRuntimeError(
            "Release ZIP is missing required Windows runtime files: " + ", ".join(missing)
        )
    if not locale_present:
        raise WindowsRuntimeError("Release ZIP is missing locales/en-US.pak")
    if pe_count < 3:
        raise WindowsRuntimeError(
            f"Release ZIP contains only {pe_count} PE32 binaries; runtime closure is incomplete"
        )
    return {
        "member_count": member_count,
        "unpacked_bytes": unpacked_bytes,
        "pe32_count": pe_count,
    }


def extract_release_zip(path: Path, version: str, destination: Path) -> Path:
    stats = validate_release_zip(path, version)
    expected_root = release_root(version)
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = _safe_member_name(info.filename, expected_root=expected_root)
            target = destination.joinpath(*PurePosixPath(name).parts)
            resolved_target = target.resolve()
            if resolved_target != resolved_destination and resolved_destination not in resolved_target.parents:
                raise WindowsRuntimeError(f"ZIP extraction target escaped destination: {name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
    root = destination / expected_root
    if not root.is_dir():
        raise WindowsRuntimeError("Validated release ZIP did not extract its expected root")
    print(
        f"Extracted validated release ZIP: members={stats['member_count']}; "
        f"unpacked_bytes={stats['unpacked_bytes']}"
    )
    return root


def _parse_7z_records(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if key in current:
            raise WindowsRuntimeError(f"7z listing repeated field {key!r}")
        current[key] = value
    if current:
        records.append(current)
    return [record for record in records if "Path" in record]


def list_7z_runtime(
    archive: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_GIB * 1024**3,
) -> dict[str, int]:
    max_members = _positive_limit(max_members, "max_members", HARD_MAX_MEMBERS)
    max_unpacked_bytes = _positive_limit(
        max_unpacked_bytes,
        "max_unpacked_bytes",
        HARD_MAX_UNPACKED_GIB * 1024**3,
    )
    result = subprocess.run(
        ["7z", "l", "-slt", "-ba", str(archive)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0:
        raise WindowsRuntimeError(
            f"7z could not list Chromium runtime archive: {(result.stderr or result.stdout).strip()}"
        )
    records = _parse_7z_records(result.stdout)
    if not records:
        raise WindowsRuntimeError("Chromium runtime 7z listing is empty")
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    unpacked = 0
    for index, record in enumerate(records, 1):
        if index > max_members:
            raise WindowsRuntimeError(f"Runtime 7z exceeds member limit {max_members}")
        name = _safe_member_name(record["Path"])
        if name in seen:
            raise WindowsRuntimeError(f"Duplicate runtime 7z member: {name}")
        folded = name.casefold()
        if folded in seen_casefold:
            raise WindowsRuntimeError(
                f"Case-insensitive duplicate runtime 7z member: {name}"
            )
        seen.add(name)
        seen_casefold.add(folded)
        if record.get("Anti", "-") not in {"", "-"}:
            raise WindowsRuntimeError(f"Runtime 7z contains an anti-item: {name}")
        if any(key in record for key in ("Symbolic Link", "Hard Link", "Alternate Stream")):
            raise WindowsRuntimeError(f"Runtime 7z contains a link/stream member: {name}")
        raw_size = record.get("Size", "0")
        if not raw_size.isdigit():
            raise WindowsRuntimeError(f"Runtime 7z member has invalid size: {name}")
        size = int(raw_size)
        if size > max_unpacked_bytes - unpacked:
            raise WindowsRuntimeError(
                f"Runtime 7z exceeds unpacked-byte limit {max_unpacked_bytes}"
            )
        unpacked += size
    return {"member_count": len(records), "unpacked_bytes": unpacked}


def extract_7z_runtime(archive: Path, destination: Path) -> dict[str, int]:
    stats = list_7z_runtime(archive)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise WindowsRuntimeError(f"Runtime extraction destination is not empty: {destination}")
    result = subprocess.run(
        ["7z", "x", "-y", f"-o{destination}", str(archive)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if result.returncode != 0:
        raise WindowsRuntimeError(f"7z runtime extraction failed: {result.stdout.strip()}")
    return stats


def validate_runtime_tree(root: Path) -> dict[str, int]:
    if not root.is_dir() or root.is_symlink():
        raise WindowsRuntimeError(f"Runtime root is not a regular directory: {root}")
    basenames: set[str] = set()
    locale_present = False
    pe_count = 0
    file_count = 0
    total_bytes = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise WindowsRuntimeError(f"Runtime tree contains a symbolic link: {path}")
        if not path.is_file():
            continue
        file_count += 1
        size = path.stat().st_size
        total_bytes += size
        basename = path.name.lower()
        if size > 0:
            basenames.add(basename)
        if size > 0 and path.parent.name.lower() == "locales" and basename == "en-us.pak":
            locale_present = True
        if basename.endswith((".exe", ".dll")):
            validate_pe32(path)
            pe_count += 1
    missing = sorted(REQUIRED_BASENAMES - basenames)
    if missing:
        raise WindowsRuntimeError(
            "Runtime tree is missing required Windows files: " + ", ".join(missing)
        )
    if not locale_present:
        raise WindowsRuntimeError("Runtime tree is missing locales/en-US.pak")
    return {"file_count": file_count, "unpacked_bytes": total_bytes, "pe32_count": pe_count}


def write_release_zip(
    root: Path,
    destination: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_GIB * 1024**3,
) -> dict[str, int]:
    """Write a bounded release ZIP with canonical POSIX member names.

    Native Windows archivers commonly record ``\\`` separators in ZIP member
    names. Those names are ambiguous across extractors and intentionally fail
    the release validator. Build the ZIP in-process and provide every arcname
    explicitly so a Windows checkout produces the same safe member namespace
    as other hosts.
    """
    max_members = _positive_limit(max_members, "max_members", HARD_MAX_MEMBERS)
    max_unpacked_bytes = _positive_limit(
        max_unpacked_bytes,
        "max_unpacked_bytes",
        HARD_MAX_UNPACKED_GIB * 1024**3,
    )
    if not root.is_dir() or root.is_symlink():
        raise WindowsRuntimeError(f"Release ZIP root is not a regular directory: {root}")
    expected_root = _safe_member_name(root.name)
    entries: list[tuple[Path, str]] = []
    seen_casefold: set[str] = set()
    unpacked_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise WindowsRuntimeError(f"Release ZIP source contains a symbolic link: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise WindowsRuntimeError(f"Release ZIP source is not a regular file: {path}")
        if len(entries) >= max_members:
            raise WindowsRuntimeError(
                f"Release ZIP source exceeds configured member limit {max_members}"
            )
        relative = path.relative_to(root)
        member_name = PurePosixPath(expected_root, *relative.parts).as_posix()
        member_name = _safe_member_name(member_name, expected_root=expected_root)
        folded = member_name.casefold()
        if folded in seen_casefold:
            raise WindowsRuntimeError(
                f"Case-insensitive duplicate release ZIP source: {member_name}"
            )
        seen_casefold.add(folded)
        size = path.stat().st_size
        if size < 0 or size > max_unpacked_bytes - unpacked_bytes:
            raise WindowsRuntimeError(
                f"Release ZIP source exceeds unpacked-byte limit {max_unpacked_bytes}"
            )
        unpacked_bytes += size
        entries.append((path, member_name))
    if not entries:
        raise WindowsRuntimeError("Release ZIP source tree is empty")
    try:
        with zipfile.ZipFile(
            destination,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=7,
            allowZip64=True,
        ) as archive:
            for source, member_name in entries:
                archive.write(source, arcname=member_name)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print(
        f"Wrote canonical Windows release ZIP: members={len(entries)}; "
        f"unpacked_bytes={unpacked_bytes}"
    )
    return {"member_count": len(entries), "unpacked_bytes": unpacked_bytes}


def _terminate_windows_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


def smoke_test_runtime(root: Path, *, timeout_seconds: int = 120) -> None:
    if os.name != "nt":
        raise WindowsRuntimeError("Windows runtime smoke must run on a Windows host")
    if not 30 <= timeout_seconds <= 300:
        raise WindowsRuntimeError("timeout_seconds must be between 30 and 300")
    chrome_candidates = sorted(root.rglob("chrome.exe"), key=lambda item: len(item.parts))
    if len(chrome_candidates) != 1:
        raise WindowsRuntimeError(
            f"Runtime tree must contain exactly one chrome.exe, found {len(chrome_candidates)}"
        )
    chrome = chrome_candidates[0]
    validate_pe32(chrome)
    marker = "chromium-windows-i686-runtime-smoke"
    with tempfile.TemporaryDirectory(prefix="chromium-win32-smoke-") as user_data:
        command = [
            str(chrome),
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            f"--user-data-dir={user_data}",
            "--dump-dom",
            f"data:text/html,<title>{marker}</title><p>{marker}</p>",
        ]
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            output, _ = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_windows_tree(proc)
            proc.wait(timeout=30)
            raise WindowsRuntimeError(
                f"Chromium headless smoke exceeded {timeout_seconds} seconds"
            ) from exc
    if proc.returncode != 0:
        raise WindowsRuntimeError(
            f"Chromium headless smoke failed with exit {proc.returncode}: {output[-4000:]}"
        )
    if marker not in output:
        raise WindowsRuntimeError(
            "Chromium headless smoke returned success without rendering the local marker"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _command_validate_zip(args: argparse.Namespace) -> None:
    max_members = _env_limit(
        "CHROMIUM_WINDOWS_I686_MAX_RELEASE_MEMBERS",
        DEFAULT_MAX_MEMBERS,
        HARD_MAX_MEMBERS,
    )
    max_gib = _env_limit(
        "CHROMIUM_WINDOWS_I686_MAX_RELEASE_UNPACKED_GIB",
        DEFAULT_MAX_UNPACKED_GIB,
        HARD_MAX_UNPACKED_GIB,
    )
    stats = validate_release_zip(
        args.archive,
        args.version,
        max_members=max_members,
        max_unpacked_bytes=max_gib * 1024**3,
    )
    print(
        f"Windows i686 release ZIP validated: members={stats['member_count']}; "
        f"unpacked_bytes={stats['unpacked_bytes']}; pe32={stats['pe32_count']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    pe = subparsers.add_parser("validate-pe")
    pe.add_argument("path", type=Path)

    validate_zip = subparsers.add_parser("validate-zip")
    validate_zip.add_argument("archive", type=Path)
    validate_zip.add_argument("--version", required=True)

    extract_zip = subparsers.add_parser("extract-zip")
    extract_zip.add_argument("archive", type=Path)
    extract_zip.add_argument("destination", type=Path)
    extract_zip.add_argument("--version", required=True)

    validate_7z = subparsers.add_parser("validate-7z")
    validate_7z.add_argument("archive", type=Path)

    extract_7z = subparsers.add_parser("extract-7z")
    extract_7z.add_argument("archive", type=Path)
    extract_7z.add_argument("destination", type=Path)

    validate_tree = subparsers.add_parser("validate-tree")
    validate_tree.add_argument("root", type=Path)

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("root", type=Path)
    smoke.add_argument("--timeout-seconds", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-pe":
        validate_pe32(args.path)
        print(f"PE32 Intel i386 validated: {args.path}")
    elif args.command == "validate-zip":
        _command_validate_zip(args)
    elif args.command == "extract-zip":
        root = extract_release_zip(args.archive, args.version, args.destination)
        print(root)
    elif args.command == "validate-7z":
        stats = list_7z_runtime(args.archive)
        print(
            f"Runtime 7z validated: members={stats['member_count']}; "
            f"unpacked_bytes={stats['unpacked_bytes']}"
        )
    elif args.command == "extract-7z":
        stats = extract_7z_runtime(args.archive, args.destination)
        print(
            f"Runtime 7z extracted: members={stats['member_count']}; "
            f"unpacked_bytes={stats['unpacked_bytes']}"
        )
    elif args.command == "validate-tree":
        stats = validate_runtime_tree(args.root)
        print(
            f"Windows runtime tree validated: files={stats['file_count']}; "
            f"bytes={stats['unpacked_bytes']}; pe32={stats['pe32_count']}"
        )
    elif args.command == "smoke":
        validate_runtime_tree(args.root)
        smoke_test_runtime(args.root, timeout_seconds=args.timeout_seconds)
        print("Windows i686 Chromium headless runtime smoke passed")
    else:  # pragma: no cover - argparse owns command choices.
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WindowsRuntimeError, OSError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
