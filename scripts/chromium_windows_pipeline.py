#!/usr/bin/env python3
"""Hardened, resumable Chromium Windows i686 build primitives.

The GitHub workflows intentionally keep control-plane policy in YAML while this
module owns platform-specific source, toolchain, checkpoint, build, packaging,
and runtime contracts. Every trust-bearing input is exact and fail-closed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from chromium_source_object import (
    marker_matches,
    source_download_url,
    validate_effective_https_host,
    validate_source_metadata,
    verify_file,
    write_marker,
)
from chromium_source_object import fetch_metadata as fetch_source_metadata
from chromium_tool_pins import resolve_pins
from chromium_windows_runtime import (
    WindowsRuntimeError,
    extract_7z_runtime,
    release_root,
    sha256_file,
    smoke_test_runtime,
    validate_release_zip,
    validate_runtime_tree,
)
from ninja_stall_watchdog import (
    STALL_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    WatchdogError,
    run_with_watchdog,
)
from validate_checkpoint_archive import validate_checkpoint
from validate_chromium_source_archive import validate_source_archive

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
OUT_NAME = "Release_x86_win"
CHECKPOINT_CONTRACT_VERSION = 1
CHECKPOINT_MANIFEST_SCHEMA = 1
PREPARED_STATE_SCHEMA = 1
PORT_CONFIG_HASH_SCHEMA = 1
MAX_NO_PROGRESS_STREAK = 2
DEFAULT_MIN_WORK_GIB = 70
DEFAULT_SOURCE_RESERVE_GIB = 25
DEFAULT_CHECKPOINT_RESERVE_GIB = 12
DEFAULT_SOURCE_MAX_MEMBERS = 2_000_000
DEFAULT_SOURCE_MAX_UNPACKED_GIB = 80
DEFAULT_CHECKPOINT_MAX_UNPACKED_GIB = 80
DEFAULT_NETWORK_TIMEOUT_SECONDS = 7200
DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS = 3600
DEFAULT_ARCHIVE_TIMEOUT_SECONDS = 1800
DEFAULT_REMOVE_TIMEOUT_SECONDS = 900
GITILES_HOST = "chromium.googlesource.com"
SOURCE_DOWNLOAD_HOST = "commondatastorage.googleapis.com"
TRUSTED_BUILD_WORKFLOW = ".github/workflows/chromium-windows-i686.yml"
BUILD_TITLE_RE = re.compile(
    r"^Chromium Windows i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) "
    r"- stage ([1-9][0-9]*) - attempt ([0-9]+)$"
)

WINDOWS_GN_ARGS = """\
target_os="win"
target_cpu="x86"
is_debug=false
is_component_build=false
is_official_build=false
symbol_level=0
blink_symbol_level=0
v8_symbol_level=0
use_remoteexec=false
use_siso=false
treat_warnings_as_errors=false
"""

CRITICAL_WINDOWS_SOURCE_FILES = (
    "DEPS",
    "chrome/VERSION",
    "BUILD.gn",
    "build/vs_toolchain.py",
    "build/toolchain/win/BUILD.gn",
    "chrome/installer/mini_installer/BUILD.gn",
    "docs/windows_build_instructions.md",
)

INFRASTRUCTURE_PATTERNS = re.compile(
    r"No space left on device|not enough space on the disk|disk full|"
    r"Input/output error|Temporary failure in name resolution|Could not resolve host|"
    r"Connection reset|TLS handshake|network is unreachable|timed? out|"
    r"Cannot allocate memory|out of memory|Killed process|paging file is too small|"
    r"The system cannot find the path specified.*runner|HTTP (?:429|5[0-9]{2})",
    re.IGNORECASE,
)
RUNTIME_ENVIRONMENT_PATTERNS = re.compile(
    r"The code execution cannot proceed because .*\.dll was not found|"
    r"The specified module could not be found|error while loading shared libraries|"
    r"CreateProcess failed.*(?:2|126)|STATUS_DLL_NOT_FOUND|0xc0000135",
    re.IGNORECASE,
)


class WindowsPipelineError(RuntimeError):
    """A deterministic Windows pipeline contract failure."""


class InfrastructureError(WindowsPipelineError):
    """A fresh hosted runner or retry may recover this failure."""


@dataclass(frozen=True)
class WindowsRequirements:
    sdk_family: str
    sdk_min_servicing: str
    visual_studio_year: str
    visual_studio_min_version: str


@dataclass(frozen=True)
class PreparedState:
    schema: int
    version: str
    source_sha256: str
    depot_tools_revision: str
    gn_version: str
    ninja_package: str
    ninja_version: str
    clang_revision: str
    sdk_family: str
    sdk_servicing: str
    visual_studio_year: str
    visual_studio_version: str
    port_config_hash_schema: int
    port_config_sha256: str
    checkpoint_no_progress_streak: int


def validate_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise WindowsPipelineError(f"Invalid Chromium version: {version!r}")
    return version


def validate_sha1(value: str, label: str) -> str:
    normalized = value.lower()
    if not SHA1_RE.fullmatch(normalized):
        raise WindowsPipelineError(f"{label} must be exactly 40 hexadecimal characters")
    return normalized


def validate_sha256(value: str, label: str) -> str:
    normalized = value.lower()
    if not SHA256_RE.fullmatch(normalized):
        raise WindowsPipelineError(f"{label} must be exactly 64 hexadecimal characters")
    return normalized


def bounded_int(
    value: str | int,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(value)
    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", raw):
        raise WindowsPipelineError(f"{label} must be an integer")
    parsed = int(raw)
    if not minimum <= parsed <= maximum:
        raise WindowsPipelineError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 600,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not command:
        raise WindowsPipelineError("Refusing to run an empty command")
    print("+ " + subprocess.list2cmdline(list(command)), flush=True)
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError(f"Command could not complete: {command[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
        error_type = InfrastructureError if INFRASTRUCTURE_PATTERNS.search(detail) else WindowsPipelineError
        raise error_type(
            f"Command failed with exit {result.returncode}: {subprocess.list2cmdline(list(command))}"
            + (f"\n{detail[-8000:]}" if detail else "")
        )
    return result


def _append_github_env(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_ENV", "")
    if not path:
        return
    if "\n" in value or "\r" in value:
        raise WindowsPipelineError(f"Refusing multiline GitHub environment value for {name}")
    with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{name}={value}\n")


def _write_github_output(values: Mapping[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise WindowsPipelineError(f"Refusing multiline GitHub output for {name}")
            handle.write(f"{name}={value}\n")


def _drive_root(path: Path) -> Path:
    anchor = path.resolve().anchor
    if not anchor:
        raise WindowsPipelineError(f"Path has no drive anchor: {path}")
    return Path(anchor)


def select_work_root(*, minimum_free_gib: int = DEFAULT_MIN_WORK_GIB) -> Path:
    minimum_free_gib = bounded_int(
        minimum_free_gib, "minimum_free_gib", minimum=20, maximum=500
    )
    candidates: dict[str, Path] = {}
    for raw in (
        os.environ.get("RUNNER_TEMP", ""),
        os.environ.get("GITHUB_WORKSPACE", ""),
        os.environ.get("SystemDrive", ""),
    ):
        if not raw:
            continue
        try:
            root = _drive_root(Path(raw))
            candidates[str(root).lower()] = root
        except (OSError, WindowsPipelineError):
            continue
    if os.name == "nt":
        for letter in "CDEFG":
            root = Path(f"{letter}:\\")
            if root.exists():
                candidates[str(root).lower()] = root
    if not candidates:
        raise InfrastructureError("No fixed workspace drive candidates are available")

    ranked: list[tuple[int, Path]] = []
    for root in candidates.values():
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        ranked.append((free, root))
        print(f"Runner drive {root}: {free / 1024**3:.1f} GiB free")
    if not ranked:
        raise InfrastructureError("Could not inspect free space on any workspace drive")
    free, drive = max(ranked, key=lambda item: item[0])
    if free < minimum_free_gib * 1024**3:
        raise InfrastructureError(
            f"Largest runner drive has {free / 1024**3:.1f} GiB free; "
            f"Windows Chromium requires at least {minimum_free_gib} GiB before preparation"
        )
    root = drive / "cw32"
    marker = root / ".chromium-windows-i686-root"
    if root.exists() and not marker.is_file():
        raise InfrastructureError(
            f"Refusing to reuse unmarked short build root on hosted runner: {root}"
        )
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(
            f"run_id={os.environ.get('GITHUB_RUN_ID', 'local')}\n",
            encoding="utf-8",
        )
    _append_github_env("CHROMIUM_WINDOWS_ROOT", str(root))
    _write_github_output({"work_root": str(root)})
    print(f"Selected short Windows Chromium build root: {root}")
    return root


def validate_work_root(work_root: Path) -> Path:
    try:
        resolved = work_root.resolve(strict=True)
    except OSError as exc:
        raise InfrastructureError(f"Windows work root is unavailable: {work_root}: {exc}") from exc
    marker = resolved / ".chromium-windows-i686-root"
    if resolved.name.lower() != "cw32" or not marker.is_file() or marker.is_symlink():
        raise WindowsPipelineError(
            f"Refusing state mutation outside the marked short Windows build root: {resolved}"
        )
    return resolved


def ensure_descendant(path: Path, parent: Path, label: str) -> Path:
    resolved_parent = parent.resolve()
    resolved = path.resolve()
    if resolved != resolved_parent and resolved_parent not in resolved.parents:
        raise WindowsPipelineError(f"{label} escapes trusted repository workspace: {resolved}")
    return resolved


def parse_windows_requirements(
    vs_toolchain_text: str, windows_docs_text: str
) -> WindowsRequirements:
    sdk_match = re.search(
        r"^SDK_VERSION\s*=\s*['\"]([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)['\"]",
        vs_toolchain_text,
        re.MULTILINE,
    )
    versions_match = re.search(
        r"MSVS_VERSIONS\s*=.*?\(\s*['\"]([0-9]{4})['\"]\s*,\s*"
        r"['\"]([0-9]+\.[0-9]+)['\"]\s*\)",
        vs_toolchain_text,
        re.DOTALL,
    )
    if not sdk_match or not versions_match:
        raise WindowsPipelineError(
            "Chromium's Windows toolchain declaration changed; could not resolve SDK/Visual Studio requirements"
        )
    sdk_family = sdk_match.group(1)
    family_prefix = ".".join(sdk_family.split(".")[:3]) + "."
    documented = re.findall(
        r"version\s+(10\.0\.[0-9]+\.[0-9]+)", windows_docs_text, re.IGNORECASE
    )
    family_versions = [value for value in documented if value.startswith(family_prefix)]
    sdk_min_servicing = family_versions[0] if family_versions else sdk_family
    requirements = WindowsRequirements(
        sdk_family=sdk_family,
        sdk_min_servicing=sdk_min_servicing,
        visual_studio_year=versions_match.group(1),
        visual_studio_min_version=versions_match.group(2),
    )
    for value in (requirements.sdk_family, requirements.sdk_min_servicing):
        if not re.fullmatch(r"10\.0\.[0-9]{4,6}\.[0-9]+", value):
            raise WindowsPipelineError(f"Unsupported Windows SDK version contract: {value!r}")
    return requirements


def verify_windows_x86_source_contract(source: Path) -> WindowsRequirements:
    root_build = (source / "BUILD.gn").read_text(encoding="utf-8")
    toolchain = (source / "build/toolchain/win/BUILD.gn").read_text(encoding="utf-8")
    vs_toolchain = (source / "build/vs_toolchain.py").read_text(encoding="utf-8")
    docs = (source / "docs/windows_build_instructions.md").read_text(encoding="utf-8")
    guard_start = root_build.find("is_valid_x86_target")
    guard_end = root_build.find("group(", guard_start)
    if guard_start < 0 or guard_end < 0:
        raise WindowsPipelineError("Chromium's root x86 target guard changed upstream")
    guard = root_build[guard_start:guard_end]
    if 'target_cpu != "x86"' not in guard:
        raise WindowsPipelineError("Chromium's x86 target assertion changed upstream")
    if re.search(r'target_os\s*!=\s*"win"', guard):
        raise WindowsPipelineError("Upstream Chromium no longer declares Windows x86 valid")
    if 'if (target_cpu == "x86" || target_cpu == "x64")' not in toolchain:
        raise WindowsPipelineError("Chromium's Windows x86 toolchain declaration changed")
    if 'win_toolchains("x86")' not in toolchain or 'toolchain_arch = "x86"' not in toolchain:
        raise WindowsPipelineError("Chromium's Windows x86 toolchain is unavailable")
    return parse_windows_requirements(vs_toolchain, docs)


def _source_stats_usable(
    path: Path,
    *,
    version: str,
    source_sha256: str,
    max_members: int,
    max_unpacked_gib: int,
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    member_count = payload.get("member_count")
    unpacked_bytes = payload.get("unpacked_bytes")
    return (
        payload.get("version") == version
        and payload.get("source_sha256") == source_sha256
        and isinstance(member_count, int)
        and not isinstance(member_count, bool)
        and 0 < member_count <= max_members
        and isinstance(unpacked_bytes, int)
        and not isinstance(unpacked_bytes, bool)
        and 0 <= unpacked_bytes <= max_unpacked_gib * 1024**3
    )


def _write_source_stats(
    path: Path, *, version: str, source_sha256: str, stats: Mapping[str, int]
) -> None:
    payload = {"version": version, "source_sha256": source_sha256, **stats}
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _fetch_gitiles_bytes(version: str, relative: str) -> bytes:
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in relative.split("/"))
    url = (
        f"https://{GITILES_HOST}/chromium/src/+show/refs/tags/{version}/{quoted}?format=TEXT"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "chromium-windows-i686/1"})
    last_error: BaseException | None = None
    encoded = b""
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                effective = response.geturl()
                validate_effective_https_host(effective, GITILES_HOST)
                encoded = response.read(64 * 1024 * 1024 + 1)
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise WindowsPipelineError(
                    f"Authoritative Chromium tag {version} lacks critical file {relative}"
                ) from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        except ValueError:
            # A redirect outside the trusted Gitiles host is deterministic and
            # must never be retried against the untrusted endpoint.
            raise
        if attempt < 5:
            delay = min(2 ** (attempt - 1), 8)
            print(
                f"::warning::Gitiles proof fetch for {relative} failed on attempt "
                f"{attempt}/5 ({last_error}); retrying in {delay}s"
            )
            time.sleep(delay)
    if last_error is not None:
        raise InfrastructureError(
            f"Could not fetch authoritative Chromium {version} {relative} after bounded retries: "
            f"{last_error}"
        ) from last_error
    if len(encoded) > 64 * 1024 * 1024:
        raise WindowsPipelineError(f"Gitiles proof unexpectedly exceeds 64 MiB: {relative}")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise WindowsPipelineError(f"Gitiles returned invalid base64 for {relative}") from exc


def validate_critical_source_identity(source: Path, version: str) -> None:
    for relative in CRITICAL_WINDOWS_SOURCE_FILES:
        local_path = source.joinpath(*PurePosixPath(relative).parts)
        if not local_path.is_file() or local_path.is_symlink():
            raise WindowsPipelineError(f"Extracted source lacks critical file: {relative}")
        local = local_path.read_bytes()
        upstream = _fetch_gitiles_bytes(version, relative)
        local_sha = hashlib.sha256(local).hexdigest()
        upstream_sha = hashlib.sha256(upstream).hexdigest()
        print(f"Critical source identity {relative}: local={local_sha} upstream={upstream_sha}")
        if local_sha != upstream_sha:
            raise WindowsPipelineError(
                f"Source archive {relative} does not match authoritative tag {version}"
            )


def _download_source_object(
    version: str,
    partial: Path,
    metadata: Mapping[str, object],
    *,
    timeout_seconds: int,
) -> None:
    expected_size = int(metadata["content_length"])
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise InfrastructureError("curl is unavailable for resumable Chromium source download")
    command = [
        curl,
        "--fail",
        "--location",
        "--retry",
        "6",
        "--retry-all-errors",
        "--retry-delay",
        "10",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--connect-timeout",
        "30",
        "--max-time",
        str(timeout_seconds),
        "--continue-at",
        "-",
        "--output",
        str(partial),
        "--write-out",
        "%{url_effective}",
        source_download_url(version),
    ]
    result = _run(command, timeout=timeout_seconds + 120, capture=True)
    effective = (result.stdout or "").strip().splitlines()[-1]
    validate_effective_https_host(effective, SOURCE_DOWNLOAD_HOST)


def _ensure_extract_capacity(stats_path: Path, target: Path, reserve_gib: int) -> None:
    payload = json.loads(stats_path.read_text(encoding="utf-8"))
    unpacked = payload.get("unpacked_bytes")
    if not isinstance(unpacked, int) or isinstance(unpacked, bool) or unpacked < 0:
        raise WindowsPipelineError("Source archive stats lack a valid unpacked byte count")
    target.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target.parent).free
    required = unpacked + reserve_gib * 1024**3
    print(
        f"Source extraction requires {required} bytes including {reserve_gib} GiB reserve; "
        f"{free} bytes are free"
    )
    if free < required:
        raise InfrastructureError("Insufficient disk space for bounded Chromium source extraction")


def _validate_extracted_version(source: Path, expected: str) -> None:
    fields: dict[str, str] = {}
    version_file = source / "chrome/VERSION"
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    actual = ".".join(fields.get(key, "") for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))
    if actual != expected:
        raise WindowsPipelineError(
            f"Extracted Chromium version {actual!r} does not match requested {expected!r}"
        )


def prepare_source(version: str, *, work_root: Path, cache_dir: Path) -> tuple[Path, str]:
    version = validate_version(version)
    work_root = validate_work_root(work_root)
    source = work_root / "src"
    if source.exists():
        raise InfrastructureError(f"Short build root already contains a source tree: {source}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / f"chromium-{version}.tar.xz"
    partial = cache_dir / f"chromium-{version}.tar.xz.partial"
    marker = cache_dir / f"chromium-{version}.validated.json"
    metadata_path = cache_dir / f"chromium-{version}.source-object.json"
    stats_path = cache_dir / f"chromium-{version}.source-archive-stats.json"
    for cache_path in (tarball, partial, marker, metadata_path, stats_path):
        if cache_path.is_symlink():
            raise WindowsPipelineError(
                f"Source cache contains a forbidden symbolic link: {cache_path.name}"
            )
        if cache_path.exists() and not cache_path.is_file():
            raise WindowsPipelineError(
                f"Source cache path is not a regular file: {cache_path.name}"
            )
    metadata = validate_source_metadata(version, fetch_source_metadata(version, timeout=120))
    trusted_marker = False
    verified: dict[str, str] | None = None

    if tarball.is_file():
        try:
            verified = verify_file(tarball, metadata)
        except (OSError, ValueError) as exc:
            print(f"::warning::Discarding source cache bytes that failed GCS identity: {exc}")
            for path in (tarball, marker, metadata_path, stats_path):
                path.unlink(missing_ok=True)
    if not tarball.is_file():
        print(f"Downloading authoritative Chromium {version} source object with resume support")
        _download_source_object(
            version,
            partial,
            metadata,
            timeout_seconds=DEFAULT_NETWORK_TIMEOUT_SECONDS,
        )
        verified = verify_file(partial, metadata)
        os.replace(partial, tarball)
    if verified is None:
        verified = verify_file(tarball, metadata)
    source_sha = validate_sha256(verified["sha256"], "source SHA-256")
    metadata_payload = {**metadata, **verified}
    metadata_path.write_text(json.dumps(metadata_payload, sort_keys=True) + "\n", encoding="utf-8")

    if marker_matches(
        marker, version=version, metadata=metadata_payload, sha256=source_sha
    ) and _source_stats_usable(
        stats_path,
        version=version,
        source_sha256=source_sha,
        max_members=DEFAULT_SOURCE_MAX_MEMBERS,
        max_unpacked_gib=DEFAULT_SOURCE_MAX_UNPACKED_GIB,
    ):
        trusted_marker = True
        print("Reusing exact GCS-generation source safety and Gitiles identity proof")
    else:
        stats = validate_source_archive(
            tarball,
            version,
            max_members=DEFAULT_SOURCE_MAX_MEMBERS,
            max_unpacked_bytes=DEFAULT_SOURCE_MAX_UNPACKED_GIB * 1024**3,
        )
        _write_source_stats(
            stats_path, version=version, source_sha256=source_sha, stats=stats
        )

    _ensure_extract_capacity(stats_path, source, DEFAULT_SOURCE_RESERVE_GIB)
    # Extract directly into the already validated, fresh short root. Chromium's
    # Windows build is still sensitive to MAX_PATH in third-party generators;
    # an atomic staging directory would add dozens of avoidable path characters.
    # A failed extraction cannot be reused because every job receives a new VM.
    source.mkdir(parents=True)
    tar = shutil.which("tar.exe") or shutil.which("tar")
    if not tar:
        raise InfrastructureError("bsdtar is unavailable for Chromium source extraction")
    _run(
        [
            tar,
            "-xJf",
            str(tarball),
            "-C",
            str(source),
            "--strip-components=1",
        ],
        timeout=DEFAULT_ARCHIVE_TIMEOUT_SECONDS,
    )

    _validate_extracted_version(source, version)
    if not trusted_marker:
        validate_critical_source_identity(source, version)
        write_marker(
            marker,
            version=version,
            metadata=metadata_payload,
            sha256=source_sha,
            safe_archive=True,
            gitiles_identity=True,
        )
    resolve_pins(source / "DEPS")
    (cache_dir / f"chromium-{version}.tar.xz.sha256").write_text(
        f"{source_sha}  chromium-{version}.tar.xz\n", encoding="utf-8"
    )
    print(f"Prepared Chromium {version} source with SHA-256 {source_sha}")
    return source, source_sha


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"([0-9]+(?:\.[0-9]+){1,3})", value)
    if not match:
        raise WindowsPipelineError(f"Could not parse numeric version from {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _find_vswhere() -> Path:
    located = shutil.which("vswhere.exe") or shutil.which("vswhere")
    candidates = [Path(located)] if located else []
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    if program_files_x86:
        candidates.append(
            Path(program_files_x86)
            / "Microsoft Visual Studio/Installer/vswhere.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise InfrastructureError("vswhere is unavailable on the selected Windows runner")


def resolve_visual_studio(requirements: WindowsRequirements) -> tuple[Path, str]:
    if os.name != "nt":
        raise InfrastructureError("Visual Studio resolution requires a Windows runner")
    vswhere = _find_vswhere()
    result = _run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Workload.NativeDesktop",
            "Microsoft.VisualStudio.Component.VC.ATLMFC",
            "-format",
            "json",
            "-utf8",
        ],
        capture=True,
        timeout=120,
    )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise InfrastructureError("vswhere returned malformed JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise WindowsPipelineError(
            "Runner must expose exactly one latest Visual Studio installation with NativeDesktop and ATLMFC"
        )
    installation = payload[0]
    path = Path(str(installation.get("installationPath", "")))
    version = str(installation.get("installationVersion", ""))
    if not path.is_dir() or not version:
        raise InfrastructureError("vswhere returned an incomplete Visual Studio installation")
    if _version_tuple(version) < _version_tuple(requirements.visual_studio_min_version):
        raise WindowsPipelineError(
            f"Visual Studio {version} is older than Chromium's minimum "
            f"{requirements.visual_studio_min_version}"
        )
    expected_major = int(requirements.visual_studio_min_version.split(".", 1)[0])
    if _version_tuple(version)[0] != expected_major:
        raise WindowsPipelineError(
            f"Hosted runner selected Visual Studio {version}, but Chromium's preferred "
            f"toolchain is {requirements.visual_studio_year} ({expected_major}.x)"
        )
    tools = sorted((path / "VC/Tools/MSVC").glob("*/bin/Hostx64/x86/cl.exe"))
    if not tools:
        raise WindowsPipelineError("Visual Studio lacks the x64-hosted x86 MSVC tools")
    atlmfc = sorted((path / "VC/Tools/MSVC").glob("*/atlmfc/include/atlbase.h"))
    if not atlmfc:
        raise WindowsPipelineError("Visual Studio lacks required x86/x64 ATL/MFC headers")
    print(f"Validated Visual Studio {version} at {path}")
    return path, version


def _windows_kits_root() -> Path:
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    if not program_files_x86:
        raise InfrastructureError("ProgramFiles(x86) is unavailable on Windows runner")
    return Path(program_files_x86) / "Windows Kits/10"


def _sdk_probe_binary(kits: Path, sdk_family: str) -> Path | None:
    for relative in (
        f"bin/{sdk_family}/x64/makeappx.exe",
        f"bin/{sdk_family}/x64/rc.exe",
        f"bin/{sdk_family}/x86/rc.exe",
    ):
        candidate = kits / relative
        if candidate.is_file():
            return candidate
    return None


def _file_product_version(path: Path) -> str:
    pwsh = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not pwsh:
        raise InfrastructureError("PowerShell is unavailable for Windows SDK version proof")
    literal = str(path).replace("'", "''")
    result = _run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Item -LiteralPath '{literal}').VersionInfo.ProductVersion",
        ],
        capture=True,
        timeout=60,
    )
    return (result.stdout or "").strip()


def _sdk_layout_is_complete(kits: Path, sdk_family: str) -> bool:
    required = (
        kits / f"Include/{sdk_family}/um/windows.h",
        kits / f"Include/{sdk_family}/shared/sdkddkver.h",
        kits / f"Include/{sdk_family}/ucrt/corecrt.h",
        kits / f"Lib/{sdk_family}/um/x86/kernel32.lib",
        kits / f"Lib/{sdk_family}/ucrt/x86/ucrt.lib",
        kits / "Debuggers/x86/dbghelp.dll",
    )
    return all(path.is_file() for path in required) and _sdk_probe_binary(kits, sdk_family) is not None


def _install_sdk_with_winget(requirements: WindowsRequirements) -> None:
    winget = shutil.which("winget.exe") or shutil.which("winget")
    if not winget:
        raise InfrastructureError(
            f"Windows SDK {requirements.sdk_family} is absent and winget is unavailable"
        )
    parts = requirements.sdk_family.split(".")
    if len(parts) != 4 or parts[-1] != "0":
        raise WindowsPipelineError(
            f"Cannot map Chromium SDK family to official winget package: {requirements.sdk_family}"
        )
    package_id = "Microsoft.WindowsSDK." + ".".join(parts[:3])
    _run(
        [
            winget,
            "show",
            "--exact",
            "--id",
            package_id,
            "--source",
            "winget",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        timeout=300,
    )
    _run(
        [
            winget,
            "install",
            "--exact",
            "--id",
            package_id,
            "--source",
            "winget",
            "--override",
            "/features OptionId.DesktopCPPx86 OptionId.DesktopCPPx64 OptionId.WindowsDesktopDebuggers /quiet /norestart",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )


def ensure_windows_sdk(requirements: WindowsRequirements) -> tuple[Path, str]:
    if os.name != "nt":
        raise InfrastructureError("Windows SDK preparation requires a Windows runner")
    kits = _windows_kits_root()
    if not _sdk_layout_is_complete(kits, requirements.sdk_family):
        system_free = shutil.disk_usage(kits.anchor or kits.parent).free
        if system_free < 8 * 1024**3:
            raise InfrastructureError(
                f"Only {system_free} bytes are free on the SDK installation drive; "
                "at least 8 GiB is required before installing a source-declared SDK family"
            )
        print(
            f"Chromium requires Windows SDK {requirements.sdk_family}; "
            "installing its official Microsoft winget package"
        )
        _install_sdk_with_winget(requirements)
    if not _sdk_layout_is_complete(kits, requirements.sdk_family):
        raise WindowsPipelineError(
            f"Windows SDK installation completed without Chromium's required "
            f"{requirements.sdk_family} x86 headers, libraries, tools, and debugging runtime"
        )
    probe = _sdk_probe_binary(kits, requirements.sdk_family)
    assert probe is not None
    servicing = _file_product_version(probe)
    if _version_tuple(servicing) < _version_tuple(requirements.sdk_min_servicing):
        raise WindowsPipelineError(
            f"Installed Windows SDK servicing version {servicing!r} is older than "
            f"Chromium's documented minimum {requirements.sdk_min_servicing}"
        )
    print(
        f"Validated Windows SDK family {requirements.sdk_family}, servicing {servicing}, "
        f"including x86 libraries and Debugging Tools"
    )
    return kits, servicing


def _depot_environment(
    depot_tools: Path,
    requirements: WindowsRequirements,
    visual_studio: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["DEPOT_TOOLS_UPDATE"] = "0"
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    env["GYP_MSVS_VERSION"] = requirements.visual_studio_year
    env[f"vs{requirements.visual_studio_year}_install"] = str(visual_studio)
    env["GYP_MSVS_OVERRIDE_PATH"] = str(visual_studio)
    env["PATH"] = os.pathsep.join(
        (str(depot_tools), str(depot_tools / ".cipd_bin"), env.get("PATH", ""))
    )
    return env


def install_depot_tools(source: Path, work_root: Path) -> tuple[Path, dict[str, str]]:
    pins = resolve_pins(source / "DEPS")
    revision = pins["depot_tools_revision"]
    depot = work_root / "depot_tools"
    if depot.exists():
        try:
            current = _run(
                ["git", "-C", str(depot), "rev-parse", "HEAD"],
                capture=True,
                timeout=60,
            ).stdout.strip()
        except WindowsPipelineError:
            current = ""
        if current != revision:
            raise InfrastructureError(
                f"Existing marked work root contains unexpected depot_tools revision {current!r}"
            )
    else:
        depot.mkdir(parents=True)
        _run(["git", "-C", str(depot), "init", "-q"], timeout=60)
        _run(
            [
                "git",
                "-C",
                str(depot),
                "remote",
                "add",
                "origin",
                "https://chromium.googlesource.com/chromium/tools/depot_tools.git",
            ],
            timeout=60,
        )
        _run(["git", "-C", str(depot), "config", "core.longpaths", "true"], timeout=60)
        _run(
            ["git", "-C", str(depot), "fetch", "--depth=1", "origin", revision],
            timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
        )
        _run(
            ["git", "-C", str(depot), "checkout", "-q", "--detach", "FETCH_HEAD"],
            timeout=120,
        )
    checked = _run(
        ["git", "-C", str(depot), "rev-parse", "HEAD"], capture=True, timeout=60
    ).stdout.strip()
    if checked != revision:
        raise WindowsPipelineError(
            f"Pinned depot_tools checkout mismatch: expected {revision}, got {checked}"
        )
    bootstrap = depot / "bootstrap/win_tools.bat"
    if not bootstrap.is_file():
        raise WindowsPipelineError("Pinned depot_tools lacks bootstrap/win_tools.bat")
    bootstrap_env = os.environ.copy()
    bootstrap_env["DEPOT_TOOLS_UPDATE"] = "0"
    _run(
        ["cmd.exe", "/d", "/s", "/c", f'call "{bootstrap}"'],
        env=bootstrap_env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    _run(
        ["cmd.exe", "/d", "/s", "/c", f'call "{depot / "cipd.bat"}" version'],
        env=bootstrap_env,
        timeout=300,
    )
    _run(
        ["cmd.exe", "/d", "/s", "/c", f'call "{depot / "gclient.bat"}" --version'],
        env={**bootstrap_env, "PATH": str(depot) + os.pathsep + bootstrap_env.get("PATH", "")},
        timeout=300,
    )
    marker = depot / "python3_bin_reldir.txt"
    if not marker.is_file():
        raise WindowsPipelineError("Pinned depot_tools bootstrap omitted python3_bin_reldir.txt")
    python_rel = marker.read_text(encoding="utf-8").strip().replace("\\", "/")
    if not python_rel or python_rel.startswith("/") or ".." in PurePosixPath(python_rel).parts:
        raise WindowsPipelineError(f"Pinned depot_tools wrote unsafe Python path {python_rel!r}")
    if not (depot / Path(*PurePosixPath(python_rel).parts) / "python3.exe").is_file():
        raise WindowsPipelineError("Pinned depot_tools Python bootstrap is incomplete")
    return depot, pins


def _depot_python(depot_tools: Path) -> Path:
    marker = depot_tools / "python3_bin_reldir.txt"
    relative = marker.read_text(encoding="utf-8").strip().replace("\\", "/")
    return depot_tools / Path(*PurePosixPath(relative).parts) / "python3.exe"


def install_source_declared_tools(
    source: Path,
    work_root: Path,
    depot_tools: Path,
    pins: Mapping[str, str],
    env: Mapping[str, str],
) -> tuple[Path, Path, str]:
    cipd = depot_tools / "cipd.bat"
    gn_root = work_root / "gn"
    gn_root.mkdir(exist_ok=True)
    _run(
        [
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            f'call "{cipd}" install gn/gn/windows-amd64 {pins["gn_version"]} -root "{gn_root}" -log-level warning',
        ],
        env=env,
        timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
    )
    gn = gn_root / "gn.exe"
    if not gn.is_file():
        raise WindowsPipelineError("Chromium-pinned GN CIPD install omitted gn.exe")
    _run([str(gn), "--version"], env=env, timeout=60)

    ninja_root = source / "third_party/ninja"
    ninja_root.mkdir(parents=True, exist_ok=True)
    ninja_package = pins["ninja_package"] + "windows-amd64"
    _run(
        [
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            f'call "{cipd}" install {ninja_package} {pins["ninja_version"]} -root "{ninja_root}" -log-level warning',
        ],
        env=env,
        timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
    )
    ninja = ninja_root / "ninja.exe"
    if not ninja.is_file():
        raise WindowsPipelineError("Chromium-pinned Ninja CIPD install omitted ninja.exe")
    _run([str(ninja), "--version"], env=env, timeout=60)

    depot_python = _depot_python(depot_tools)
    _run(
        [str(depot_python), str(source / "tools/clang/scripts/update.py")],
        cwd=source,
        env=env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    clang = source / "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe"
    clang_revision_file = source / "third_party/llvm-build/Release+Asserts/cr_build_revision"
    if not clang.is_file() or not clang_revision_file.is_file():
        raise WindowsPipelineError("Chromium Clang update omitted clang-cl.exe or revision proof")
    clang_revision = clang_revision_file.read_text(encoding="utf-8").strip()
    if not clang_revision or "\n" in clang_revision:
        raise WindowsPipelineError("Chromium Clang revision proof is malformed")
    _run([str(clang), "--version"], env=env, timeout=60)

    rc_sha = source / "build/toolchain/win/rc/win/rc.exe.sha1"
    rc_exe = rc_sha.with_suffix("")
    if rc_sha.is_file():
        _run(
            [
                str(depot_python),
                str(depot_tools / "download_from_google_storage.py"),
                "--no_resume",
                "--bucket",
                "chromium-browser-clang/rc",
                "-s",
                str(rc_sha),
            ],
            cwd=source,
            env=env,
            timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
        )
        if not rc_exe.is_file():
            raise WindowsPipelineError("Chromium-pinned Windows rc download omitted rc.exe")

    util = source / "build/util"
    util.mkdir(parents=True, exist_ok=True)
    (util / "LASTCHANGE").write_text(
        "LASTCHANGE=0000000000000000000000000000000000000000-refs/heads/main@{#0}\n",
        encoding="utf-8",
    )
    (util / "LASTCHANGE.committime").write_text("0\n", encoding="utf-8")
    return gn, ninja, clang_revision


PORT_CONFIG_FILES = (
    "scripts/chromium_windows_pipeline.py",
    "scripts/chromium_windows_runtime.py",
    "scripts/ninja_stall_watchdog.py",
    "scripts/validate_checkpoint_archive.py",
    ".github/actions/chromium-windows-i686-stage/action.yml",
)


def compute_port_config_sha256(repository_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"chromium-windows-i686-port-config-v1\0")
    digest.update(WINDOWS_GN_ARGS.encode("utf-8"))
    for relative in PORT_CONFIG_FILES:
        path = repository_root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise WindowsPipelineError(f"Port configuration file is missing: {relative}")
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def configure_gn(
    source: Path,
    gn: Path,
    env: Mapping[str, str],
    *,
    evidence_dir: Path | None = None,
) -> Path:
    out = source / "out" / OUT_NAME
    out.mkdir(parents=True, exist_ok=True)
    _run(
        [str(gn), "gen", str(out), f"--args={WINDOWS_GN_ARGS}"],
        cwd=source,
        env=env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    build_ninja = out / "build.ninja"
    args_gn = out / "args.gn"
    if not build_ninja.is_file() or not args_gn.is_file():
        raise WindowsPipelineError("GN succeeded without build.ninja and args.gn")
    rendered = args_gn.read_text(encoding="utf-8")
    for pattern, label in (
        (r'target_os\s*=\s*"win"', "target_os=win"),
        (r'target_cpu\s*=\s*"x86"', "target_cpu=x86"),
        (r"is_debug\s*=\s*false", "release mode"),
        (r"is_component_build\s*=\s*false", "non-component runtime"),
    ):
        if not re.search(pattern, rendered):
            raise WindowsPipelineError(f"GN args.gn omitted required {label} contract")
    queries: dict[str, str] = {}
    for label in ("//chrome", "//chrome/installer/mini_installer:mini_installer"):
        result = _run(
            [str(gn), "desc", str(out), label],
            cwd=source,
            env=env,
            timeout=600,
            capture=True,
        )
        if not (result.stdout or "").strip():
            raise WindowsPipelineError(f"GN graph lacks required target {label}")
        queries[label] = result.stdout
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args_gn, evidence_dir / "args.gn")
        (evidence_dir / "gn-targets.json").write_text(
            json.dumps(queries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return out


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsPipelineError(f"Could not read {label} JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WindowsPipelineError(f"{label} must be a JSON object")
    return payload


def _state_path(work_root: Path) -> Path:
    return work_root / "prepared-state.json"


def write_prepared_state(work_root: Path, state: PreparedState) -> None:
    path = _state_path(work_root)
    temporary = path.with_suffix(".json.partial")
    temporary.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_prepared_state(work_root: Path) -> PreparedState:
    payload = _read_json_object(_state_path(work_root), "prepared build state")
    try:
        state = PreparedState(**payload)
    except TypeError as exc:
        raise WindowsPipelineError("Prepared build state fields changed or are incomplete") from exc
    if state.schema != PREPARED_STATE_SCHEMA:
        raise WindowsPipelineError(f"Unsupported prepared state schema: {state.schema}")
    validate_version(state.version)
    validate_sha256(state.source_sha256, "prepared source SHA-256")
    validate_sha256(state.port_config_sha256, "prepared port configuration SHA-256")
    bounded_int(
        state.checkpoint_no_progress_streak,
        "checkpoint no-progress streak",
        minimum=0,
        maximum=MAX_NO_PROGRESS_STREAK,
    )
    return state


def _gh_json(args: Sequence[str], *, timeout: int = 120) -> object:
    if not os.environ.get("GH_TOKEN"):
        raise InfrastructureError("GH_TOKEN is required for checkpoint provenance validation")
    result = _run(["gh", *args], timeout=timeout, capture=True)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise InfrastructureError("GitHub CLI returned malformed JSON") from exc


def verify_checkpoint_run(
    *,
    repository: str,
    run_id: str,
    version: str,
    expected_stage: int,
    expected_ref: str,
    expected_sha: str,
    artifact_name: str,
) -> dict[str, object]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise WindowsPipelineError(f"Invalid repository: {repository!r}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise WindowsPipelineError(f"Invalid checkpoint run ID: {run_id!r}")
    validate_version(version)
    expected_stage = bounded_int(expected_stage, "expected_stage", minimum=1, maximum=50)
    if not BRANCH_RE.fullmatch(expected_ref) or ".." in expected_ref:
        raise WindowsPipelineError(f"Invalid expected checkpoint ref: {expected_ref!r}")
    expected_sha = validate_sha1(expected_sha, "checkpoint lineage SHA")
    expected_artifact = f"chromium-windows-i686-out-stage-{expected_stage}"
    if artifact_name != expected_artifact:
        raise WindowsPipelineError(
            f"Checkpoint artifact name {artifact_name!r} does not match {expected_artifact!r}"
        )
    payload = _gh_json(["api", f"repos/{repository}/actions/runs/{run_id}"])
    if not isinstance(payload, dict):
        raise InfrastructureError("GitHub checkpoint run response is not an object")
    workflow_path = str(payload.get("path", "")).split("@", 1)[0]
    head_repository = payload.get("head_repository")
    head_repository_name = (
        str(head_repository.get("full_name", ""))
        if isinstance(head_repository, dict)
        else ""
    )
    title = str(payload.get("display_title", ""))
    title_match = BUILD_TITLE_RE.fullmatch(title)
    checks = {
        "workflow path": workflow_path == TRUSTED_BUILD_WORKFLOW,
        "head repository": head_repository_name == repository,
        "head branch": str(payload.get("head_branch", "")) == expected_ref,
        "head SHA": str(payload.get("head_sha", "")).lower() == expected_sha,
        "event": str(payload.get("event", "")) == "workflow_dispatch",
        "terminal status": str(payload.get("status", "")) == "completed",
        "title": title_match is not None
        and title_match.group(1) == version
        and int(title_match.group(2)) == expected_stage,
    }
    failed = [label for label, accepted in checks.items() if not accepted]
    if failed:
        raise WindowsPipelineError(
            f"Checkpoint run {run_id} failed trusted provenance checks: {', '.join(failed)}"
        )
    conclusion = str(payload.get("conclusion", ""))
    if conclusion not in {"success", "failure", "cancelled", "timed_out"}:
        raise WindowsPipelineError(
            f"Checkpoint run {run_id} has unsupported conclusion {conclusion!r}"
        )
    encoded_name = urllib.parse.quote(artifact_name, safe="")
    artifacts_payload = _gh_json(
        [
            "api",
            f"repos/{repository}/actions/runs/{run_id}/artifacts?name={encoded_name}&per_page=100",
        ]
    )
    if not isinstance(artifacts_payload, dict):
        raise InfrastructureError("GitHub artifact response is not an object")
    artifacts = artifacts_payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise InfrastructureError("GitHub artifact response lacks artifacts list")
    exact = [
        item
        for item in artifacts
        if isinstance(item, dict) and item.get("name") == artifact_name
    ]
    if int(artifacts_payload.get("total_count", -1)) != 1 or len(exact) != 1:
        raise WindowsPipelineError(
            f"Checkpoint run {run_id} must expose exactly one {artifact_name!r} artifact"
        )
    artifact = exact[0]
    if artifact.get("expired") is not False or not isinstance(artifact.get("size_in_bytes"), int):
        raise WindowsPipelineError(f"Checkpoint artifact {artifact_name!r} is expired or malformed")
    if int(artifact["size_in_bytes"]) <= 0:
        raise WindowsPipelineError(f"Checkpoint artifact {artifact_name!r} is empty")
    return {
        "run_id": run_id,
        "run_attempt": int(payload.get("run_attempt", 0) or 0),
        "producer_stage": expected_stage,
        "producer_sha": expected_sha,
        "artifact_name": artifact_name,
        "artifact_id": int(artifact.get("id", 0) or 0),
        "conclusion": conclusion,
    }


def verify_completed_build_run(
    *,
    repository: str,
    run_id: str,
    version: str,
    expected_ref: str,
    expected_sha: str,
) -> dict[str, object]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise WindowsPipelineError(f"Invalid repository: {repository!r}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise WindowsPipelineError(f"Invalid build run ID: {run_id!r}")
    validate_version(version)
    if not BRANCH_RE.fullmatch(expected_ref) or ".." in expected_ref:
        raise WindowsPipelineError(f"Invalid build ref: {expected_ref!r}")
    expected_sha = validate_sha1(expected_sha, "expected build SHA")
    payload = _gh_json(["api", f"repos/{repository}/actions/runs/{run_id}"])
    if not isinstance(payload, dict):
        raise InfrastructureError("GitHub build run response is not an object")
    title = str(payload.get("display_title", ""))
    match = BUILD_TITLE_RE.fullmatch(title)
    head_repository = payload.get("head_repository")
    head_repository_name = (
        str(head_repository.get("full_name", ""))
        if isinstance(head_repository, dict)
        else ""
    )
    checks = {
        "workflow path": str(payload.get("path", "")).split("@", 1)[0]
        == TRUSTED_BUILD_WORKFLOW,
        "head repository": head_repository_name == repository,
        "head branch": str(payload.get("head_branch", "")) == expected_ref,
        "head SHA": str(payload.get("head_sha", "")).lower() == expected_sha,
        "workflow_dispatch event": str(payload.get("event", "")) == "workflow_dispatch",
        "successful terminal state": str(payload.get("status", "")) == "completed"
        and str(payload.get("conclusion", "")) == "success",
        "versioned title": match is not None and match.group(1) == version,
    }
    failed = [label for label, accepted in checks.items() if not accepted]
    if failed:
        raise WindowsPipelineError(
            f"Build run {run_id} failed trusted release provenance: {', '.join(failed)}"
        )
    assert match is not None
    stage = int(match.group(2))
    artifact_name = f"chromium-{version}-windows-i686"
    encoded_name = urllib.parse.quote(artifact_name, safe="")
    artifacts_payload = _gh_json(
        [
            "api",
            f"repos/{repository}/actions/runs/{run_id}/artifacts?name={encoded_name}&per_page=100",
        ]
    )
    if not isinstance(artifacts_payload, dict):
        raise InfrastructureError("GitHub build artifact response is not an object")
    artifacts = artifacts_payload.get("artifacts")
    exact = (
        [item for item in artifacts if isinstance(item, dict) and item.get("name") == artifact_name]
        if isinstance(artifacts, list)
        else []
    )
    if artifacts_payload.get("total_count") != 1 or len(exact) != 1:
        raise WindowsPipelineError(
            f"Build run {run_id} must expose exactly one final artifact {artifact_name!r}"
        )
    artifact = exact[0]
    if artifact.get("expired") is not False or not isinstance(artifact.get("size_in_bytes"), int):
        raise WindowsPipelineError("Final Windows build artifact is expired or malformed")
    if int(artifact["size_in_bytes"]) <= 0:
        raise WindowsPipelineError("Final Windows build artifact is empty")
    return {
        "run_id": run_id,
        "head_sha": expected_sha,
        "stage": stage,
        "artifact_name": artifact_name,
        "artifact_id": int(artifact.get("id", 0) or 0),
    }


def _parse_release_manifest(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise WindowsPipelineError("Release manifest is absent, linked, empty, or over 256 KiB")
    fields: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        if not line:
            break
        if "=" not in line:
            raise WindowsPipelineError(f"Malformed release manifest line: {line!r}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in fields:
            raise WindowsPipelineError(f"Invalid/duplicate release manifest key: {key!r}")
        if not value:
            raise WindowsPipelineError(f"Release manifest field is empty: {key}")
        fields[key] = value
    return fields


def validate_release_bundle(
    directory: Path,
    *,
    version: str,
    expected_run_id: str,
    expected_sha: str,
) -> dict[str, object]:
    version = validate_version(version)
    if not RUN_ID_RE.fullmatch(expected_run_id):
        raise WindowsPipelineError("Expected release run ID is malformed")
    expected_sha = validate_sha1(expected_sha, "expected release SHA")
    package = directory / f"chromium-{version}-windows-i686.zip"
    checksum = directory / f"chromium-{version}-windows-i686.zip.sha256"
    manifest_path = directory / f"chromium-{version}-windows-i686-manifest.txt"
    expected_names = {package.name, checksum.name, manifest_path.name}
    actual_names = {
        path.name for path in directory.iterdir() if path.is_file() or path.is_symlink()
    }
    if actual_names != expected_names:
        raise WindowsPipelineError(
            "Final artifact file set differs from the exact release contract: "
            f"expected={sorted(expected_names)}, actual={sorted(actual_names)}"
        )
    for path in (package, checksum, manifest_path):
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise WindowsPipelineError(f"Release sidecar is not a non-empty regular file: {path.name}")
    package_sha = sha256_file(package)
    if checksum.read_text(encoding="utf-8") != f"{package_sha}  {package.name}\n":
        raise WindowsPipelineError("Release checksum sidecar is malformed or inconsistent")
    fields = _parse_release_manifest(manifest_path)
    exact = {
        "manifest_schema": "1",
        "version": version,
        "target_cpu": "x86",
        "target_os": "win",
        "source_tarball": source_download_url(version),
        "package_sha256": package_sha,
        "github_sha": expected_sha,
        "github_run_id": expected_run_id,
        "port_config_hash_schema": str(PORT_CONFIG_HASH_SCHEMA),
        "checkpoint_contract_version": str(CHECKPOINT_CONTRACT_VERSION),
    }
    mismatches = [key for key, expected in exact.items() if fields.get(key) != expected]
    if mismatches:
        raise WindowsPipelineError(
            "Release manifest failed exact provenance fields: " + ", ".join(mismatches)
        )
    for key in (
        "source_tar_sha256",
        "port_config_sha256",
    ):
        validate_sha256(fields.get(key, ""), f"manifest {key}")
    if not re.fullmatch(r"git_revision:[0-9a-f]{40}", fields.get("gn_version", "")):
        raise WindowsPipelineError("Release manifest GN pin is absent or mutable")
    if not re.fullmatch(r"[0-9a-f]{40}", fields.get("depot_tools_revision", "")):
        raise WindowsPipelineError("Release manifest depot_tools pin is absent or mutable")
    if fields.get("ninja_package") != "infra/3pp/tools/ninja/" or not re.fullmatch(
        r"version:[1-9][0-9]*@[A-Za-z0-9._+-]+", fields.get("ninja_version", "")
    ):
        raise WindowsPipelineError("Release manifest Ninja CIPD pin is absent or mutable")
    if not re.fullmatch(r"10\.0\.[0-9]{4,6}\.0", fields.get("windows_sdk_family", "")):
        raise WindowsPipelineError("Release manifest Windows SDK family is malformed")
    stats = validate_release_zip(package, version)
    return {
        "package": str(package),
        "checksum": str(checksum),
        "manifest": str(manifest_path),
        "package_sha256": package_sha,
        **stats,
    }


def _checkpoint_expected_files(directory: Path) -> tuple[Path, Path, Path]:
    archive = directory / f"out-{OUT_NAME}.tar.zst"
    checksum = directory / f"out-{OUT_NAME}.tar.zst.sha256"
    manifest = directory / "checkpoint-manifest.json"
    return archive, checksum, manifest


def _checkpoint_manifest_matches_state(
    manifest: Mapping[str, object],
    state: PreparedState,
    proof: Mapping[str, object],
) -> int:
    exact = {
        "schema": CHECKPOINT_MANIFEST_SCHEMA,
        "checkpoint_contract_version": CHECKPOINT_CONTRACT_VERSION,
        "target_os": "win",
        "target_cpu": "x86",
        "output_root": OUT_NAME,
        "version": state.version,
        "source_sha256": state.source_sha256,
        "depot_tools_revision": state.depot_tools_revision,
        "gn_version": state.gn_version,
        "ninja_package": state.ninja_package,
        "ninja_version": state.ninja_version,
        "clang_revision": state.clang_revision,
        "sdk_family": state.sdk_family,
        "visual_studio_year": state.visual_studio_year,
        "port_config_hash_schema": state.port_config_hash_schema,
        "port_config_sha256": state.port_config_sha256,
        "github_sha": proof["producer_sha"],
        "github_run_id": proof["run_id"],
        "github_run_attempt": proof["run_attempt"],
        "stage": proof["producer_stage"],
    }
    mismatches = [
        key for key, expected in exact.items() if manifest.get(key) != expected
    ]
    if mismatches:
        raise WindowsPipelineError(
            "Checkpoint manifest is incompatible with current source/toolchain/lineage: "
            + ", ".join(mismatches)
        )
    streak = bounded_int(
        manifest.get("no_progress_streak", -1),
        "checkpoint no-progress streak",
        minimum=0,
        maximum=MAX_NO_PROGRESS_STREAK,
    )
    return streak


def validate_checkpoint_bundle(
    directory: Path,
    *,
    state: PreparedState,
    proof: Mapping[str, object],
) -> tuple[Path, int]:
    archive, checksum, manifest_path = _checkpoint_expected_files(directory)
    for path in (archive, checksum, manifest_path):
        if not path.is_file() or path.is_symlink():
            raise WindowsPipelineError(f"Checkpoint artifact lacks regular file: {path.name}")
    manifest = _read_json_object(manifest_path, "checkpoint manifest")
    streak = _checkpoint_manifest_matches_state(manifest, state, proof)
    archive_sha = sha256_file(archive)
    expected_sha = validate_sha256(str(manifest.get("archive_sha256", "")), "checkpoint archive SHA-256")
    if archive_sha != expected_sha:
        raise WindowsPipelineError(
            f"Checkpoint archive SHA-256 mismatch: expected {expected_sha}, got {archive_sha}"
        )
    expected_bytes = manifest.get("archive_bytes")
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes <= 0:
        raise WindowsPipelineError("Checkpoint manifest archive_bytes is invalid")
    if archive.stat().st_size != expected_bytes:
        raise WindowsPipelineError("Checkpoint archive byte length does not match manifest")
    expected_sidecar = f"{archive_sha}  {archive.name}\n"
    if checksum.read_text(encoding="utf-8") != expected_sidecar:
        raise WindowsPipelineError("Checkpoint checksum sidecar is malformed or inconsistent")
    validate_checkpoint(archive, root=OUT_NAME)
    return archive, streak


def acquire_checkpoint(
    *,
    repository: str,
    version: str,
    stage: int,
    expected_ref: str,
    expected_sha: str,
    state: PreparedState,
    destination: Path,
    preferred_run_id: str = "",
    fallback_run_id: str = "",
) -> tuple[Path | None, int]:
    destination.mkdir(parents=True, exist_ok=True)
    candidates: list[tuple[str, int, str]] = []
    if preferred_run_id:
        candidates.append((preferred_run_id, stage, "preferred"))
    if fallback_run_id and stage > 1:
        candidates.append((fallback_run_id, stage - 1, "fallback"))
    for run_id, producer_stage, label in candidates:
        artifact_name = f"chromium-windows-i686-out-stage-{producer_stage}"
        candidate_dir = destination / label
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)
        candidate_dir.mkdir()
        try:
            proof = verify_checkpoint_run(
                repository=repository,
                run_id=run_id,
                version=version,
                expected_stage=producer_stage,
                expected_ref=expected_ref,
                expected_sha=expected_sha,
                artifact_name=artifact_name,
            )
            _run(
                [
                    "gh",
                    "run",
                    "download",
                    run_id,
                    "--repo",
                    repository,
                    "--name",
                    artifact_name,
                    "--dir",
                    str(candidate_dir),
                ],
                timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
            )
            archive, streak = validate_checkpoint_bundle(
                candidate_dir, state=state, proof=proof
            )
        except InfrastructureError:
            raise
        except (OSError, WindowsPipelineError, ValueError) as exc:
            print(f"::warning::Rejected {label} checkpoint from run {run_id}: {exc}")
            shutil.rmtree(candidate_dir, ignore_errors=True)
            continue
        print(
            f"Accepted {label} checkpoint from run {run_id}, stage {producer_stage}, "
            f"no-progress streak {streak}"
        )
        return archive, streak
    return None, 0


def restore_checkpoint(archive: Path, *, source: Path) -> None:
    stats = validate_checkpoint(archive, root=OUT_NAME)
    out_parent = source / "out"
    out_parent.mkdir(parents=True, exist_ok=True)
    required = stats["unpacked_bytes"] + DEFAULT_CHECKPOINT_RESERVE_GIB * 1024**3
    free = shutil.disk_usage(out_parent).free
    if free < required:
        raise InfrastructureError(
            f"Checkpoint restore requires {required} bytes including reserve; only {free} are free"
        )
    staging = out_parent / f".checkpoint-restore-{uuid.uuid4().hex}"
    staging.mkdir()
    target = out_parent / OUT_NAME
    backup = out_parent / f".{OUT_NAME}-before-restore-{uuid.uuid4().hex}"
    tar = shutil.which("tar.exe") or shutil.which("tar")
    if not tar:
        raise InfrastructureError("bsdtar is unavailable for checkpoint restoration")
    try:
        _run(
            [tar, "-xaf", str(archive), "-C", str(staging)],
            timeout=DEFAULT_ARCHIVE_TIMEOUT_SECONDS,
        )
        staged = staging / OUT_NAME
        if not (staged / "build.ninja").is_file() or not (staged / "args.gn").is_file():
            raise WindowsPipelineError("Restored checkpoint lacks build.ninja or args.gn")
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except Exception:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"Atomically restored Windows Ninja output checkpoint to {target}")


def create_checkpoint(
    *,
    work_root: Path,
    repository_root: Path,
    destination: Path,
    stage: int,
    no_progress_streak: int,
) -> tuple[Path, Path, Path]:
    work_root = validate_work_root(work_root)
    destination = ensure_descendant(destination, repository_root, "checkpoint destination")
    state = read_prepared_state(work_root)
    current_port_hash = compute_port_config_sha256(repository_root)
    if current_port_hash != state.port_config_sha256:
        raise WindowsPipelineError(
            "Port configuration changed after preparation; refusing a mixed-lineage checkpoint"
        )
    stage = bounded_int(stage, "stage", minimum=1, maximum=50)
    no_progress_streak = bounded_int(
        no_progress_streak,
        "no_progress_streak",
        minimum=0,
        maximum=MAX_NO_PROGRESS_STREAK,
    )
    out = work_root / "src/out" / OUT_NAME
    if not (out / "build.ninja").is_file() or not (out / "args.gn").is_file():
        raise WindowsPipelineError("Cannot checkpoint an output tree without Ninja/GN state")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"out-{OUT_NAME}.tar.zst"
    partial = destination / f".{archive.name}.{uuid.uuid4().hex}.partial"
    checksum = destination / f"out-{OUT_NAME}.tar.zst.sha256"
    manifest_path = destination / "checkpoint-manifest.json"
    for path in (archive, partial, checksum, manifest_path):
        path.unlink(missing_ok=True)
    logical_bytes = 0
    file_count = 0
    for root, _directories, files in os.walk(out, followlinks=False):
        for name in files:
            path = Path(root) / name
            if path.is_symlink():
                continue
            logical_bytes += path.stat().st_size
            file_count += 1
            if file_count > 4_000_000:
                raise WindowsPipelineError(
                    "Output tree exceeds hard 4,000,000-file checkpoint contract"
                )
    estimated_archive_reserve = min(
        40 * 1024**3,
        logical_bytes // 2 + 2 * 1024**3,
    )
    required_free = max(
        DEFAULT_CHECKPOINT_RESERVE_GIB * 1024**3, estimated_archive_reserve
    )
    free = shutil.disk_usage(destination).free
    if free < required_free:
        raise InfrastructureError(
            f"Only {free} bytes are free before checkpoint creation; bounded reserve is "
            f"{required_free} bytes for {logical_bytes} logical output bytes"
        )
    tar = shutil.which("tar.exe") or shutil.which("tar")
    if not tar:
        raise InfrastructureError("bsdtar is unavailable for checkpoint creation")
    _run(
        [
            tar,
            "--zstd",
            "-cf",
            str(partial),
            "--format=pax",
            "-C",
            str(out.parent),
            OUT_NAME,
        ],
        timeout=DEFAULT_ARCHIVE_TIMEOUT_SECONDS,
    )
    os.replace(partial, archive)
    validate_checkpoint(archive, root=OUT_NAME)
    archive_sha = sha256_file(archive)
    checksum.write_text(f"{archive_sha}  {archive.name}\n", encoding="utf-8")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_attempt = bounded_int(
        os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "GITHUB_RUN_ATTEMPT",
        minimum=1,
        maximum=1000,
    )
    github_sha = validate_sha1(os.environ.get("GITHUB_SHA", ""), "GITHUB_SHA")
    if not RUN_ID_RE.fullmatch(run_id):
        raise WindowsPipelineError("GITHUB_RUN_ID is missing or malformed")
    manifest = {
        "schema": CHECKPOINT_MANIFEST_SCHEMA,
        "checkpoint_contract_version": CHECKPOINT_CONTRACT_VERSION,
        "target_os": "win",
        "target_cpu": "x86",
        "output_root": OUT_NAME,
        "version": state.version,
        "source_sha256": state.source_sha256,
        "depot_tools_revision": state.depot_tools_revision,
        "gn_version": state.gn_version,
        "ninja_package": state.ninja_package,
        "ninja_version": state.ninja_version,
        "clang_revision": state.clang_revision,
        "sdk_family": state.sdk_family,
        "sdk_servicing": state.sdk_servicing,
        "visual_studio_year": state.visual_studio_year,
        "visual_studio_version": state.visual_studio_version,
        "port_config_hash_schema": state.port_config_hash_schema,
        "port_config_sha256": state.port_config_sha256,
        "github_repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "github_ref_name": os.environ.get("GITHUB_REF_NAME", ""),
        "github_sha": github_sha,
        "github_run_id": run_id,
        "github_run_attempt": run_attempt,
        "stage": stage,
        "no_progress_streak": no_progress_streak,
        "archive_sha256": archive_sha,
        "archive_bytes": archive.stat().st_size,
        "runner_image": os.environ.get("ImageOS", "unknown"),
        "runner_image_version": os.environ.get("ImageVersion", "unknown"),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"Created checkpoint {archive.name}: {archive.stat().st_size} bytes, SHA-256 {archive_sha}"
    )
    return archive, checksum, manifest_path


def prepare_pipeline(
    *,
    version: str,
    work_root: Path,
    cache_dir: Path,
    repository_root: Path,
    stage: int,
    repository: str = "",
    expected_ref: str = "",
    expected_sha: str = "",
    preferred_run_id: str = "",
    fallback_run_id: str = "",
    evidence_dir: Path | None = None,
) -> PreparedState:
    version = validate_version(version)
    work_root = validate_work_root(work_root)
    ensure_descendant(cache_dir, repository_root, "source cache directory")
    stage = bounded_int(stage, "stage", minimum=1, maximum=50)
    if expected_sha:
        expected_sha = validate_sha1(expected_sha, "expected lineage SHA")
        current_sha = os.environ.get("GITHUB_SHA", "").lower()
        if current_sha and current_sha != expected_sha:
            raise WindowsPipelineError(
                f"Workflow lineage drift: expected {expected_sha}, current {current_sha}"
            )
    source, source_sha = prepare_source(
        version, work_root=work_root, cache_dir=cache_dir
    )
    requirements = verify_windows_x86_source_contract(source)
    visual_studio, visual_studio_version = resolve_visual_studio(requirements)
    _kits, sdk_servicing = ensure_windows_sdk(requirements)
    depot_tools, pins = install_depot_tools(source, work_root)
    env = _depot_environment(depot_tools, requirements, visual_studio)
    depot_python = _depot_python(depot_tools)
    # This configures Chromium's source-pinned SDK/VS metadata without attempting
    # the Google-internal toolchain download (DEPOT_TOOLS_WIN_TOOLCHAIN=0).
    _run(
        [str(depot_python), str(source / "build/vs_toolchain.py"), "update", "--force"],
        cwd=source,
        env=env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    gn, ninja, clang_revision = install_source_declared_tools(
        source, work_root, depot_tools, pins, env
    )
    port_hash = compute_port_config_sha256(repository_root)
    state = PreparedState(
        schema=PREPARED_STATE_SCHEMA,
        version=version,
        source_sha256=source_sha,
        depot_tools_revision=pins["depot_tools_revision"],
        gn_version=pins["gn_version"],
        ninja_package=pins["ninja_package"],
        ninja_version=pins["ninja_version"],
        clang_revision=clang_revision,
        sdk_family=requirements.sdk_family,
        sdk_servicing=sdk_servicing,
        visual_studio_year=requirements.visual_studio_year,
        visual_studio_version=visual_studio_version,
        port_config_hash_schema=PORT_CONFIG_HASH_SCHEMA,
        port_config_sha256=port_hash,
        checkpoint_no_progress_streak=0,
    )
    write_prepared_state(work_root, state)

    if preferred_run_id or fallback_run_id:
        if not (repository and expected_ref and expected_sha):
            raise WindowsPipelineError(
                "Checkpoint inputs require repository, expected ref, and immutable lineage SHA"
            )
        archive, streak = acquire_checkpoint(
            repository=repository,
            version=version,
            stage=stage,
            expected_ref=expected_ref,
            expected_sha=expected_sha,
            state=state,
            destination=work_root / "resume",
            preferred_run_id=preferred_run_id,
            fallback_run_id=fallback_run_id,
        )
        if archive is not None:
            restore_checkpoint(archive, source=source)
            state = PreparedState(
                **{
                    **asdict(state),
                    "checkpoint_no_progress_streak": streak,
                }
            )
            write_prepared_state(work_root, state)

    out = configure_gn(source, gn, env, evidence_dir=evidence_dir)
    if evidence_dir is not None:
        (evidence_dir / "requirements.json").write_text(
            json.dumps(asdict(requirements), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "prepared-state.json").write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    exports = {
        "CHROMIUM_WINDOWS_ROOT": str(work_root),
        "CHROMIUM_WINDOWS_SRC": str(source),
        "CHROMIUM_WINDOWS_OUT": str(out),
        "CHROMIUM_WINDOWS_DEPOT_TOOLS": str(depot_tools),
        "CHROMIUM_WINDOWS_GN": str(gn),
        "CHROMIUM_WINDOWS_NINJA": str(ninja),
        "DEPOT_TOOLS_UPDATE": "0",
        "DEPOT_TOOLS_WIN_TOOLCHAIN": "0",
        "GYP_MSVS_VERSION": requirements.visual_studio_year,
        "GYP_MSVS_OVERRIDE_PATH": str(visual_studio),
        f"vs{requirements.visual_studio_year}_install": str(visual_studio),
    }
    for name, value in exports.items():
        _append_github_env(name, value)
    _write_github_output(
        {
            "source_sha256": source_sha,
            "checkpoint_no_progress_streak": str(
                state.checkpoint_no_progress_streak
            ),
            "sdk_family": requirements.sdk_family,
            "visual_studio_year": requirements.visual_studio_year,
        }
    )
    print(
        f"Prepared Chromium {version} Windows i686 graph at {out}; "
        f"SDK {requirements.sdk_family}; VS {visual_studio_version}"
    )
    return state


def _ninja_log_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for line in handle if line.strip() and not line.startswith("#"))
    except FileNotFoundError:
        return 0


def classify_build_log(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-4_000_000:]
    except OSError:
        return "infrastructure"
    if RUNTIME_ENVIRONMENT_PATTERNS.search(text):
        return "runtime_environment"
    if INFRASTRUCTURE_PATTERNS.search(text):
        return "infrastructure"
    return "deterministic_build"


def run_build_slice(
    *,
    work_root: Path,
    result_file: Path,
    job_started_at: int,
    checkpoint_minutes: int,
    stall_minutes: int,
    jobs: int,
) -> dict[str, object]:
    work_root = validate_work_root(work_root)
    state = read_prepared_state(work_root)
    checkpoint_minutes = bounded_int(
        checkpoint_minutes,
        "checkpoint_minutes",
        minimum=30,
        maximum=325,
    )
    stall_minutes = bounded_int(
        stall_minutes, "stall_minutes", minimum=30, maximum=180
    )
    jobs = bounded_int(jobs, "jobs", minimum=1, maximum=8)
    now = int(time.time())
    if job_started_at < now - 12 * 60 * 60 or job_started_at > now + 300:
        raise WindowsPipelineError("job_started_at is outside the bounded current job window")
    cutoff = job_started_at + checkpoint_minutes * 60
    remaining = cutoff - now
    source = work_root / "src"
    out = source / "out" / OUT_NAME
    ninja = Path(os.environ.get("CHROMIUM_WINDOWS_NINJA", str(source / "third_party/ninja/ninja.exe")))
    if not ninja.is_file():
        raise InfrastructureError(f"Prepared Ninja executable is unavailable: {ninja}")
    log = work_root / "windows-i686-build.log"
    progress_log = out / ".ninja_log"
    before = _ninja_log_count(progress_log)
    prior_streak = state.checkpoint_no_progress_streak
    result: dict[str, object] = {
        "complete": False,
        "failure_class": "",
        "no_progress_streak": prior_streak,
        "ninja_entries_before": before,
        "ninja_entries_after": before,
        "status": 0,
    }
    if remaining <= 600:
        print("::warning::Preparation consumed the compiler budget; checkpointing without starting Ninja")
        result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        return result
    free = shutil.disk_usage(out).free
    if free < 10 * 1024**3:
        raise InfrastructureError(
            f"Only {free} bytes are free before Windows Chromium compilation"
        )
    env = os.environ.copy()
    depot = Path(os.environ.get("CHROMIUM_WINDOWS_DEPOT_TOOLS", str(work_root / "depot_tools")))
    env["DEPOT_TOOLS_UPDATE"] = "0"
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    env["PATH"] = os.pathsep.join(
        (str(depot), str(depot / ".cipd_bin"), env.get("PATH", ""))
    )
    command = [
        str(ninja),
        "-C",
        str(out),
        f"-j{jobs}",
        "chrome",
        "mini_installer",
    ]
    stall_marker = work_root / "ninja-stall.marker"
    try:
        status = run_with_watchdog(
            command,
            progress_log=progress_log,
            stall_seconds=stall_minutes * 60,
            poll_seconds=15,
            kill_grace_seconds=30,
            stall_marker=stall_marker,
            timeout_seconds=remaining,
            timeout_kill_grace_seconds=120,
            output_log=log,
            cwd=source,
            env=env,
        )
    except WatchdogError as exc:
        raise InfrastructureError(f"Ninja watchdog failed internally: {exc}") from exc
    after = _ninja_log_count(progress_log)
    result["status"] = status
    result["ninja_entries_after"] = after
    if status == 0:
        required = (
            out / "chrome.exe",
            out / "mini_installer.exe",
            out / "chrome.7z",
        )
        missing = [str(path) for path in required if not path.is_file() or path.stat().st_size <= 0]
        if missing:
            result["failure_class"] = "deterministic_build"
            result["no_progress_streak"] = 0 if after > before else min(prior_streak + 1, MAX_NO_PROGRESS_STREAK)
            result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            raise WindowsPipelineError(
                "Ninja returned success without required Windows package outputs: "
                + ", ".join(missing)
            )
        result["complete"] = True
        result["no_progress_streak"] = 0
    elif status in {TIMEOUT_EXIT_CODE, STALL_EXIT_CODE}:
        streak = 0 if after > before else min(prior_streak + 1, MAX_NO_PROGRESS_STREAK)
        result["no_progress_streak"] = streak
        if streak >= MAX_NO_PROGRESS_STREAK:
            result["failure_class"] = "deterministic_build"
            result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            raise WindowsPipelineError(
                "Two consecutive Windows compiler slices made no durable Ninja progress"
            )
        print(
            f"Compiler slice rotated at status {status}; Ninja entries {before}->{after}; "
            f"no-progress streak {streak}/{MAX_NO_PROGRESS_STREAK}"
        )
    else:
        result["failure_class"] = classify_build_log(log)
        result["no_progress_streak"] = 0 if after > before else min(prior_streak + 1, MAX_NO_PROGRESS_STREAK)
        result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        raise WindowsPipelineError(
            f"Windows Chromium Ninja failed with status {status} ({result['failure_class']})"
        )
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    _write_github_output(
        {
            "complete": str(result["complete"]).lower(),
            "failure_class": str(result["failure_class"]),
            "no_progress_streak": str(result["no_progress_streak"]),
        }
    )
    return result


def _zip_member_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(
            info.filename.rstrip("/")
            for info in archive.infolist()
            if not info.is_dir()
        )


def package_build(
    *,
    version: str,
    work_root: Path,
    repository_root: Path,
    destination: Path,
    smoke_timeout_seconds: int = 120,
) -> tuple[Path, Path, Path]:
    version = validate_version(version)
    work_root = validate_work_root(work_root)
    destination = ensure_descendant(destination, repository_root, "release destination")
    state = read_prepared_state(work_root)
    if state.version != version:
        raise WindowsPipelineError("Prepared state version does not match package version")
    if compute_port_config_sha256(repository_root) != state.port_config_sha256:
        raise WindowsPipelineError("Port configuration changed after compilation")
    out = work_root / "src/out" / OUT_NAME
    mini_installer = out / "mini_installer.exe"
    chrome_archive = out / "chrome.7z"
    if not mini_installer.is_file() or not chrome_archive.is_file():
        raise WindowsPipelineError("Completed output lacks mini_installer.exe or chrome.7z")
    staging = work_root / "release-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    bundle = staging / release_root(version)
    bundle.mkdir()
    extract_7z_runtime(chrome_archive, bundle)
    shutil.copy2(mini_installer, bundle / "mini_installer.exe")
    runtime_stats = validate_runtime_tree(bundle)
    smoke_test_runtime(bundle, timeout_seconds=smoke_timeout_seconds)

    destination.mkdir(parents=True, exist_ok=True)
    package = destination / f"chromium-{version}-windows-i686.zip"
    partial = destination / f".{package.name}.{uuid.uuid4().hex}.partial"
    checksum = destination / f"{package.name}.sha256"
    manifest = destination / f"chromium-{version}-windows-i686-manifest.txt"
    for path in (package, partial, checksum, manifest):
        path.unlink(missing_ok=True)
    seven_zip = shutil.which("7z.exe") or shutil.which("7z")
    if not seven_zip:
        raise InfrastructureError("7z is unavailable for Windows runtime packaging")
    _run(
        [seven_zip, "a", "-tzip", "-mx=7", str(partial), bundle.name],
        cwd=staging,
        timeout=DEFAULT_ARCHIVE_TIMEOUT_SECONDS,
    )
    os.replace(partial, package)
    if package.stat().st_size > 16 * 1024**3:
        raise WindowsPipelineError("Windows release ZIP exceeds hard 16 GiB compressed limit")
    validate_release_zip(package, version)
    package_sha = sha256_file(package)
    checksum.write_text(f"{package_sha}  {package.name}\n", encoding="utf-8")
    github_sha = validate_sha1(os.environ.get("GITHUB_SHA", ""), "GITHUB_SHA")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not RUN_ID_RE.fullmatch(run_id):
        raise WindowsPipelineError("GITHUB_RUN_ID is missing or malformed during packaging")
    packaged_files = _zip_member_names(package)
    fields = (
        ("manifest_schema", "1"),
        ("version", version),
        ("target_cpu", "x86"),
        ("target_os", "win"),
        ("source_tarball", source_download_url(version)),
        ("source_tar_sha256", state.source_sha256),
        ("package_sha256", package_sha),
        ("github_sha", github_sha),
        ("github_run_id", run_id),
        ("clang_revision", state.clang_revision),
        ("gn_version", state.gn_version),
        ("ninja_package", state.ninja_package),
        ("ninja_version", state.ninja_version),
        ("depot_tools_revision", state.depot_tools_revision),
        ("windows_sdk_family", state.sdk_family),
        ("windows_sdk_servicing", state.sdk_servicing),
        ("visual_studio_year", state.visual_studio_year),
        ("visual_studio_version", state.visual_studio_version),
        ("port_config_hash_schema", str(state.port_config_hash_schema)),
        ("port_config_sha256", state.port_config_sha256),
        ("checkpoint_contract_version", str(CHECKPOINT_CONTRACT_VERSION)),
        ("runner_os", os.environ.get("RUNNER_OS", "unknown")),
        ("runner_image", os.environ.get("ImageOS", "unknown")),
        ("runner_image_version", os.environ.get("ImageVersion", "unknown")),
        ("runtime_file_count", str(runtime_stats["file_count"])),
        ("runtime_pe32_count", str(runtime_stats["pe32_count"])),
    )
    with manifest.open("w", encoding="utf-8", newline="\n") as handle:
        for key, value in fields:
            if "\n" in value or "\r" in value:
                raise WindowsPipelineError(f"Manifest field {key} is multiline")
            handle.write(f"{key}={value}\n")
        handle.write("\npackaged_files:\n")
        for name in packaged_files:
            handle.write(name + "\n")
    print(
        f"Packaged Windows i686 Chromium {version}: {package.stat().st_size} bytes, "
        f"SHA-256 {package_sha}, {runtime_stats['pe32_count']} PE32 binaries"
    )
    return package, checksum, manifest


def cleanup_source_archive(cache_dir: Path, version: str) -> None:
    version = validate_version(version)
    for suffix in (".tar.xz", ".tar.xz.partial"):
        path = cache_dir / f"chromium-{version}{suffix}"
        if path.is_file() and not path.is_symlink():
            path.unlink()
            print(f"Removed expendable prepared source archive: {path}")


def write_stage_summary(
    *,
    work_root: Path,
    version: str,
    stage: str,
    attempt: str,
    result_file: Path | None,
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not summary_path:
        return
    result: dict[str, object] = {}
    if result_file is not None and result_file.is_file():
        result = _read_json_object(result_file, "build result")
    archive = Path(os.environ.get("GITHUB_WORKSPACE", str(work_root))) / "checkpoints-windows" / f"out-{OUT_NAME}.tar.zst"
    free = shutil.disk_usage(work_root).free / 1024**3
    with Path(summary_path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("## Chromium Windows i686 stage summary\n\n")
        handle.write("| Field | Value |\n| --- | --- |\n")
        for key, value in (
            ("Chromium", version),
            ("Stage", stage),
            ("Attempt", attempt),
            ("Complete", str(result.get("complete", "unknown"))),
            ("Failure class", str(result.get("failure_class", "none") or "none")),
            ("Ninja entries", f"{result.get('ninja_entries_before', '?')} -> {result.get('ninja_entries_after', '?')}"),
            ("No-progress streak", str(result.get("no_progress_streak", "?"))),
            ("Checkpoint bytes", str(archive.stat().st_size if archive.is_file() else "none")),
            ("Free disk GiB", f"{free:.1f}"),
        ):
            handle.write(f"| {key} | `{value}` |\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select-root")
    select.add_argument("--minimum-free-gib", type=int, default=DEFAULT_MIN_WORK_GIB)

    requirements = subparsers.add_parser("requirements")
    requirements.add_argument("--source", type=Path, required=True)

    config = subparsers.add_parser("config-hash")
    config.add_argument("--repository-root", type=Path, required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--work-root", type=Path, required=True)
    prepare.add_argument("--cache-dir", type=Path, required=True)
    prepare.add_argument("--repository-root", type=Path, required=True)
    prepare.add_argument("--stage", type=int, required=True)
    prepare.add_argument("--repository", default="")
    prepare.add_argument("--expected-ref", default="")
    prepare.add_argument("--expected-sha", default="")
    prepare.add_argument("--preferred-run-id", default="")
    prepare.add_argument("--fallback-run-id", default="")
    prepare.add_argument("--evidence-dir", type=Path)

    verify_run = subparsers.add_parser("verify-checkpoint-run")
    verify_run.add_argument("--repository", required=True)
    verify_run.add_argument("--run-id", required=True)
    verify_run.add_argument("--version", required=True)
    verify_run.add_argument("--expected-stage", type=int, required=True)
    verify_run.add_argument("--expected-ref", required=True)
    verify_run.add_argument("--expected-sha", required=True)
    verify_run.add_argument("--artifact-name", required=True)

    verify_build = subparsers.add_parser("verify-build-run")
    verify_build.add_argument("--repository", required=True)
    verify_build.add_argument("--run-id", required=True)
    verify_build.add_argument("--version", required=True)
    verify_build.add_argument("--expected-ref", required=True)
    verify_build.add_argument("--expected-sha", required=True)

    release_bundle = subparsers.add_parser("validate-release-bundle")
    release_bundle.add_argument("directory", type=Path)
    release_bundle.add_argument("--version", required=True)
    release_bundle.add_argument("--expected-run-id", required=True)
    release_bundle.add_argument("--expected-sha", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--work-root", type=Path, required=True)
    build.add_argument("--result-file", type=Path, required=True)
    build.add_argument("--job-started-at", type=int, required=True)
    build.add_argument("--checkpoint-minutes", type=int, default=325)
    build.add_argument("--stall-minutes", type=int, default=90)
    build.add_argument("--jobs", type=int, default=4)

    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("--work-root", type=Path, required=True)
    checkpoint.add_argument("--repository-root", type=Path, required=True)
    checkpoint.add_argument("--destination", type=Path, required=True)
    checkpoint.add_argument("--stage", type=int, required=True)
    checkpoint.add_argument("--no-progress-streak", type=int, required=True)

    package = subparsers.add_parser("package")
    package.add_argument("--version", required=True)
    package.add_argument("--work-root", type=Path, required=True)
    package.add_argument("--repository-root", type=Path, required=True)
    package.add_argument("--destination", type=Path, required=True)
    package.add_argument("--smoke-timeout-seconds", type=int, default=120)

    cleanup = subparsers.add_parser("cleanup-source-archive")
    cleanup.add_argument("--cache-dir", type=Path, required=True)
    cleanup.add_argument("--version", required=True)

    classify = subparsers.add_parser("classify-log")
    classify.add_argument("path", type=Path)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--work-root", type=Path, required=True)
    summary.add_argument("--version", required=True)
    summary.add_argument("--stage", required=True)
    summary.add_argument("--attempt", required=True)
    summary.add_argument("--result-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "select-root":
        select_work_root(minimum_free_gib=args.minimum_free_gib)
    elif args.command == "requirements":
        print(json.dumps(asdict(verify_windows_x86_source_contract(args.source)), sort_keys=True))
    elif args.command == "config-hash":
        print(compute_port_config_sha256(args.repository_root))
    elif args.command == "prepare":
        prepare_pipeline(
            version=args.version,
            work_root=args.work_root,
            cache_dir=args.cache_dir,
            repository_root=args.repository_root,
            stage=args.stage,
            repository=args.repository,
            expected_ref=args.expected_ref,
            expected_sha=args.expected_sha,
            preferred_run_id=args.preferred_run_id,
            fallback_run_id=args.fallback_run_id,
            evidence_dir=args.evidence_dir,
        )
    elif args.command == "verify-checkpoint-run":
        proof = verify_checkpoint_run(
            repository=args.repository,
            run_id=args.run_id,
            version=args.version,
            expected_stage=args.expected_stage,
            expected_ref=args.expected_ref,
            expected_sha=args.expected_sha,
            artifact_name=args.artifact_name,
        )
        print(json.dumps(proof, sort_keys=True))
    elif args.command == "verify-build-run":
        proof = verify_completed_build_run(
            repository=args.repository,
            run_id=args.run_id,
            version=args.version,
            expected_ref=args.expected_ref,
            expected_sha=args.expected_sha,
        )
        print(json.dumps(proof, sort_keys=True))
    elif args.command == "validate-release-bundle":
        result = validate_release_bundle(
            args.directory,
            version=args.version,
            expected_run_id=args.expected_run_id,
            expected_sha=args.expected_sha,
        )
        print(json.dumps(result, sort_keys=True))
    elif args.command == "build":
        run_build_slice(
            work_root=args.work_root,
            result_file=args.result_file,
            job_started_at=args.job_started_at,
            checkpoint_minutes=args.checkpoint_minutes,
            stall_minutes=args.stall_minutes,
            jobs=args.jobs,
        )
    elif args.command == "checkpoint":
        create_checkpoint(
            work_root=args.work_root,
            repository_root=args.repository_root,
            destination=args.destination,
            stage=args.stage,
            no_progress_streak=args.no_progress_streak,
        )
    elif args.command == "package":
        package_build(
            version=args.version,
            work_root=args.work_root,
            repository_root=args.repository_root,
            destination=args.destination,
            smoke_timeout_seconds=args.smoke_timeout_seconds,
        )
    elif args.command == "cleanup-source-archive":
        cleanup_source_archive(args.cache_dir, args.version)
    elif args.command == "classify-log":
        print(classify_build_log(args.path))
    elif args.command == "summary":
        write_stage_summary(
            work_root=args.work_root,
            version=args.version,
            stage=args.stage,
            attempt=args.attempt,
            result_file=args.result_file,
        )
    else:  # pragma: no cover - argparse owns choices.
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InfrastructureError as exc:
        print(f"infrastructure error: {exc}", file=sys.stderr)
        raise SystemExit(75) from exc
    except (
        WindowsPipelineError,
        WindowsRuntimeError,
        WatchdogError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"deterministic pipeline error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
