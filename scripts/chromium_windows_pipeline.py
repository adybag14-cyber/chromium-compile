#!/usr/bin/env python3
"""Hardened, resumable Chromium Windows i686 build primitives.

The GitHub workflows intentionally keep control-plane policy in YAML while this
module owns platform-specific source, toolchain, checkpoint, build, packaging,
and runtime contracts. Every trust-bearing input is exact and fail-closed.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
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
    source_cache_key,
    source_download_url,
    validate_effective_https_host,
    validate_source_metadata,
    verify_file,
    write_marker,
)
from chromium_source_object import fetch_metadata as fetch_source_metadata
from chromium_tool_pins import (
    CipdPackagePin,
    GcsObjectPin,
    GitDependencyPin,
    resolve_pins,
    resolve_windows_cipd_tool_pins,
    resolve_windows_gcs_tool_pins,
    resolve_windows_git_tool_pins,
    windows_cipd_tool_descriptor_sha256,
    windows_gcs_tool_descriptor_sha256,
    windows_git_tool_descriptor_sha256,
)
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
CHROMIUM_COMMIT_POSITION_RE = re.compile(
    r"^refs/(?:heads/main|branch-heads/[1-9][0-9]*)@\{#[1-9][0-9]*\}$"
)
GN_TARGET_LABEL_RE = re.compile(
    r"^//[A-Za-z0-9_./+-]+:[A-Za-z0-9_.+-]+$"
)
OUT_NAME = "Release_x86_win"
CHECKPOINT_CONTRACT_VERSION = 5
CHECKPOINT_MANIFEST_SCHEMA = 5
PREPARED_STATE_SCHEMA = 5
PORT_CONFIG_HASH_SCHEMA = 1
MAX_NO_PROGRESS_STREAK = 2
# Ninja 1.12's native Windows Stat implementation converts FILETIME values by
# subtracting an internal epoch at 2000-12-31 21:06:40 UTC. Earlier timestamps
# become negative, which Ninja reserves for Stat errors; because that conversion
# does not populate an error string, the user-visible result is exactly
# ``ninja: error:`` followed by ``ninja: build stopped: .``. Keep Windows resume
# inputs comfortably above that boundary while still well behind build outputs.
WINDOWS_LEGACY_RESUME_INPUT_EPOCH = 946_684_800
WINDOWS_NINJA_TIMESTAMP_ZERO_EPOCH = 978_296_800
WINDOWS_RESUME_INPUT_EPOCH = 1_262_304_000
DEFAULT_MIN_WORK_GIB = 70
DEFAULT_SOURCE_RESERVE_GIB = 25
DEFAULT_CHECKPOINT_RESERVE_GIB = 12
DEFAULT_SOURCE_MAX_MEMBERS = 2_000_000
DEFAULT_SOURCE_MAX_UNPACKED_GIB = 80
DEFAULT_CHECKPOINT_MAX_UNPACKED_GIB = 80
DEFAULT_TOOL_MAX_MEMBERS = 500_000
DEFAULT_TOOL_MAX_UNPACKED_GIB = 8
DEFAULT_NETWORK_TIMEOUT_SECONDS = 7200
DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS = 3600
DEFAULT_ARCHIVE_TIMEOUT_SECONDS = 1800
DEFAULT_REMOVE_TIMEOUT_SECONDS = 900
MAX_GN_REGEN_DEPFILE_BYTES = 4 * 1024 * 1024
MAX_GN_REGEN_DEPENDENCIES = 100_000
MAX_GN_REGEN_FUTURE_SKEW_SECONDS = 300
MAX_NINJA_INPUT_CLOSURE_BYTES = 128 * 1024 * 1024
MAX_NINJA_INPUT_CLOSURE_COUNT = 2_000_000
MIN_WINDOWS_COMPILER_SLICE_SECONDS = 10 * 60
WINDOWS_CHECKPOINT_TERMINATION_RESERVE_SECONDS = 5 * 60
NINJA_CONTROLLER_ROTATION_EXIT_CODE = 87
NINJA_CONTROLLER_RETRY_EXIT_CODE = 88
# The restored tab-strip generator has three COPY destinations. Production
# evidence showed that a destination can require two controller passes before
# Ninja advances, so allow two full passes plus a bounded margin.
MAX_NINJA_CONTROLLER_RESTARTS_PER_SLICE = 8
GITILES_HOST = "chromium.googlesource.com"
MIN_TRUSTED_CHROMIUM_TIMESTAMP = 1_200_000_000
MAX_PE_COFF_TIMESTAMP = 0xFFFFFFFF
SOURCE_DOWNLOAD_HOST = "commondatastorage.googleapis.com"
WINDOWS_GCS_TOOL_DOWNLOAD_HOST = "commondatastorage.googleapis.com"
WINDOWS_KITS_ROOT = Path(r"C:\Program Files (x86)\Windows Kits\10")
WINDOWS_SYSTEM_DRIVE_ROOT = Path("C:\\")
TRUSTED_BUILD_WORKFLOW = ".github/workflows/chromium-windows-i686.yml"
BUILD_TITLE_RE = re.compile(
    r"^Chromium Windows i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) "
    r"- stage ([1-9][0-9]*) - attempt ([0-9]+)$"
)
TRUSTED_EXECUTABLE_BASENAMES = frozenset(
    (
        "7z",
        "7z.exe",
        "bindgen.exe",
        "cargo.exe",
        "clang-cl.exe",
        "clang-format.exe",
        "cmd.exe",
        "curl",
        "curl.exe",
        "esbuild.exe",
        "gh",
        "gh.exe",
        "git",
        "git.exe",
        "gn.exe",
        "go",
        "go.exe",
        "gperf.exe",
        "ninja.exe",
        "node.exe",
        "perl.exe",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "python",
        "python.exe",
        "python3",
        "python3.exe",
        "rustc.exe",
        "rustfmt.exe",
        "tar",
        "tar.exe",
        "tsc.exe",
        "winget",
        "winget.exe",
        "vswhere.exe",
    )
)

WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
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
DAWN_GENERATOR_LABEL = "//third_party/dawn/src/tint:generate_sources"

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
class ChromiumTagIdentity:
    commit: str
    commit_position: str
    committer_time: str
    timestamp: int


@dataclass(frozen=True)
class PreparedState:
    schema: int
    version: str
    source_sha256: str
    chromium_commit: str
    chromium_commit_position: str
    chromium_commit_timestamp: int
    windows_build_timestamp: int
    depot_tools_revision: str
    gn_version: str
    ninja_package: str
    ninja_version: str
    cpython3_version: str
    windows_cipd_tools_sha256: str
    windows_gcs_tools_sha256: str
    windows_git_tools_sha256: str
    clang_revision: str
    sdk_family: str
    sdk_servicing: str
    visual_studio_year: str
    visual_studio_version: str
    port_config_hash_schema: int
    port_config_sha256: str
    checkpoint_no_progress_streak: int


@dataclass(frozen=True)
class CheckpointCompatibility:
    no_progress_streak: int
    requires_gn_refresh: bool
    gn_refresh_fields: tuple[str, ...]
    migration_run_id: str = ""


@dataclass(frozen=True)
class CheckpointMigration:
    run_id: str
    version: str
    stage: int
    producer_sha: str
    port_config_sha256: str
    archive_sha256: str
    resume_input_epoch: int | None = None


# Immutable bridges preserve already-proven compiler output across narrowly
# scoped resume-path repairs. Every field is exact; these are not generic
# cross-lineage compatibility escape hatches. The next checkpoint is written
# with the current lineage/configuration and resumes normal strict validation.
APPROVED_CHECKPOINT_MIGRATIONS = {
    "33274094424": CheckpointMigration(
        run_id="33274094424",
        version="153.0.8010.12",
        stage=1,
        producer_sha="70443e888304bc1ac17e986e33f1fab605243fca",
        port_config_sha256=(
            "6ed02add0b467c87a1b1072d8340713374562dcfc090752ce0631deeaf078787"
        ),
        archive_sha256=(
            "6f124bee59ed5693db7a0477f91ad57330f990cc0f32aa43fe0e9cabb426e058"
        ),
        resume_input_epoch=WINDOWS_LEGACY_RESUME_INPUT_EPOCH,
    ),
    "33357533082": CheckpointMigration(
        run_id="33357533082",
        version="153.0.8010.12",
        stage=4,
        producer_sha="4bc44f0ba432ca2b3eaeac40811e2533daeb2e98",
        port_config_sha256=(
            "2c2fade4813c2baca7f6ef770c102e7bbe999b62dd8871282d336bf03852bc67"
        ),
        archive_sha256=(
            "c81f702c589404b3f4ffa6bade8db08650f6e8cade30a45b274d8e0740497ce7"
        ),
        resume_input_epoch=WINDOWS_LEGACY_RESUME_INPUT_EPOCH,
    ),
    "33390506701": CheckpointMigration(
        run_id="33390506701",
        version="153.0.8010.12",
        stage=5,
        producer_sha="cbef7e08f1abc62d05715978ee4f96a02c13163b",
        port_config_sha256=(
            "8b1d3f5e50c730efac4325089f1afbb68ead1490335fcf4689d4ec373b06d317"
        ),
        archive_sha256=(
            "ace1e90426e6973d8ec4dabef9f73b5e106a9652003bfd2dfcde91723429f392"
        ),
        resume_input_epoch=WINDOWS_LEGACY_RESUME_INPUT_EPOCH,
    ),
    "33454594511": CheckpointMigration(
        run_id="33454594511",
        version="153.0.8010.12",
        stage=6,
        producer_sha="b31d768fa3843489a63e6f4b375ccd77e79a85fe",
        port_config_sha256=(
            "fec64b496706536a4e15c1b6dd8b0a72ff40aeaf9e0427994517770cace86e08"
        ),
        archive_sha256=(
            "c0264ff9c1685648502127fb64d1472270a7021670228e6810fb62f060dd9040"
        ),
        resume_input_epoch=WINDOWS_LEGACY_RESUME_INPUT_EPOCH,
    ),
    "33502755381": CheckpointMigration(
        run_id="33502755381",
        version="153.0.8010.12",
        stage=7,
        producer_sha="08d73b9dfb715ede3c05c77158bc52d8dccf6a6b",
        port_config_sha256=(
            "7754ab4a089b72c33a35a0686377ae343eae6e188d73d26b5c32fda6e5c9b918"
        ),
        archive_sha256=(
            "e066c261980fafb4ec8c086edcf93090d0ce0fbe24c8f2c08238472e7acf3dd5"
        ),
        resume_input_epoch=WINDOWS_LEGACY_RESUME_INPUT_EPOCH,
    ),
}


def validate_version(version: str) -> str:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise WindowsPipelineError(f"Invalid Chromium version: {version!r}")
    return version


def validate_sha1(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise WindowsPipelineError(f"{label} must be exactly 40 hexadecimal characters")
    normalized = value.lower()
    if not SHA1_RE.fullmatch(normalized):
        raise WindowsPipelineError(f"{label} must be exactly 40 hexadecimal characters")
    return normalized


def validate_sha256(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise WindowsPipelineError(f"{label} must be exactly 64 hexadecimal characters")
    normalized = value.lower()
    if not SHA256_RE.fullmatch(normalized):
        raise WindowsPipelineError(f"{label} must be exactly 64 hexadecimal characters")
    return normalized


def validate_chromium_commit_position(value: str, label: str) -> str:
    if not isinstance(value, str) or not CHROMIUM_COMMIT_POSITION_RE.fullmatch(value):
        raise WindowsPipelineError(f"{label} is not a canonical Chromium commit position")
    return value


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


def validate_chromium_timestamp(value: str | int, label: str) -> int:
    timestamp = bounded_int(
        value,
        label,
        minimum=MIN_TRUSTED_CHROMIUM_TIMESTAMP,
        maximum=MAX_PE_COFF_TIMESTAMP,
    )
    if timestamp > int(time.time()) + 86_400:
        raise WindowsPipelineError(f"{label} is implausibly far in the future")
    return timestamp


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 600,
    capture: bool = False,
    discard_stdout: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not command:
        raise WindowsPipelineError("Refusing to run an empty command")
    validated_command = list(command)
    if any(
        not isinstance(value, str) or not value or any(char in value for char in "\x00\r\n")
        for value in validated_command
    ):
        raise WindowsPipelineError("Command arguments must be non-empty, single-line strings")
    executable_name = Path(validated_command[0]).name.lower()
    if executable_name not in TRUSTED_EXECUTABLE_BASENAMES:
        raise WindowsPipelineError(
            f"Executable is outside the Windows pipeline allowlist: {executable_name!r}"
        )
    if capture and discard_stdout:
        raise WindowsPipelineError("Command output cannot be captured and discarded together")
    print("+ " + subprocess.list2cmdline(validated_command), flush=True)
    try:
        # The executable is selected from the fixed allowlist above, every call
        # uses shell=False, and all caller-provided fields have strict semantic
        # validation before reaching an argument slot.
        result = subprocess.run(
            validated_command,  # lgtm [py/command-line-injection]
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=(
                subprocess.PIPE
                if capture
                else subprocess.DEVNULL if discard_stdout else None
            ),
            stderr=subprocess.PIPE if capture or discard_stdout else None,
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


def _runner_command_file(variable: str, prefix: str) -> Path | None:
    raw = os.environ.get(variable, "")
    if not raw:
        return None
    runner_temp_raw = os.environ.get("RUNNER_TEMP", "")
    if not runner_temp_raw:
        raise WindowsPipelineError(f"{variable} is set without RUNNER_TEMP authority")
    runner_temp = Path(runner_temp_raw).resolve()
    trusted_parent = (runner_temp / "_runner_file_commands").resolve()
    candidate = Path(raw)
    name = candidate.name
    if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{8,100}", name):
        raise WindowsPipelineError(f"{variable} has an unexpected runner-command filename")
    expected = trusted_parent / name
    if candidate.resolve() != expected or not expected.parent.is_dir() or expected.is_symlink():
        raise WindowsPipelineError(f"{variable} escapes the trusted runner command directory")
    return expected


def _append_github_env(name: str, value: str) -> None:
    path = _runner_command_file("GITHUB_ENV", "set_env_")
    if path is None:
        return
    if "\n" in value or "\r" in value:
        raise WindowsPipelineError(f"Refusing multiline GitHub environment value for {name}")
    with path.open("a", encoding="utf-8", newline="\n") as handle:  # lgtm [py/path-injection]
        handle.write(f"{name}={value}\n")


def _write_github_output(values: Mapping[str, str]) -> None:
    path = _runner_command_file("GITHUB_OUTPUT", "set_output_")
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as handle:  # lgtm [py/path-injection]
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise WindowsPipelineError(f"Refusing multiline GitHub output for {name}")
            handle.write(f"{name}={value}\n")


def select_work_root(*, minimum_free_gib: int = DEFAULT_MIN_WORK_GIB) -> Path:
    minimum_free_gib = bounded_int(
        minimum_free_gib, "minimum_free_gib", minimum=20, maximum=500
    )
    if os.name != "nt":
        raise InfrastructureError("Windows work-root selection requires a Windows runner")
    candidates: dict[str, Path] = {}
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
    sdk_family = normalize_sdk_family(sdk_match.group(1))
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


def fetch_gitiles_tag_identity(version: str) -> ChromiumTagIdentity:
    version = validate_version(version)
    url = f"https://{GITILES_HOST}/chromium/src/+/refs/tags/{version}?format=JSON"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "chromium-windows-i686/1"},
    )
    last_error: BaseException | None = None
    raw = b""
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                validate_effective_https_host(response.geturl(), GITILES_HOST)
                raw = response.read(4 * 1024 * 1024 + 1)
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise WindowsPipelineError(
                    f"Authoritative Chromium tag does not exist: {version}"
                ) from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        except ValueError:
            raise
        if attempt < 5:
            delay = min(2 ** (attempt - 1), 8)
            print(
                f"::warning::Gitiles tag identity fetch failed on attempt "
                f"{attempt}/5 ({last_error}); retrying in {delay}s"
            )
            time.sleep(delay)
    if last_error is not None:
        raise InfrastructureError(
            f"Could not fetch authoritative Chromium {version} tag identity "
            f"after bounded retries: {last_error}"
        ) from last_error
    if len(raw) > 4 * 1024 * 1024:
        raise WindowsPipelineError("Gitiles tag identity unexpectedly exceeds 4 MiB")
    prefix = b")]}'\n"
    if not raw.startswith(prefix):
        raise WindowsPipelineError("Gitiles tag identity omitted its XSSI prefix")
    try:
        payload = json.loads(raw[len(prefix) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WindowsPipelineError("Gitiles tag identity returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise WindowsPipelineError("Gitiles tag identity must be a JSON object")
    commit_value = payload.get("commit")
    if not isinstance(commit_value, str):
        raise WindowsPipelineError("Gitiles tag identity omitted its commit")
    commit = validate_sha1(commit_value, "Chromium tag commit")
    committer = payload.get("committer")
    if not isinstance(committer, dict):
        raise WindowsPipelineError("Gitiles tag identity omitted committer metadata")
    committer_time = committer.get("time")
    if not isinstance(committer_time, str):
        raise WindowsPipelineError("Gitiles tag identity omitted committer time")
    match = re.fullmatch(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
        r"([ 0-9][0-9]?) ([0-2][0-9]):([0-5][0-9]):([0-5][0-9]) "
        r"([0-9]{4})",
        committer_time,
    )
    if not match:
        raise WindowsPipelineError(
            f"Gitiles tag committer time has an unexpected format: {committer_time!r}"
        )
    months = {
        name: index
        for index, name in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
            start=1,
        )
    }
    try:
        moment = datetime.datetime(
            int(match.group(6)),
            months[match.group(1)],
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            tzinfo=datetime.timezone.utc,
        )
    except ValueError as exc:
        raise WindowsPipelineError("Gitiles tag committer time is invalid") from exc
    timestamp = int(moment.timestamp())
    timestamp = validate_chromium_timestamp(timestamp, "Gitiles tag timestamp")
    message = payload.get("message")
    if not isinstance(message, str):
        raise WindowsPipelineError("Gitiles tag identity omitted its commit message")
    positions = re.findall(r"(?m)^Cr-Commit-Position: (\S+)$", message)
    if len(positions) != 1:
        raise WindowsPipelineError(
            "Gitiles tag commit omitted one canonical Cr-Commit-Position"
        )
    commit_position = validate_chromium_commit_position(
        positions[0], "Gitiles tag commit position"
    )
    return ChromiumTagIdentity(
        commit=commit,
        commit_position=commit_position,
        committer_time=committer_time,
        timestamp=timestamp,
    )


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
    effective_lines = (result.stdout or "").strip().splitlines()
    if not effective_lines:
        raise InfrastructureError(
            f"curl omitted its effective URL for Windows GCS tool {pin.dependency}"
        )
    effective = effective_lines[-1]
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


def normalize_windows_resume_inputs(
    source: Path,
    *,
    epoch: int = WINDOWS_RESUME_INPUT_EPOCH,
) -> dict[str, int]:
    """Give immutable source/tool inputs stable mtimes before restoring Ninja output."""
    epoch = bounded_int(
        epoch,
        "Windows resume input epoch",
        minimum=WINDOWS_NINJA_TIMESTAMP_ZERO_EPOCH + 1,
        maximum=int(time.time()),
    )
    if not source.is_dir() or source.is_symlink():
        raise WindowsPipelineError(
            f"Chromium source is unavailable for resume timestamp normalization: {source}"
        )
    source = source.resolve()
    output_root = source / "out"
    timestamp_ns = epoch * 1_000_000_000
    directories: list[Path] = []
    file_count = 0
    directory_count = 0
    symlink_count = 0
    started = time.monotonic()

    def normalize_symlink(path: Path) -> None:
        if os.name != "nt":
            os.utime(
                path,
                ns=(timestamp_ns, timestamp_ns),
                follow_symlinks=False,
            )
            return

        # Python's os.utime(..., follow_symlinks=False) is not implemented on
        # Windows. Open the reparse point itself and set its access/write time
        # without following it, including for directory symlinks.
        import ctypes
        from ctypes import wintypes

        file_write_attributes = 0x0100
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        file_flag_open_reparse_point = 0x00200000
        # Chromium source archives contain POSIX/Linux reparse tags whose
        # directory nature is not necessarily exposed by Path.is_dir() or the
        # ordinary FILE_ATTRIBUTE_DIRECTORY bit. BACKUP_SEMANTICS is harmless
        # for file reparse points and is required to open directory-backed
        # reparse points themselves instead of following their targets.
        flags = file_flag_open_reparse_point | file_flag_backup_semantics

        native = os.path.abspath(path)
        if native.startswith("\\\\"):
            native = "\\\\?\\UNC\\" + native[2:]
        elif not native.startswith("\\\\?\\"):
            native = "\\\\?\\" + native

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        set_file_time = kernel32.SetFileTime
        set_file_time.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        set_file_time.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            native,
            file_write_attributes,
            share_all,
            None,
            open_existing,
            flags,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        windows_ticks = (epoch + 11_644_473_600) * 10_000_000
        file_time = wintypes.FILETIME(
            windows_ticks & 0xFFFFFFFF,
            windows_ticks >> 32,
        )
        try:
            if not set_file_time(handle, None, ctypes.byref(file_time), ctypes.byref(file_time)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle(handle)

    def normalize(path: Path) -> None:
        nonlocal file_count, directory_count, symlink_count
        is_link = path.is_symlink()
        try:
            if is_link:
                normalize_symlink(path)
            else:
                os.utime(path, ns=(timestamp_ns, timestamp_ns))
        except (NotImplementedError, OSError) as exc:
            raise InfrastructureError(
                f"Could not normalize immutable Windows resume input mtime: {path}: {exc}"
            ) from exc
        if is_link:
            symlink_count += 1
        elif path.is_dir():
            directory_count += 1
        else:
            file_count += 1

    for root, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        if root_path == source:
            directory_names[:] = [name for name in directory_names if name != output_root.name]
        traversable: list[str] = []
        for name in directory_names:
            path = root_path / name
            if path.is_symlink():
                normalize(path)
            else:
                traversable.append(name)
        directory_names[:] = traversable
        for name in file_names:
            normalize(root_path / name)
        directories.append(root_path)

    # Directories are normalized last, matching the proven Linux resume path.
    # This removes extraction/installation directory mtimes from GN regeneration inputs.
    for directory in reversed(directories):
        normalize(directory)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    stats = {
        "epoch": epoch,
        "files": file_count,
        "directories": directory_count,
        "symlinks": symlink_count,
        "elapsed_ms": elapsed_ms,
    }
    print(
        "Normalized immutable Windows source/tool mtimes before checkpoint restore: "
        f"epoch={epoch}; files={file_count}; directories={directory_count}; "
        f"symlinks={symlink_count}; elapsed_ms={elapsed_ms}"
    )
    return stats


def rebase_windows_ninja_unsafe_output_mtimes(
    out: Path,
    *,
    safe_epoch: int = WINDOWS_RESUME_INPUT_EPOCH,
) -> dict[str, int]:
    """Move restored output mtimes out of Ninja's reserved error range.

    Chromium's Windows ``copy`` rule uses ``shutil.copy2``, so generated files
    can legitimately retain the resume-input epoch. Checkpoints written before
    the Windows-safe epoch repair therefore contain output files dated
    2000-01-01. Native Windows Ninja maps those FILETIMEs to negative values and
    treats them as failed Stat calls. Rebase only timestamps in that unsafe
    range; completed compiler outputs and both Ninja progress databases keep
    their original contents and timestamps.
    """
    safe_epoch = bounded_int(
        safe_epoch,
        "Windows Ninja-safe output epoch",
        minimum=WINDOWS_NINJA_TIMESTAMP_ZERO_EPOCH + 1,
        maximum=int(time.time()),
    )
    if not out.is_dir() or out.is_symlink():
        raise WindowsPipelineError(
            f"Restored Windows output is unavailable for timestamp repair: {out}"
        )

    unsafe_ns = WINDOWS_NINJA_TIMESTAMP_ZERO_EPOCH * 1_000_000_000
    safe_ns = safe_epoch * 1_000_000_000
    progress_databases = {out / ".ninja_log", out / ".ninja_deps"}
    directories: list[Path] = []
    files_rebased = 0
    directories_rebased = 0
    symlinks_skipped = 0
    started = time.monotonic()

    def rebase(path: Path, *, directory: bool) -> None:
        nonlocal files_rebased, directories_rebased, symlinks_skipped
        if path.is_symlink():
            symlinks_skipped += 1
            return
        try:
            if path.stat().st_mtime_ns <= unsafe_ns:
                os.utime(path, ns=(safe_ns, safe_ns))
                if directory:
                    directories_rebased += 1
                else:
                    files_rebased += 1
        except OSError as exc:
            raise InfrastructureError(
                f"Could not rebase Ninja-unsafe restored output mtime: {path}: {exc}"
            ) from exc

    for root, directory_names, file_names in os.walk(
        out, topdown=True, followlinks=False
    ):
        root_path = Path(root)
        traversable: list[str] = []
        for name in directory_names:
            path = root_path / name
            if path.is_symlink():
                symlinks_skipped += 1
            else:
                traversable.append(name)
        directory_names[:] = traversable
        for name in file_names:
            path = root_path / name
            if path in progress_databases:
                continue
            rebase(path, directory=False)
        directories.append(root_path)

    # Child creation/extraction can update parent directory mtimes, so repair
    # directories last just like the immutable-input normalization pass.
    for directory in reversed(directories):
        rebase(directory, directory=True)

    stats = {
        "ninja_timestamp_zero_epoch": WINDOWS_NINJA_TIMESTAMP_ZERO_EPOCH,
        "safe_epoch": safe_epoch,
        "files_rebased": files_rebased,
        "directories_rebased": directories_rebased,
        "symlinks_skipped": symlinks_skipped,
        "progress_databases_preserved": sum(
            path.is_file() and not path.is_symlink()
            for path in progress_databases
        ),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    print(
        "Rebased Ninja-unsafe restored Windows output mtimes: "
        f"files={files_rebased}; directories={directories_rebased}; "
        f"symlinks_skipped={symlinks_skipped}; safe_epoch={safe_epoch}; "
        f"progress_databases_preserved={stats['progress_databases_preserved']}; "
        f"elapsed_ms={stats['elapsed_ms']}"
    )
    return stats


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"([0-9]+(?:\.[0-9]+){1,3})", value)
    if not match:
        raise WindowsPipelineError(f"Could not parse numeric version from {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def normalize_sdk_family(value: str) -> str:
    if not re.fullmatch(r"10\.0\.[0-9]{4,6}\.0", value):
        raise WindowsPipelineError(f"Unsupported Windows SDK family: {value!r}")
    major, minor, build, revision = (int(part) for part in value.split("."))
    if major != 10 or minor != 0 or revision != 0 or not 1000 <= build <= 999999:
        raise WindowsPipelineError(f"Unsupported Windows SDK family: {value!r}")
    return f"{major}.{minor}.{build}.{revision}"


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
        # sdk_family is canonicalized through integer components before this
        # trusted fixed-root path is constructed.
        if candidate.is_file():  # lgtm [py/path-injection]
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
    return WINDOWS_KITS_ROOT


def _sdk_probe_binary(kits: Path, sdk_family: str) -> Path | None:
    sdk_family = normalize_sdk_family(sdk_family)
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
    sdk_family = normalize_sdk_family(sdk_family)
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
    sdk_family = normalize_sdk_family(requirements.sdk_family)
    parts = sdk_family.split(".")
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
    sdk_family = normalize_sdk_family(requirements.sdk_family)
    kits = _windows_kits_root()
    if not _sdk_layout_is_complete(kits, sdk_family):
        system_free = shutil.disk_usage(WINDOWS_SYSTEM_DRIVE_ROOT).free
        if system_free < 8 * 1024**3:
            raise InfrastructureError(
                f"Only {system_free} bytes are free on the SDK installation drive; "
                "at least 8 GiB is required before installing a source-declared SDK family"
            )
        print(
            f"Chromium requires Windows SDK {sdk_family}; "
            "installing its official Microsoft winget package"
        )
        _install_sdk_with_winget(requirements)
    if not _sdk_layout_is_complete(kits, sdk_family):
        raise WindowsPipelineError(
            f"Windows SDK installation completed without Chromium's required "
            f"{sdk_family} x86 headers, libraries, tools, and debugging runtime"
        )
    probe = _sdk_probe_binary(kits, sdk_family)
    assert probe is not None
    servicing = _file_product_version(probe)
    if _version_tuple(servicing) < _version_tuple(requirements.sdk_min_servicing):
        raise WindowsPipelineError(
            f"Installed Windows SDK servicing version {servicing!r} is older than "
            f"Chromium's documented minimum {requirements.sdk_min_servicing}"
        )
    print(
        f"Validated Windows SDK family {sdk_family}, servicing {servicing}, "
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
    env["GOTOOLCHAIN"] = "local"
    env[f"vs{requirements.visual_studio_year}_install"] = str(visual_studio)
    env["GYP_MSVS_OVERRIDE_PATH"] = str(visual_studio)
    env["PATH"] = os.pathsep.join(
        (str(depot_tools), str(depot_tools / ".cipd_bin"), env.get("PATH", ""))
    )
    return env


def install_depot_tools(source: Path, work_root: Path) -> tuple[Path, dict[str, str]]:
    pins = resolve_pins(source / "DEPS")
    if "cpython3_version" not in pins:
        raise WindowsPipelineError(
            "Windows Chromium DEPS lacks an immutable cpython3_version required by early GN scripts"
        )
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
        ["cmd.exe", "/d", "/c", "call", str(bootstrap)],
        env=bootstrap_env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    _run(
        ["cmd.exe", "/d", "/c", "call", str(depot / "cipd.bat"), "version"],
        env=bootstrap_env,
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


def _gcs_tool_download_url(pin: GcsObjectPin) -> str:
    bucket = urllib.parse.quote(pin.bucket, safe="")
    object_name = urllib.parse.quote(pin.object_name, safe="/._+-")
    return (
        f"https://{WINDOWS_GCS_TOOL_DOWNLOAD_HOST}/{bucket}/{object_name}"
        f"?generation={pin.generation}"
    )


def _download_gcs_tool_archive(pin: GcsObjectPin, work_root: Path) -> Path:
    archive_dir = work_root / "gcs-tool-archives"
    archive_dir.mkdir(exist_ok=True)
    safe_stem = pin.dependency.rsplit("/", 1)[-1]
    source_name = pin.output_file or PurePosixPath(pin.object_name).name
    suffix = "".join(Path(source_name).suffixes) or ".object"
    archive = archive_dir / f"{safe_stem}-{pin.sha256[:16]}{suffix}"
    partial = archive.with_suffix(archive.suffix + ".partial")
    for candidate in (archive, partial):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise WindowsPipelineError(
                f"Windows GCS tool cache path is not a regular file: {candidate}"
            )
    if archive.is_file():
        if archive.stat().st_size != pin.size_bytes or sha256_file(archive) != pin.sha256:
            raise WindowsPipelineError(
                f"Cached Windows GCS tool archive does not match Chromium DEPS: {archive.name}"
            )
        print(f"Reusing verified Windows GCS tool archive {archive.name}")
        return archive
    if partial.exists() and partial.stat().st_size > pin.size_bytes:
        partial.unlink()
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise InfrastructureError("curl is unavailable for source-declared Windows GCS tools")
    result = _run(
        [
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
            str(DEFAULT_NETWORK_TIMEOUT_SECONDS),
            "--continue-at",
            "-",
            "--output",
            str(partial),
            "--write-out",
            "%{url_effective}",
            _gcs_tool_download_url(pin),
        ],
        timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS + 120,
        capture=True,
    )
    effective = (result.stdout or "").strip().splitlines()[-1]
    validate_effective_https_host(effective, WINDOWS_GCS_TOOL_DOWNLOAD_HOST)
    actual_size = partial.stat().st_size if partial.is_file() else -1
    if actual_size != pin.size_bytes:
        raise InfrastructureError(
            f"Windows GCS tool download length mismatch for {pin.dependency}: "
            f"expected {pin.size_bytes}, got {actual_size}"
        )
    actual_sha = sha256_file(partial)
    if actual_sha != pin.sha256:
        raise WindowsPipelineError(
            f"Windows GCS tool SHA-256 mismatch for {pin.dependency}: "
            f"expected {pin.sha256}, got {actual_sha}"
        )
    os.replace(partial, archive)
    print(
        f"Downloaded exact {pin.dependency} GCS generation {pin.generation}: "
        f"{pin.size_bytes} bytes, SHA-256 {pin.sha256}"
    )
    return archive


def _safe_tool_member_name(name: str) -> str | None:
    if not name or "\x00" in name or "\\" in name:
        raise WindowsPipelineError(f"Unsafe Windows tool archive member: {name!r}")
    while name.startswith("./"):
        name = name[2:]
    if name.rstrip("/") in {"", "."}:
        return None
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise WindowsPipelineError(f"Unsafe Windows tool archive path: {name!r}")
    if re.match(r"^[A-Za-z]:", path.as_posix()):
        raise WindowsPipelineError(f"Drive-qualified Windows tool archive path: {name!r}")
    for part in path.parts:
        if any(ord(character) < 32 for character in part):
            raise WindowsPipelineError(
                f"Control character in Windows tool archive path: {name!r}"
            )
        if ":" in part or part.endswith((" ", ".")):
            raise WindowsPipelineError(
                f"Windows-ambiguous tool archive path: {name!r}"
            )
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise WindowsPipelineError(
                f"Windows device-name tool archive path: {name!r}"
            )
    return path.as_posix()


def _extract_gcs_tool_archive(
    archive: Path,
    destination: Path,
    *,
    source_root: Path,
) -> dict[str, int]:
    source_root = source_root.resolve()
    resolved_parent = destination.parent.resolve()
    if source_root != resolved_parent and source_root not in resolved_parent.parents:
        raise WindowsPipelineError(
            f"Windows GCS tool destination escapes source: {destination}"
        )
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise WindowsPipelineError(
            f"Windows GCS tool destination is not a regular directory: {destination}"
        )
    staging = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    if staging.exists() or staging.is_symlink():
        raise InfrastructureError(
            f"Generated Windows tool staging path already exists: {staging}"
        )
    staging.mkdir(parents=False)
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    member_count = 0
    unpacked_bytes = 0
    max_unpacked_bytes = DEFAULT_TOOL_MAX_UNPACKED_GIB * 1024**3
    try:
        try:
            tar = tarfile.open(archive, mode="r:*")
        except tarfile.TarError as exc:
            raise WindowsPipelineError(
                f"Source-declared Windows tool object is not a valid tar archive: {archive.name}"
            ) from exc
        with tar:
            for member in tar:
                member_count += 1
                if member_count > DEFAULT_TOOL_MAX_MEMBERS:
                    raise WindowsPipelineError(
                        f"Windows tool archive exceeds member limit {DEFAULT_TOOL_MAX_MEMBERS}"
                    )
                name = _safe_tool_member_name(member.name)
                if name is None:
                    continue
                folded = name.casefold()
                if name in seen or folded in seen_casefold:
                    raise WindowsPipelineError(
                        f"Duplicate or case-aliasing Windows tool archive member: {name}"
                    )
                seen.add(name)
                seen_casefold.add(folded)
                if not (member.isfile() or member.isdir()):
                    raise WindowsPipelineError(
                        f"Windows tool archive contains a link or special file: {name}"
                    )
                target = staging.joinpath(*PurePosixPath(name).parts)
                resolved_target = target.resolve()
                resolved_staging = staging.resolve()
                if (
                    resolved_target != resolved_staging
                    and resolved_staging not in resolved_target.parents
                ):
                    raise WindowsPipelineError(
                        f"Windows tool archive extraction escaped staging: {name}"
                    )
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.size < 0 or member.size > max_unpacked_bytes - unpacked_bytes:
                    raise WindowsPipelineError(
                        f"Windows tool archive exceeds {DEFAULT_TOOL_MAX_UNPACKED_GIB} GiB unpacked bound"
                    )
                unpacked_bytes += member.size
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise WindowsPipelineError(
                        f"Could not read regular Windows tool archive member: {name}"
                    )
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except Exception:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    print(
        f"Safely extracted {archive.name}: members={member_count}; "
        f"unpacked_bytes={unpacked_bytes}; destination={destination}"
    )
    return {"member_count": member_count, "unpacked_bytes": unpacked_bytes}


def install_windows_gcs_tools(
    source: Path,
    work_root: Path,
    depot_tools: Path,
    env: Mapping[str, str],
) -> tuple[str, tuple[GcsObjectPin, ...]]:
    pins = resolve_windows_gcs_tool_pins(source / "DEPS")
    descriptor_sha = windows_gcs_tool_descriptor_sha256(pins)
    for pin in pins:
        relative = PurePosixPath(pin.dependency).relative_to("src")
        destination = source.joinpath(*relative.parts)
        archive = _download_gcs_tool_archive(pin, work_root)
        if pin.output_file:
            resolved_source = source.resolve()
            resolved_parent = destination.parent.resolve()
            if (
                resolved_parent != resolved_source
                and resolved_source not in resolved_parent.parents
            ):
                raise WindowsPipelineError(
                    f"Windows GCS file destination escapes source: {destination}"
                )
            if destination.is_symlink() or (
                destination.exists() and not destination.is_dir()
            ):
                raise WindowsPipelineError(
                    f"Windows GCS file destination is unsafe: {destination}"
                )
            is_archive = pin.output_file.endswith((".tar.gz", ".tar.xz"))
            if is_archive:
                _extract_gcs_tool_archive(
                    archive,
                    destination,
                    source_root=source,
                )
                output = destination / pin.output_file
                shutil.copy2(archive, output)
            else:
                if destination.exists():
                    shutil.rmtree(destination)
                destination.mkdir(parents=True)
                output = destination / pin.output_file
                shutil.copy2(archive, output)
            if output.is_symlink() or sha256_file(output) != pin.sha256:
                raise InfrastructureError(
                    f"Could not materialize exact Windows GCS file {pin.dependency}"
                )
        else:
            _extract_gcs_tool_archive(archive, destination, source_root=source)
        marker = destination / ".chromium-windows-i686-gcs.json"
        marker.write_text(
            json.dumps(asdict(pin), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    rust_root = source / "third_party/rust-toolchain"
    clang_format = source / "buildtools/win-format/clang-format.exe"
    if not clang_format.is_file() or clang_format.is_symlink():
        raise WindowsPipelineError(
            "Chromium-pinned Windows clang-format object is incomplete"
        )
    _run([str(clang_format), "--version"], cwd=source, env=env, timeout=120)
    node = source / "third_party/node/win/node.exe"
    if not node.is_file() or node.is_symlink():
        raise WindowsPipelineError("Chromium-pinned Windows Node object is incomplete")
    _run([str(node), "--version"], cwd=source, env=env, timeout=120)
    node_modules = source / "third_party/node/node_modules"
    package_manifests = [
        path
        for path in node_modules.rglob("package.json")
        if path.is_file() and not path.is_symlink()
    ]
    if len(package_manifests) < 10:
        raise WindowsPipelineError(
            "Chromium-pinned Node modules payload is structurally incomplete"
        )
    required_rust = (
        rust_root / "VERSION",
        rust_root / "bin/bindgen.exe",
        rust_root / "bin/cargo.exe",
        rust_root / "bin/rustc.exe",
        rust_root / "bin/rustfmt.exe",
    )
    missing = [
        str(path)
        for path in required_rust
        if not path.is_file() or path.is_symlink()
    ]
    if missing:
        raise WindowsPipelineError(
            "Chromium-pinned Windows Rust toolchain is incomplete: " + ", ".join(missing)
        )
    libclang_root = source / "third_party/llvm-libclang"
    libclang = [
        path
        for path in libclang_root.rglob("libclang.dll")
        if path.is_file() and not path.is_symlink()
    ]
    if len(libclang) != 1:
        raise WindowsPipelineError(
            f"Chromium-pinned Windows libclang payload must contain exactly one libclang.dll; "
            f"found {len(libclang)}"
        )
    for executable in required_rust[1:]:
        _run([str(executable), "--version"], cwd=source, env=env, timeout=120)
    depot_python = _depot_python(depot_tools)
    _run(
        [
            str(depot_python),
            str(source / "tools/rust/update_rust.py"),
            "--print-revision",
            "validate",
        ],
        cwd=source,
        env=env,
        timeout=120,
    )
    print(
        "Validated source-declared Windows Rust/libclang GCS descriptors: "
        f"SHA-256 {descriptor_sha}"
    )
    return descriptor_sha, pins


def install_windows_cipd_tools(
    source: Path,
    depot_tools: Path,
    env: Mapping[str, str],
) -> tuple[str, tuple[CipdPackagePin, ...]]:
    devtools_deps = source / "third_party/devtools-frontend/src/DEPS"
    if not devtools_deps.is_file() or devtools_deps.is_symlink():
        raise WindowsPipelineError(
            "Chromium source lacks a regular nested DevTools DEPS file"
        )
    dawn_deps = source / "third_party/dawn/DEPS"
    if not dawn_deps.is_file() or dawn_deps.is_symlink():
        raise WindowsPipelineError(
            "Chromium source lacks a regular nested Dawn DEPS file"
        )
    pins = resolve_windows_cipd_tool_pins(
        source / "DEPS", devtools_deps, dawn_deps
    )
    descriptor_sha = windows_cipd_tool_descriptor_sha256(pins)
    cipd = depot_tools / "cipd.bat"
    resolved_source = source.resolve()
    for pin in pins:
        relative = PurePosixPath(pin.dependency).relative_to("src")
        destination = source.joinpath(*relative.parts)
        resolved_parent = destination.parent.resolve()
        if (
            resolved_parent != resolved_source
            and resolved_source not in resolved_parent.parents
        ):
            raise WindowsPipelineError(
                f"Windows CIPD destination escapes source: {destination}"
            )
        if destination.is_symlink() or (
            destination.exists() and not destination.is_dir()
        ):
            raise WindowsPipelineError(
                f"Windows CIPD destination is unsafe: {destination}"
            )
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        _run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "call",
                str(cipd),
                "install",
                pin.package,
                pin.version,
                "-root",
                str(destination),
                "-log-level",
                "warning",
            ],
            env=env,
            timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
        )
        marker = destination / ".chromium-windows-i686-cipd.json"
        marker.write_text(
            json.dumps(asdict(pin), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    tsc = source / "third_party/typescript/windows-amd64/src/lib/tsc.exe"
    if not tsc.is_file() or tsc.is_symlink():
        raise WindowsPipelineError(
            "Chromium-pinned Windows TypeScript CIPD package omitted lib/tsc.exe"
        )
    _run([str(tsc), "--version"], cwd=source, env=env, timeout=120)
    esbuild = (
        source
        / "third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe"
    )
    if not esbuild.is_file() or esbuild.is_symlink():
        raise WindowsPipelineError(
            "Chromium-pinned DevTools esbuild CIPD package omitted esbuild.exe"
        )
    _run([str(esbuild), "--version"], cwd=source, env=env, timeout=120)
    dawn_go = source / "third_party/dawn/tools/golang/windows-amd64/bin/go.exe"
    if not dawn_go.is_file() or dawn_go.is_symlink():
        raise WindowsPipelineError(
            "Dawn-pinned Windows Go CIPD package omitted bin/go.exe"
        )
    _run([str(dawn_go), "version"], cwd=source, env=env, timeout=120)
    rollup_root = (
        source
        / "third_party/devtools-frontend/src/third_party/rollup_libs"
    )
    rollup_files = [
        path
        for path in rollup_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name not in {".cipd", ".chromium-windows-i686-cipd.json"}
    ]
    if not rollup_files:
        raise WindowsPipelineError(
            "Chromium-pinned DevTools rollup libraries package is empty"
        )
    devtools_root = source / "third_party/devtools-frontend/src"
    sync_rollup = devtools_root / "scripts/deps/sync_rollup_libs.py"
    if not sync_rollup.is_file() or sync_rollup.is_symlink():
        raise WindowsPipelineError(
            "Pinned DevTools source omitted scripts/deps/sync_rollup_libs.py"
        )
    depot_python = _depot_python(depot_tools)
    _run(
        [str(depot_python), str(sync_rollup)],
        cwd=devtools_root,
        env=env,
        timeout=300,
    )
    rollup_native_package = (
        devtools_root
        / "node_modules/@rollup/rollup-win32-x64-msvc/package.json"
    )
    rollup_cli = devtools_root / "node_modules/rollup/dist/bin/rollup"
    node = source / "third_party/node/win/node.exe"
    for path, label in (
        (rollup_native_package, "materialized native Rollup package"),
        (rollup_cli, "DevTools Rollup CLI"),
        (node, "Windows Node executable"),
    ):
        if not path.is_file() or path.is_symlink():
            raise WindowsPipelineError(
                f"DevTools Rollup runtime integration omitted {label}: {path}"
            )
    _run(
        [str(node), str(rollup_cli), "--version"],
        cwd=devtools_root,
        env=env,
        timeout=120,
    )
    print(
        "Validated source-declared Windows CIPD tool descriptors: "
        f"SHA-256 {descriptor_sha}"
    )
    return descriptor_sha, pins


def install_windows_git_tools(
    source: Path,
    work_root: Path,
    env: Mapping[str, str],
) -> tuple[str, tuple[GitDependencyPin, ...]]:
    pins = resolve_windows_git_tool_pins(source / "DEPS")
    descriptor_sha = windows_git_tool_descriptor_sha256(pins)
    checkout_root = work_root / "git-tools"
    checkout_root.mkdir(exist_ok=True)
    names = {
        "src/third_party/gperf": "gperf",
        "src/third_party/microsoft_dxheaders/src": "microsoft-dxheaders",
        "src/third_party/microsoft_webauthn/src": "microsoft-webauthn",
        "src/third_party/perl": "perl",
    }
    resolved_source = source.resolve()
    for pin in pins:
        checkout = checkout_root / names[pin.dependency]
        if checkout.is_symlink() or (checkout.exists() and not checkout.is_dir()):
            raise WindowsPipelineError(
                f"Windows Git tool checkout path is unsafe: {checkout}"
            )
        if checkout.exists():
            current = _run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                capture=True,
                timeout=60,
            ).stdout.strip()
            if current != pin.revision:
                raise InfrastructureError(
                    f"Existing Windows Git tool checkout has revision {current!r}, "
                    f"expected {pin.revision}"
                )
        else:
            checkout.mkdir(parents=True)
            _run(["git", "-C", str(checkout), "init", "-q"], timeout=60)
            _run(
                ["git", "-C", str(checkout), "remote", "add", "origin", pin.repository],
                timeout=60,
            )
            _run(
                ["git", "-C", str(checkout), "config", "core.longpaths", "true"],
                timeout=60,
            )
            _run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "fetch",
                    "--depth=1",
                    "origin",
                    pin.revision,
                ],
                timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
            )
            _run(
                ["git", "-C", str(checkout), "checkout", "-q", "--detach", "FETCH_HEAD"],
                timeout=120,
            )
        checked = _run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture=True,
            timeout=60,
        ).stdout.strip()
        if checked != pin.revision:
            raise WindowsPipelineError(
                f"Windows Git tool revision mismatch for {pin.dependency}: {checked}"
            )
        relative = PurePosixPath(pin.dependency).relative_to("src")
        destination = source.joinpath(*relative.parts)
        resolved_parent = destination.parent.resolve()
        if (
            resolved_parent != resolved_source
            and resolved_source not in resolved_parent.parents
        ):
            raise WindowsPipelineError(
                f"Windows Git tool destination escapes source: {destination}"
            )
        if destination.is_symlink() or (
            destination.exists() and not destination.is_dir()
        ):
            raise WindowsPipelineError(
                f"Windows Git tool destination is unsafe: {destination}"
            )
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            checkout,
            destination,
            ignore=shutil.ignore_patterns(".git"),
        )
        marker = destination / ".chromium-windows-i686-git.json"
        marker.write_text(
            json.dumps(asdict(pin), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    gperf = source / "third_party/gperf/bin/gperf.exe"
    perl = source / "third_party/perl/perl/bin/perl.exe"
    dxheader = source / "third_party/microsoft_dxheaders/src/include/directx/d3d12.h"
    webauthn = source / "third_party/microsoft_webauthn/src/webauthn.h"
    for path, label in (
        (gperf, "gperf.exe"),
        (perl, "perl.exe"),
        (dxheader, "DirectX d3d12.h"),
        (webauthn, "Microsoft webauthn.h"),
    ):
        if not path.is_file() or path.is_symlink():
            raise WindowsPipelineError(
                f"Chromium-pinned Windows Git dependency omitted {label}: {path}"
            )
    _run([str(gperf), "--version"], cwd=source, env=env, timeout=120)
    _run([str(perl), "-v"], cwd=source, env=env, timeout=120)
    print(
        "Validated source-declared Windows Git tool descriptors: "
        f"SHA-256 {descriptor_sha}"
    )
    return descriptor_sha, pins


def materialize_chromium_revision_metadata(
    source: Path,
    depot_python: Path,
    env: Mapping[str, str],
    tag_identity: ChromiumTagIdentity,
) -> int:
    commit = validate_sha1(tag_identity.commit, "Chromium tag commit")
    commit_position = validate_chromium_commit_position(
        tag_identity.commit_position, "Chromium tag commit position"
    )
    commit_timestamp = validate_chromium_timestamp(
        tag_identity.timestamp, "Chromium tag commit timestamp"
    )
    timestamp_script = source / "build/compute_build_timestamp.py"
    if not timestamp_script.is_file() or timestamp_script.is_symlink():
        raise WindowsPipelineError(
            "Chromium source omitted its regular build timestamp calculator"
        )
    util = source / "build/util"
    util.mkdir(parents=True, exist_ok=True)
    if not util.is_dir() or util.is_symlink():
        raise WindowsPipelineError("Chromium build/util is not a regular directory")
    lastchange = util / "LASTCHANGE"
    committime = util / "LASTCHANGE.committime"
    for path in (lastchange, committime):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WindowsPipelineError(
                f"Refusing unsafe Chromium revision metadata path: {path}"
            )
    lastchange.write_text(
        f"LASTCHANGE={commit}-{commit_position}\n",
        encoding="utf-8",
    )
    committime.write_text(f"{commit_timestamp}\n", encoding="utf-8")
    result = _run(
        [str(depot_python), str(timestamp_script), "default"],
        cwd=source,
        env=env,
        timeout=120,
        capture=True,
    )
    windows_build_timestamp = validate_chromium_timestamp(
        (result.stdout or "").strip(), "Chromium Windows build timestamp"
    )
    if windows_build_timestamp > commit_timestamp:
        raise WindowsPipelineError(
            "Chromium Windows build timestamp is newer than its authoritative tag commit"
        )
    print(
        "Materialized Chromium tag revision metadata and validated Windows "
        f"/TIMESTAMP:{windows_build_timestamp}"
    )
    return windows_build_timestamp


def install_source_declared_tools(
    source: Path,
    work_root: Path,
    depot_tools: Path,
    pins: Mapping[str, str],
    env: Mapping[str, str],
    *,
    tag_identity: ChromiumTagIdentity,
) -> tuple[
    Path,
    Path,
    str,
    str,
    tuple[GcsObjectPin, ...],
    str,
    tuple[CipdPackagePin, ...],
    str,
    tuple[GitDependencyPin, ...],
    int,
]:
    cipd = depot_tools / "cipd.bat"
    gn_root = work_root / "gn"
    gn_root.mkdir(exist_ok=True)
    _run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call",
            str(cipd),
            "install",
            "gn/gn/windows-amd64",
            pins["gn_version"],
            "-root",
            str(gn_root),
            "-log-level",
            "warning",
        ],
        env=env,
        timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
    )
    gn = gn_root / "gn.exe"
    if not gn.is_file():
        raise WindowsPipelineError("Chromium-pinned GN CIPD install omitted gn.exe")
    _run([str(gn), "--version"], env=env, timeout=60)

    # The official source tarball already contains a populated
    # third_party/ninja directory, so it cannot become a fresh CIPD site root.
    # Install the exact DEPS pin beside GN under the marked short work root and
    # invoke that executable directly during every compiler slice.
    ninja_root = work_root / "ninja"
    ninja_root.mkdir(exist_ok=True)
    ninja_package = pins["ninja_package"] + "windows-amd64"
    _run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call",
            str(cipd),
            "install",
            ninja_package,
            pins["ninja_version"],
            "-root",
            str(ninja_root),
            "-log-level",
            "warning",
        ],
        env=env,
        timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
    )
    ninja = ninja_root / "ninja.exe"
    if not ninja.is_file():
        raise WindowsPipelineError("Chromium-pinned Ninja CIPD install omitted ninja.exe")
    _run([str(ninja), "--version"], env=env, timeout=60)

    cpython_root = work_root / "cpython3-host"
    cpython_root.mkdir(exist_ok=True)
    _run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call",
            str(cipd),
            "install",
            "infra/3pp/tools/cpython3/windows-amd64",
            pins["cpython3_version"],
            "-root",
            str(cpython_root),
            "-log-level",
            "warning",
        ],
        env=env,
        timeout=DEFAULT_NETWORK_TIMEOUT_SECONDS,
    )
    cpython_executable = cpython_root / "bin/python3.exe"
    if not cpython_executable.is_file():
        raise WindowsPipelineError(
            "Chromium-pinned CPython CIPD install omitted bin/python3.exe"
        )
    _run([str(cpython_executable), "--version"], env=env, timeout=60)
    cpython_target = source / "third_party/cpython3/host"
    cpython_target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cpython_root, cpython_target, dirs_exist_ok=True)
    target_python_exe = cpython_target / "bin/python3.exe"
    if not target_python_exe.is_file():
        raise InfrastructureError(
            "Could not materialize source-declared CPython at Chromium's GN path"
        )
    # concurrent_links.gni runs before GN's host executable suffix is
    # initialized and therefore asks Windows for bin/python3 without '.exe'.
    # Preserve the exact pinned PE bytes under that extensionless path.
    target_python = cpython_target / "bin/python3"
    shutil.copy2(target_python_exe, target_python)
    if sha256_file(target_python) != sha256_file(target_python_exe):
        raise InfrastructureError(
            "Extensionless Chromium CPython launcher differs from pinned python3.exe"
        )
    _run([str(target_python), "--version"], env=env, timeout=60)

    windows_gcs_tools_sha256, windows_gcs_pins = install_windows_gcs_tools(
        source, work_root, depot_tools, env
    )
    windows_cipd_tools_sha256, windows_cipd_pins = install_windows_cipd_tools(
        source, depot_tools, env
    )
    windows_git_tools_sha256, windows_git_pins = install_windows_git_tools(
        source, work_root, env
    )

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

    windows_build_timestamp = materialize_chromium_revision_metadata(
        source,
        depot_python,
        env,
        tag_identity,
    )
    return (
        gn,
        ninja,
        clang_revision,
        windows_gcs_tools_sha256,
        windows_gcs_pins,
        windows_cipd_tools_sha256,
        windows_cipd_pins,
        windows_git_tools_sha256,
        windows_git_pins,
        windows_build_timestamp,
    )


PORT_CONFIG_FILES = (
    "scripts/chromium_tool_pins.py",
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


def _validate_windows_linker_timestamp_text(
    text: str, expected_timestamp: int, label: str
) -> int:
    expected_timestamp = validate_chromium_timestamp(
        expected_timestamp, "expected Windows linker timestamp"
    )
    marker_count = text.lower().count("/timestamp:")
    matches = re.findall(r"(?i)/TIMESTAMP:([^\s\"']{1,100})", text)
    if len(matches) != marker_count:
        raise WindowsPipelineError(
            f"{label} contains a malformed Windows /TIMESTAMP token"
        )
    timestamps = set(matches)
    if timestamps != {str(expected_timestamp)}:
        rendered = sorted(value[:100] for value in timestamps)
        raise WindowsPipelineError(
            f"{label} does not contain only the validated Windows "
            f"/TIMESTAMP:{expected_timestamp}; observed={rendered}"
        )
    return marker_count


def validate_generated_chrome_linker_timestamp(
    source: Path,
    out: Path,
    gn: Path,
    env: Mapping[str, str],
    expected_timestamp: int,
) -> dict[str, int | str]:
    expected_timestamp = validate_chromium_timestamp(
        expected_timestamp, "expected generated Chrome linker timestamp"
    )
    dependency_result = _run(
        [
            str(gn),
            "desc",
            str(out),
            "//chrome",
            "deps",
            "--type=executable",
            "--as=label",
            "--default-toolchain",
        ],
        cwd=source,
        env=env,
        timeout=600,
        capture=True,
    )
    labels = sorted(
        {
            line.strip()
            for line in (dependency_result.stdout or "").splitlines()
            if line.strip()
        }
    )
    if not labels or len(labels) > 32:
        raise WindowsPipelineError(
            "Chrome group has no bounded direct executable dependency set"
        )
    for label in labels:
        if (
            not GN_TARGET_LABEL_RE.fullmatch(label)
            or ".." in label
            or "//" in label[2:]
        ):
            raise WindowsPipelineError(
                f"GN returned an unsafe Chrome executable dependency label: {label!r}"
            )
    chrome_outputs: list[tuple[str, str]] = []
    for label in labels:
        output_result = _run(
            [str(gn), "desc", str(out), label, "outputs"],
            cwd=source,
            env=env,
            timeout=600,
            capture=True,
        )
        for raw_output in (output_result.stdout or "").splitlines():
            output = raw_output.strip()
            normalized = output.replace("\\", "/").lower()
            if normalized == "chrome.exe" or normalized.endswith("/chrome.exe"):
                chrome_outputs.append((label, output))
    if len(chrome_outputs) != 1:
        raise WindowsPipelineError(
            "Could not resolve exactly one Chrome executable from the generated "
            f"group dependencies: {chrome_outputs}"
        )
    chrome_label, chrome_output = chrome_outputs[0]
    flags_result = _run(
        [str(gn), "desc", str(out), chrome_label, "ldflags"],
        cwd=source,
        env=env,
        timeout=600,
        capture=True,
    )
    timestamp_occurrences = _validate_windows_linker_timestamp_text(
        flags_result.stdout or "",
        expected_timestamp,
        f"generated linker flags for {chrome_label}",
    )
    stats = {
        "chrome_executable_dependency_count": len(labels),
        "chrome_executable_label": chrome_label,
        "chrome_executable_output": chrome_output,
        "timestamp_occurrences": timestamp_occurrences,
        "windows_build_timestamp": expected_timestamp,
    }
    print(
        "Resolved the generated Chrome executable without a target-name assumption "
        f"({chrome_label} -> {chrome_output}) and validated "
        f"/TIMESTAMP:{expected_timestamp}"
    )
    return stats


def _gn_generated_output_under_out(source: Path, out: Path, raw: str) -> Path:
    """Translate one resolved GN output into a safe path below the output tree."""
    normalized = raw.strip().replace("\\", "/")
    try:
        out_relative = out.resolve().relative_to(source.resolve()).as_posix()
    except ValueError as exc:
        raise WindowsPipelineError(
            f"GN output directory escapes Chromium source: {out}"
        ) from exc
    if normalized.startswith("//"):
        expected_prefix = f"//{out_relative}/"
        if not normalized.startswith(expected_prefix):
            raise WindowsPipelineError(
                f"GN generated output escapes {out_relative!r}: {raw!r}"
            )
        normalized = normalized[len(expected_prefix) :]
    elif normalized.startswith(f"{out_relative}/"):
        normalized = normalized[len(out_relative) + 1 :]
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "gen"
        or ".." in relative.parts
        or any(":" in part for part in relative.parts)
    ):
        raise WindowsPipelineError(f"Unsafe GN generated output: {raw!r}")
    destination = out.joinpath(*relative.parts)
    resolved_out = out.resolve()
    resolved_destination = destination.resolve()
    if resolved_out not in resolved_destination.parents:
        raise WindowsPipelineError(f"GN generated output escapes build root: {raw!r}")
    return destination


def validate_dawn_source_generator(
    source: Path,
    out: Path,
    gn: Path,
    ninja: Path,
    env: Mapping[str, str],
) -> dict[str, object]:
    """Execute the real Dawn host generator that a Ninja dry-run cannot probe."""
    result = _run(
        [str(gn), "desc", str(out), DAWN_GENERATOR_LABEL, "outputs"],
        cwd=source,
        env=env,
        timeout=600,
        capture=True,
    )
    outputs = [
        _gn_generated_output_under_out(source, out, line)
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]
    if not outputs:
        raise WindowsPipelineError(
            f"GN returned no outputs for required Dawn generator {DAWN_GENERATOR_LABEL}"
        )
    folded = [path.relative_to(out).as_posix().casefold() for path in outputs]
    if len(folded) != len(set(folded)):
        raise WindowsPipelineError(
            "Dawn generator outputs contain duplicate or Windows-case-aliased paths"
        )
    target = outputs[0].relative_to(out).as_posix()
    _run(
        [str(ninja), "-C", str(out), target],
        cwd=source,
        env=env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    missing = [
        str(path)
        for path in outputs
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0
    ]
    if missing:
        raise WindowsPipelineError(
            "Dawn generator completed without all declared regular outputs: "
            + ", ".join(missing[:20])
        )
    stats: dict[str, object] = {
        "label": DAWN_GENERATOR_LABEL,
        "ninja_target": target,
        "output_count": len(outputs),
        "validated": True,
    }
    print(
        f"Executed {DAWN_GENERATOR_LABEL} with its source-pinned Windows Go tool "
        f"and validated {len(outputs)} generated outputs"
    )
    return stats


def _validate_configured_gn_graph(
    source: Path,
    out: Path,
    gn: Path,
    env: Mapping[str, str],
    *,
    windows_build_timestamp: int,
    evidence_dir: Path | None = None,
) -> Path:
    windows_build_timestamp = validate_chromium_timestamp(
        windows_build_timestamp, "expected Chromium Windows build timestamp"
    )
    args_gn = out / "args.gn"
    build_ninja = out / "build.ninja"
    for path in (build_ninja, args_gn):
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise WindowsPipelineError(
                f"Configured Windows Ninja graph lacks regular nonempty {path.name}"
            )
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
    timestamp_stats = validate_generated_chrome_linker_timestamp(
        source, out, gn, env, windows_build_timestamp
    )
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args_gn, evidence_dir / "args.gn")
        (evidence_dir / "gn-targets.json").write_text(
            json.dumps(queries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence_dir / "windows-linker-timestamp.json").write_text(
            json.dumps(timestamp_stats, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return out


def configure_gn(
    source: Path,
    gn: Path,
    env: Mapping[str, str],
    *,
    windows_build_timestamp: int,
    evidence_dir: Path | None = None,
) -> Path:
    out = source / "out" / OUT_NAME
    out.mkdir(parents=True, exist_ok=True)
    args_gn = out / "args.gn"
    args_gn.write_text(WINDOWS_GN_ARGS, encoding="utf-8", newline="\n")
    _run(
        [str(gn), "gen", str(out)],
        cwd=source,
        env=env,
        timeout=DEFAULT_TOOLCHAIN_TIMEOUT_SECONDS,
    )
    return _validate_configured_gn_graph(
        source,
        out,
        gn,
        env,
        windows_build_timestamp=windows_build_timestamp,
        evidence_dir=evidence_dir,
    )


def reuse_restored_gn_graph(
    source: Path,
    gn: Path,
    ninja: Path,
    env: Mapping[str, str],
    *,
    visual_studio: Path,
    windows_build_timestamp: int,
    evidence_dir: Path | None = None,
) -> Path:
    out = source / "out" / OUT_NAME
    args_gn = out / "args.gn"
    build_ninja = out / "build.ninja"
    for path in (args_gn, build_ninja):
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise WindowsPipelineError(
                f"Restored checkpoint lacks reusable regular nonempty {path.name}"
            )
    if args_gn.read_text(encoding="utf-8") != WINDOWS_GN_ARGS:
        raise WindowsPipelineError(
            "Restored Windows args.gn differs from the exact current GN contract"
        )

    manifest_stats = revalidate_restored_gn_manifest(
        source,
        out,
        visual_studio=visual_studio,
    )

    manifest_probe = _run(
        [str(ninja), "-d", "explain", "-C", str(out), "-n", "build.ninja"],
        cwd=source,
        env=env,
        timeout=600,
        capture=True,
    )
    probe_text = "\n".join(
        part for part in (manifest_probe.stdout or "", manifest_probe.stderr or "") if part
    )
    actionable = [
        line.strip()
        for line in probe_text.splitlines()
        if line.strip()
        and "entering directory" not in line.lower()
        and "no work to do" not in line.lower()
    ]
    if actionable:
        raise WindowsPipelineError(
            "Restored build.ninja is dirty after immutable-input timestamp normalization; "
            "refusing silent graph regeneration: " + " | ".join(actionable[:10])
        )

    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "restored-gn-manifest.json").write_text(
            json.dumps(manifest_stats, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(
        "Reusing build.ninja and args.gn from the restored Windows checkpoint; "
        "the manifest regeneration target is clean"
    )
    return _validate_configured_gn_graph(
        source,
        out,
        gn,
        env,
        windows_build_timestamp=windows_build_timestamp,
        evidence_dir=evidence_dir,
    )


def _parse_restored_gn_depfile(path: Path) -> list[str]:
    if not path.is_file() or path.is_symlink():
        raise WindowsPipelineError(
            "Restored checkpoint lacks regular build.ninja.d regeneration metadata"
        )
    size = path.stat().st_size
    if size <= 0 or size > MAX_GN_REGEN_DEPFILE_BYTES:
        raise WindowsPipelineError(
            "Restored build.ninja.d exceeds the bounded regeneration metadata policy"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WindowsPipelineError(
            f"Could not read restored build.ninja.d: {exc}"
        ) from exc
    if "\0" in text:
        raise WindowsPipelineError("Restored build.ninja.d contains a NUL byte")
    # GN emits a Make-style depfile. Join bounded continuations before shlex
    # consumes its backslash-escaped spaces (for example Program\ Files).
    rendered = text.replace("\\\r\n", "").replace("\\\n", "").strip()
    target, separator, dependencies = rendered.partition(": ")
    if separator != ": " or target != "build.ninja.stamp":
        raise WindowsPipelineError(
            "Restored build.ninja.d does not describe build.ninja.stamp"
        )
    try:
        tokens = shlex.split(dependencies, posix=True)
    except ValueError as exc:
        raise WindowsPipelineError(
            f"Restored build.ninja.d has malformed escaping: {exc}"
        ) from exc
    if not tokens or len(tokens) > MAX_GN_REGEN_DEPENDENCIES:
        raise WindowsPipelineError(
            "Restored build.ninja.d dependency count is empty or unbounded"
        )
    folded = [token.replace("\\", "/").casefold() for token in tokens]
    if len(folded) != len(set(folded)):
        raise WindowsPipelineError(
            "Restored build.ninja.d contains duplicate or Windows-case-aliased inputs"
        )
    return tokens


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def revalidate_restored_gn_manifest(
    source: Path,
    out: Path,
    *,
    visual_studio: Path,
    windows_kits_root: Path = WINDOWS_KITS_ROOT,
) -> dict[str, object]:
    """Revalidate and advance only GN's restored zero-byte manifest stamp.

    GN records Visual Studio and Windows Kits directory mtimes in build.ninja.d.
    Those exact-version directories are recreated on each hosted runner, so a
    checkpoint's stamp is necessarily older even when its graph is still exact.
    The checkpoint manifest has already bound source, tools, SDK, VS, and port
    configuration before this function is called. This additional check keeps
    every depfile input inside those authorities and touches no compiled output.
    """
    source = source.resolve()
    out = out.resolve()
    visual_studio = visual_studio.resolve()
    windows_kits_root = windows_kits_root.resolve()
    netfx_sdk_root = (windows_kits_root.parent / "NETFXSDK").resolve()
    if not _is_descendant(out, source):
        raise WindowsPipelineError("Restored GN output is outside the Chromium source")
    for root, label in (
        (visual_studio, "Visual Studio"),
        (windows_kits_root, "Windows Kits"),
    ):
        if not root.is_dir() or root.is_symlink():
            raise WindowsPipelineError(
                f"Trusted {label} root is unavailable for GN manifest revalidation: {root}"
            )

    args_gn = out / "args.gn"
    build_ninja = out / "build.ninja"
    depfile = out / "build.ninja.d"
    stamp = out / "build.ninja.stamp"
    for path, allow_empty in (
        (args_gn, False),
        (build_ninja, False),
        (depfile, False),
        (stamp, True),
    ):
        if (
            not path.is_file()
            or path.is_symlink()
            or (not allow_empty and path.stat().st_size <= 0)
        ):
            raise WindowsPipelineError(
                f"Restored checkpoint lacks reusable regular {path.name}"
            )

    header = "\n".join(build_ninja.read_text(encoding="utf-8").splitlines()[:32])
    required_lines = (
        "rule gn",
        "  command = ../../../gn/gn.exe --root=../.. -q --regeneration gen .",
        "build build.ninja.stamp: gn",
        "  depfile = build.ninja.d",
        "build build.ninja: phony build.ninja.stamp",
    )
    missing_lines = [line for line in required_lines if line not in header]
    if missing_lines:
        raise WindowsPipelineError(
            "Restored build.ninja lacks the expected bounded GN regeneration contract: "
            + ", ".join(missing_lines)
        )

    dependencies = _parse_restored_gn_depfile(depfile)
    trusted_external_roots = [visual_studio, windows_kits_root]
    if netfx_sdk_root.is_dir() and not netfx_sdk_root.is_symlink():
        trusted_external_roots.append(netfx_sdk_root)
    source_count = 0
    external_count = 0
    newest_dependency_mtime_ns = args_gn.stat().st_mtime_ns
    for token in dependencies:
        raw = Path(token)
        if raw.is_absolute():
            resolved = raw.resolve()
            if not any(
                _is_descendant(resolved, root) for root in trusted_external_roots
            ):
                raise WindowsPipelineError(
                    f"Restored GN depfile contains an untrusted absolute input: {token}"
                )
            if not resolved.is_dir() or resolved.is_symlink():
                raise WindowsPipelineError(
                    f"Restored GN external input is not a trusted regular directory: {token}"
                )
            external_count += 1
        else:
            resolved = (out / raw).resolve()
            if not _is_descendant(resolved, source):
                raise WindowsPipelineError(
                    f"Restored GN depfile input escapes the Chromium source: {token}"
                )
            if not resolved.exists() and not resolved.is_symlink():
                raise WindowsPipelineError(
                    f"Restored GN depfile input is unavailable: {token}"
                )
            source_count += 1
        newest_dependency_mtime_ns = max(
            newest_dependency_mtime_ns,
            resolved.stat().st_mtime_ns,
        )

    if external_count <= 0:
        raise WindowsPipelineError(
            "Restored Windows GN depfile contains no trusted external toolchain directories"
        )
    now_ns = time.time_ns()
    future_limit_ns = now_ns + MAX_GN_REGEN_FUTURE_SKEW_SECONDS * 1_000_000_000
    if newest_dependency_mtime_ns > future_limit_ns:
        raise WindowsPipelineError(
            "Restored GN dependency mtime is implausibly far in the future"
        )
    before_ns = stamp.stat().st_mtime_ns
    refreshed = before_ns <= newest_dependency_mtime_ns
    if refreshed:
        wanted_ns = max(now_ns, newest_dependency_mtime_ns + 1_000_000_000)
        os.utime(
            stamp,
            ns=(stamp.stat().st_atime_ns, wanted_ns),
        )
    after_ns = stamp.stat().st_mtime_ns
    if after_ns <= newest_dependency_mtime_ns:
        raise WindowsPipelineError(
            "Could not advance restored build.ninja.stamp beyond validated dependencies"
        )

    stats: dict[str, object] = {
        "dependency_count": len(dependencies),
        "source_dependency_count": source_count,
        "external_directory_count": external_count,
        "trusted_external_root_count": len(trusted_external_roots),
        "stamp_mtime_before_ns": before_ns,
        "stamp_mtime_after_ns": after_ns,
        "newest_dependency_mtime_ns": newest_dependency_mtime_ns,
        "stamp_refreshed": refreshed,
        "build_ninja_sha256": sha256_file(build_ninja),
        "build_ninja_depfile_sha256": sha256_file(depfile),
        "args_gn_sha256": sha256_file(args_gn),
    }
    print(
        "Revalidated restored Windows GN manifest without regenerating its graph: "
        f"dependencies={len(dependencies)}; source={source_count}; "
        f"external_directories={external_count}; stamp_refreshed={str(refreshed).lower()}; "
        f"stamp_mtime_ns={before_ns}->{after_ns}"
    )
    return stats


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
    validate_sha1(state.chromium_commit, "prepared Chromium tag commit")
    validate_chromium_commit_position(
        state.chromium_commit_position, "prepared Chromium tag commit position"
    )
    chromium_commit_timestamp = validate_chromium_timestamp(
        state.chromium_commit_timestamp, "prepared Chromium tag commit timestamp"
    )
    windows_build_timestamp = validate_chromium_timestamp(
        state.windows_build_timestamp, "prepared Chromium Windows build timestamp"
    )
    if windows_build_timestamp > chromium_commit_timestamp:
        raise WindowsPipelineError(
            "Prepared Chromium Windows build timestamp is newer than the tag commit"
        )
    validate_sha256(
        state.windows_cipd_tools_sha256,
        "prepared Windows CIPD tool descriptor SHA-256",
    )
    validate_sha256(
        state.windows_gcs_tools_sha256,
        "prepared Windows GCS tool descriptor SHA-256",
    )
    validate_sha256(
        state.windows_git_tools_sha256,
        "prepared Windows Git tool descriptor SHA-256",
    )
    if state.port_config_hash_schema != PORT_CONFIG_HASH_SCHEMA:
        raise WindowsPipelineError(
            "Prepared port configuration hash schema is incompatible"
        )
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
    tag_identity = fetch_gitiles_tag_identity(version)
    exact = {
        "manifest_schema": "5",
        "version": version,
        "target_cpu": "x86",
        "target_os": "win",
        "source_tarball": source_download_url(version),
        "package_sha256": package_sha,
        "chromium_commit": tag_identity.commit,
        "chromium_commit_position": tag_identity.commit_position,
        "chromium_commit_timestamp": str(tag_identity.timestamp),
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
    windows_build_timestamp = validate_chromium_timestamp(
        fields.get("windows_build_timestamp", ""),
        "manifest Chromium Windows build timestamp",
    )
    if windows_build_timestamp > tag_identity.timestamp:
        raise WindowsPipelineError(
            "Manifest Chromium Windows build timestamp is newer than the tag commit"
        )
    for key in (
        "source_tar_sha256",
        "port_config_sha256",
        "windows_cipd_tools_sha256",
        "windows_gcs_tools_sha256",
        "windows_git_tools_sha256",
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
    if not re.fullmatch(
        r"version:[1-9][0-9]*@[A-Za-z0-9._+-]+",
        fields.get("cpython3_version", ""),
    ):
        raise WindowsPipelineError("Release manifest CPython 3 CIPD pin is absent or mutable")
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
    *,
    migration: CheckpointMigration | None = None,
) -> CheckpointCompatibility:
    exact = {
        "schema": CHECKPOINT_MANIFEST_SCHEMA,
        "checkpoint_contract_version": CHECKPOINT_CONTRACT_VERSION,
        "target_os": "win",
        "target_cpu": "x86",
        "output_root": OUT_NAME,
        "version": state.version,
        "source_sha256": state.source_sha256,
        "chromium_commit": state.chromium_commit,
        "chromium_commit_position": state.chromium_commit_position,
        "chromium_commit_timestamp": state.chromium_commit_timestamp,
        "windows_build_timestamp": state.windows_build_timestamp,
        "depot_tools_revision": state.depot_tools_revision,
        "gn_version": state.gn_version,
        "ninja_package": state.ninja_package,
        "ninja_version": state.ninja_version,
        "cpython3_version": state.cpython3_version,
        "windows_cipd_tools_sha256": state.windows_cipd_tools_sha256,
        "windows_gcs_tools_sha256": state.windows_gcs_tools_sha256,
        "windows_git_tools_sha256": state.windows_git_tools_sha256,
        "clang_revision": state.clang_revision,
        "sdk_family": state.sdk_family,
        "visual_studio_year": state.visual_studio_year,
        "resume_input_epoch": (
            migration.resume_input_epoch
            if migration and migration.resume_input_epoch is not None
            else WINDOWS_RESUME_INPUT_EPOCH
        ),
        "port_config_hash_schema": state.port_config_hash_schema,
        "port_config_sha256": (
            migration.port_config_sha256 if migration else state.port_config_sha256
        ),
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
    refresh_expected = {
        "sdk_servicing": state.sdk_servicing,
        "visual_studio_version": state.visual_studio_version,
    }
    if os.environ.get("RUNNER_OS", "").casefold() == "windows":
        for manifest_field, environment_field in (
            ("runner_image", "ImageOS"),
            ("runner_image_version", "ImageVersion"),
        ):
            current = os.environ.get(environment_field, "")
            if current:
                refresh_expected[manifest_field] = current
    refresh_fields = tuple(
        key
        for key, expected in refresh_expected.items()
        if manifest.get(key) != expected
    )
    if migration and refresh_fields:
        raise WindowsPipelineError(
            "Approved checkpoint migration cannot cross runner toolchain drift: "
            + ", ".join(refresh_fields)
        )
    return CheckpointCompatibility(
        no_progress_streak=streak,
        requires_gn_refresh=bool(refresh_fields),
        gn_refresh_fields=refresh_fields,
        migration_run_id=migration.run_id if migration else "",
    )


def validate_checkpoint_bundle(
    directory: Path,
    *,
    state: PreparedState,
    proof: Mapping[str, object],
    migration: CheckpointMigration | None = None,
) -> tuple[Path, CheckpointCompatibility]:
    archive, checksum, manifest_path = _checkpoint_expected_files(directory)
    for path in (archive, checksum, manifest_path):
        if not path.is_file() or path.is_symlink():
            raise WindowsPipelineError(f"Checkpoint artifact lacks regular file: {path.name}")
    manifest = _read_json_object(manifest_path, "checkpoint manifest")
    compatibility = _checkpoint_manifest_matches_state(
        manifest,
        state,
        proof,
        migration=migration,
    )
    archive_sha = sha256_file(archive)
    expected_sha = validate_sha256(str(manifest.get("archive_sha256", "")), "checkpoint archive SHA-256")
    if archive_sha != expected_sha:
        raise WindowsPipelineError(
            f"Checkpoint archive SHA-256 mismatch: expected {expected_sha}, got {archive_sha}"
        )
    if migration and archive_sha != migration.archive_sha256:
        raise WindowsPipelineError(
            "Approved checkpoint migration archive SHA-256 is not the exact allowlisted object"
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
    return archive, compatibility


def _resolve_checkpoint_migration(
    run_id: str,
    *,
    version: str,
    stage: int,
) -> CheckpointMigration | None:
    migration = APPROVED_CHECKPOINT_MIGRATIONS.get(run_id)
    if migration is None:
        return None
    if migration.run_id != run_id or migration.version != version or migration.stage != stage:
        raise WindowsPipelineError(
            f"Checkpoint run {run_id} does not match its exact approved migration scope"
        )
    validate_sha1(migration.producer_sha, "approved migration producer SHA")
    validate_sha256(
        migration.port_config_sha256,
        "approved migration port configuration SHA-256",
    )
    validate_sha256(
        migration.archive_sha256,
        "approved migration archive SHA-256",
    )
    if migration.resume_input_epoch is not None:
        bounded_int(
            migration.resume_input_epoch,
            "approved migration resume input epoch",
            minimum=0,
            maximum=int(time.time()),
        )
    return migration


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
) -> tuple[Path | None, CheckpointCompatibility]:
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
            migration = _resolve_checkpoint_migration(
                run_id,
                version=version,
                stage=producer_stage,
            )
            proof = verify_checkpoint_run(
                repository=repository,
                run_id=run_id,
                version=version,
                expected_stage=producer_stage,
                expected_ref=expected_ref,
                expected_sha=(migration.producer_sha if migration else expected_sha),
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
            archive, compatibility = validate_checkpoint_bundle(
                candidate_dir,
                state=state,
                proof=proof,
                migration=migration,
            )
        except InfrastructureError:
            raise
        except (OSError, WindowsPipelineError, ValueError) as exc:
            print(f"::warning::Rejected {label} checkpoint from run {run_id}: {exc}")
            shutil.rmtree(candidate_dir, ignore_errors=True)
            continue
        print(
            f"Accepted {label} checkpoint from run {run_id}, stage {producer_stage}, "
            f"no-progress streak {compatibility.no_progress_streak}, "
            f"GN refresh required={str(compatibility.requires_gn_refresh).lower()}, "
            f"approved migration={str(bool(compatibility.migration_run_id)).lower()}"
        )
        return archive, compatibility
    return None, CheckpointCompatibility(0, False, ())


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
        "chromium_commit": state.chromium_commit,
        "chromium_commit_position": state.chromium_commit_position,
        "chromium_commit_timestamp": state.chromium_commit_timestamp,
        "windows_build_timestamp": state.windows_build_timestamp,
        "depot_tools_revision": state.depot_tools_revision,
        "gn_version": state.gn_version,
        "ninja_package": state.ninja_package,
        "ninja_version": state.ninja_version,
        "cpython3_version": state.cpython3_version,
        "windows_cipd_tools_sha256": state.windows_cipd_tools_sha256,
        "windows_gcs_tools_sha256": state.windows_gcs_tools_sha256,
        "windows_git_tools_sha256": state.windows_git_tools_sha256,
        "clang_revision": state.clang_revision,
        "sdk_family": state.sdk_family,
        "sdk_servicing": state.sdk_servicing,
        "visual_studio_year": state.visual_studio_year,
        "visual_studio_version": state.visual_studio_version,
        "resume_input_epoch": WINDOWS_RESUME_INPUT_EPOCH,
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


def _ninja_database_identity(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False}
    if not path.is_file() or path.is_symlink():
        raise WindowsPipelineError(
            f"Ninja progress database is not a regular file: {path}"
        )
    return {
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def validate_ninja_input_closure(
    source: Path,
    out: Path,
    ninja: Path,
    env: Mapping[str, str],
) -> dict[str, object]:
    """Traverse target inputs without simulating or mutating the resumed build.

    A normal ``ninja -n`` schedules every unfinished edge and can fail while
    creating response files or pretending that generated outputs completed. It
    is therefore not a read-only checkpoint validator. Ninja's ``inputs`` and
    ``missingdeps`` tools parse the same manifest closure, while ``missingdeps``
    also loads and checks the dependency database. ``-n`` prevents the log-open
    path from recompacting either database. Exact before/after identities make
    preserving completed work an enforced invariant rather than an assumption.
    """
    targets = ["chrome", "mini_installer"]
    database_paths = (out / ".ninja_log", out / ".ninja_deps")
    before = {
        path.name: _ninja_database_identity(path) for path in database_paths
    }
    try:
        input_probe = _run(
            [
                str(ninja),
                "-C",
                str(out),
                "-n",
                "-t",
                "inputs",
                *targets,
            ],
            cwd=source,
            env=env,
            timeout=1200,
            capture=True,
        )
        input_text = input_probe.stdout or ""
        input_payload = input_text.encode("utf-8")
        input_lines = input_text.splitlines()
        if input_probe.stderr and input_probe.stderr.strip():
            raise WindowsPipelineError(
                "Ninja input-closure traversal emitted an unexpected diagnostic: "
                + input_probe.stderr.strip()[-8000:]
            )
        if (
            not input_lines
            or len(input_lines) > MAX_NINJA_INPUT_CLOSURE_COUNT
            or len(input_payload) > MAX_NINJA_INPUT_CLOSURE_BYTES
            or any(
                not line or "\0" in line or len(line) > 32_768
                for line in input_lines
            )
            or len(input_lines) != len(set(input_lines))
        ):
            raise WindowsPipelineError(
                "Ninja input-closure traversal was empty, duplicated, malformed, or unbounded"
            )

        missing_probe = _run(
            [
                str(ninja),
                "-C",
                str(out),
                "-n",
                "-t",
                "missingdeps",
                *targets,
            ],
            cwd=source,
            env=env,
            timeout=1200,
            capture=True,
        )
        missing_text = "\n".join(
            part.strip()
            for part in (missing_probe.stdout or "", missing_probe.stderr or "")
            if part.strip()
        )
        missing_match = re.fullmatch(
            r"Processed ([1-9][0-9]*) nodes\.\s*"
            r"No missing dependencies on generated files found\.",
            missing_text,
        )
        if not missing_match:
            raise WindowsPipelineError(
                "Ninja missing-dependency validation did not produce its exact clean "
                f"summary: {missing_text[-8000:]}"
            )
    finally:
        after = {
            path.name: _ninja_database_identity(path) for path in database_paths
        }
        if after != before:
            raise WindowsPipelineError(
                "Read-only Ninja closure validation changed a progress database; "
                "refusing to continue from mutated checkpoint state"
            )

    stats: dict[str, object] = {
        "build_simulation": False,
        "dependency_nodes_processed": int(missing_match.group(1)),
        "manifest_input_bytes": len(input_payload),
        "manifest_input_count": len(input_lines),
        "manifest_input_sha256": hashlib.sha256(input_payload).hexdigest(),
        "ninja_databases": before,
        "ninja_tool_dry_run_guard": True,
        "state_unchanged": True,
        "targets": targets,
        "validated": True,
    }
    print(
        "Read-only Ninja tools validated the chrome + mini_installer closure: "
        f"manifest_inputs={len(input_lines)}; "
        f"dependency_nodes={missing_match.group(1)}; progress_databases_unchanged=true"
    )
    return stats


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
    tag_identity = fetch_gitiles_tag_identity(version)
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
    (
        gn,
        ninja,
        clang_revision,
        windows_gcs_tools_sha256,
        windows_gcs_pins,
        windows_cipd_tools_sha256,
        windows_cipd_pins,
        windows_git_tools_sha256,
        windows_git_pins,
        windows_build_timestamp,
    ) = install_source_declared_tools(
        source,
        work_root,
        depot_tools,
        pins,
        env,
        tag_identity=tag_identity,
    )
    # Create the excluded output parent before directory normalization. Later
    # checkpoint staging beneath it must not make the source root look newer
    # than the restored build.ninja regeneration inputs.
    (source / "out").mkdir(parents=True, exist_ok=True)
    resume_input_stats = normalize_windows_resume_inputs(source)
    port_hash = compute_port_config_sha256(repository_root)
    state = PreparedState(
        schema=PREPARED_STATE_SCHEMA,
        version=version,
        source_sha256=source_sha,
        chromium_commit=tag_identity.commit,
        chromium_commit_position=tag_identity.commit_position,
        chromium_commit_timestamp=tag_identity.timestamp,
        windows_build_timestamp=windows_build_timestamp,
        depot_tools_revision=pins["depot_tools_revision"],
        gn_version=pins["gn_version"],
        ninja_package=pins["ninja_package"],
        ninja_version=pins["ninja_version"],
        cpython3_version=pins["cpython3_version"],
        windows_cipd_tools_sha256=windows_cipd_tools_sha256,
        windows_gcs_tools_sha256=windows_gcs_tools_sha256,
        windows_git_tools_sha256=windows_git_tools_sha256,
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

    checkpoint_archive: Path | None = None
    checkpoint_compatibility = CheckpointCompatibility(0, False, ())
    resume_output_rebase_stats: dict[str, int] | None = None
    if preferred_run_id or fallback_run_id:
        if not (repository and expected_ref and expected_sha):
            raise WindowsPipelineError(
                "Checkpoint inputs require repository, expected ref, and immutable lineage SHA"
            )
        checkpoint_archive, checkpoint_compatibility = acquire_checkpoint(
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
        if checkpoint_archive is not None:
            restore_checkpoint(checkpoint_archive, source=source)
            resume_output_rebase_stats = rebase_windows_ninja_unsafe_output_mtimes(
                source / "out" / OUT_NAME
            )
            state = PreparedState(
                **{
                    **asdict(state),
                    "checkpoint_no_progress_streak": (
                        checkpoint_compatibility.no_progress_streak
                    ),
                }
            )
            write_prepared_state(work_root, state)

    if checkpoint_archive is not None and not checkpoint_compatibility.requires_gn_refresh:
        out = reuse_restored_gn_graph(
            source,
            gn,
            ninja,
            env,
            visual_studio=visual_studio,
            windows_build_timestamp=windows_build_timestamp,
            evidence_dir=evidence_dir,
        )
    else:
        if checkpoint_archive is not None:
            print(
                "Refreshing the restored Windows GN graph because runner toolchain "
                "metadata changed: "
                + ", ".join(checkpoint_compatibility.gn_refresh_fields)
            )
        out = configure_gn(
            source,
            gn,
            env,
            windows_build_timestamp=windows_build_timestamp,
            evidence_dir=evidence_dir,
        )
    ninja_closure_stats = validate_ninja_input_closure(
        source,
        out,
        ninja,
        env,
    )
    if evidence_dir is not None:
        (evidence_dir / "resume-input-normalization.json").write_text(
            json.dumps(resume_input_stats, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if resume_output_rebase_stats is not None:
            (evidence_dir / "resume-output-timestamp-rebase.json").write_text(
                json.dumps(
                    resume_output_rebase_stats,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        dawn_generator_stats = validate_dawn_source_generator(
            source, out, gn, ninja, env
        )
        (evidence_dir / "requirements.json").write_text(
            json.dumps(asdict(requirements), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "prepared-state.json").write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "chromium-tag-identity.json").write_text(
            json.dumps(
                {
                    **asdict(tag_identity),
                    "windows_build_timestamp": windows_build_timestamp,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "windows-gcs-tools.json").write_text(
            json.dumps(
                [asdict(pin) for pin in windows_gcs_pins],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "windows-cipd-tools.json").write_text(
            json.dumps(
                [asdict(pin) for pin in windows_cipd_pins],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "windows-git-tools.json").write_text(
            json.dumps(
                [asdict(pin) for pin in windows_git_pins],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "ninja-input-closure.json").write_text(
            json.dumps(ninja_closure_stats, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "dawn-generator.json").write_text(
            json.dumps(dawn_generator_stats, indent=2, sort_keys=True) + "\n",
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
        "GOTOOLCHAIN": "local",
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


def _ninja_log_stats(path: Path) -> tuple[int, int]:
    rows = 0
    outputs: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                rows += 1
                fields = line.rstrip("\r\n").split("\t")
                if len(fields) >= 5 and fields[3]:
                    outputs.add(fields[3].replace("\\", "/").casefold())
    except FileNotFoundError:
        return 0, 0
    return rows, len(outputs)


def _ninja_log_count(path: Path) -> int:
    return _ninja_log_stats(path)[0]


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


def is_empty_ninja_controller_exit(path: Path) -> bool:
    """Recognize only Ninja's empty controller exit without a build diagnostic."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-4_000_000:]
    except OSError:
        return False
    lines = [line.rstrip("\r") for line in text.splitlines()]
    marker = any(
        lines[index].strip() == "ninja: error:"
        and lines[index + 1].strip() == "ninja: build stopped: ."
        for index in range(len(lines) - 1)
    )
    if not marker:
        return False
    explicit_failure = re.compile(
        r"(?i)(?:^FAILED:|\berror:|\bfatal error(?:\s+[A-Z]*[0-9])?|"
        r"\berror\s+(?:C|LNK)[0-9]+)"
    )
    return not any(
        line.strip() != "ninja: error:" and explicit_failure.search(line)
        for line in lines
    )


def normalize_empty_ninja_controller_status(
    status: int,
    *,
    durable_progress: bool,
    build_log: Path,
) -> int:
    """Rotate a diagnostic-free Ninja controller failure into a checkpoint.

    A failed build edge is actionable and must remain fatal: Ninja prints a
    ``FAILED:`` record, the command's diagnostic, and a non-empty stop reason.
    The pinned Windows Ninja can instead return status 1 after an internal
    controller/filesystem-start failure with only the exact empty
    ``ninja: error:`` / ``ninja: build stopped: .`` pair.  That has now occurred
    both at a checkpoint boundary and well before one.  When the durable Ninja
    database gained unique outputs, preserve those outputs and let the next
    bounded stage resume.  If no new unique output was recorded, preserve the
    checkpoint but route the failure through the workflow's bounded fresh-runner
    retry policy.  A real diagnostic or a different status still fails closed.
    """
    if status == 1 and is_empty_ninja_controller_exit(build_log):
        if durable_progress:
            return NINJA_CONTROLLER_ROTATION_EXIT_CODE
        return NINJA_CONTROLLER_RETRY_EXIT_CODE
    return status


def compiler_slice_timeout_seconds(remaining_seconds: int) -> int:
    """Reserve time to own Ninja termination before an outer step boundary.

    The Windows runner can end a long-lived Ninja child immediately before the
    requested checkpoint deadline. In that race Ninja exits 1 with no compiler
    diagnostic, so the wrapper never gets to return its controlled status 124.
    Stop the child five minutes earlier and leave at least ten useful compiler
    minutes; otherwise checkpoint without launching a new slice.
    """
    if (
        not isinstance(remaining_seconds, int)
        or isinstance(remaining_seconds, bool)
        or remaining_seconds < 0
    ):
        raise WindowsPipelineError(
            "remaining compiler budget must be a non-negative integer"
        )
    minimum_budget = (
        MIN_WINDOWS_COMPILER_SLICE_SECONDS
        + WINDOWS_CHECKPOINT_TERMINATION_RESERVE_SECONDS
    )
    if remaining_seconds <= minimum_budget:
        return 0
    return remaining_seconds - WINDOWS_CHECKPOINT_TERMINATION_RESERVE_SECONDS


def _windows_ninja_build_command(ninja: Path, out: Path, jobs: int) -> list[str]:
    """Build with fresh Windows directory stats instead of Ninja's batch cache.

    The diagnostic-free controller failures occur while Ninja is selecting the
    next edge, including immediately after a successful COPY.  Ninja's Windows
    ``nostatcache`` mode avoids reusing batched directory metadata across that
    edge transition while leaving the durable build/dependency logs intact.
    """
    return [
        str(ninja),
        "-d",
        "nostatcache",
        "-C",
        str(out),
        f"-j{jobs}",
        "chrome",
        "mini_installer",
    ]


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
    compiler_timeout = compiler_slice_timeout_seconds(max(0, remaining))
    source = work_root / "src"
    out = source / "out" / OUT_NAME
    ninja = work_root / "ninja/ninja.exe"
    if not ninja.is_file():
        raise InfrastructureError(f"Prepared Ninja executable is unavailable: {ninja}")
    log = work_root / "windows-i686-build.log"
    progress_log = out / ".ninja_log"
    before, unique_before = _ninja_log_stats(progress_log)
    prior_streak = state.checkpoint_no_progress_streak
    result: dict[str, object] = {
        "complete": False,
        "failure_class": "",
        "no_progress_streak": prior_streak,
        "ninja_controller_restarts": 0,
        "ninja_entries_before": before,
        "ninja_entries_after": before,
        "ninja_unique_outputs_before": unique_before,
        "ninja_unique_outputs_after": unique_before,
        "status": 0,
    }
    if compiler_timeout == 0:
        print(
            "::warning::Preparation consumed the usable compiler budget; "
            "checkpointing without starting Ninja"
        )
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
    env["GOTOOLCHAIN"] = "local"
    env["PATH"] = os.pathsep.join(
        (str(depot), str(depot / ".cipd_bin"), env.get("PATH", ""))
    )
    command = _windows_ninja_build_command(ninja, out, jobs)
    stall_marker = work_root / "ninja-stall.marker"
    print(
        "Starting bounded Ninja compiler slice with a reserved checkpoint "
        f"termination margin: timeout_seconds={compiler_timeout}; "
        f"reserve_seconds={WINDOWS_CHECKPOINT_TERMINATION_RESERVE_SECONDS}"
    )
    print(
        "Ninja Windows directory-stat cache is disabled for resumable "
        "compilation (-d nostatcache)"
    )
    compiler_deadline = time.monotonic() + compiler_timeout
    controller_restarts = 0
    while True:
        attempt_timeout = int(compiler_deadline - time.monotonic())
        if attempt_timeout <= 0:
            status = TIMEOUT_EXIT_CODE
            break
        log = work_root / (
            "windows-i686-build.log"
            if controller_restarts == 0
            else f"windows-i686-build-controller-{controller_restarts}.log"
        )
        attempt_rows, attempt_unique = _ninja_log_stats(progress_log)
        try:
            status = run_with_watchdog(
                command,
                progress_log=progress_log,
                stall_seconds=stall_minutes * 60,
                poll_seconds=15,
                kill_grace_seconds=30,
                stall_marker=stall_marker,
                timeout_seconds=attempt_timeout,
                timeout_kill_grace_seconds=120,
                output_log=log,
                cwd=source,
                env=env,
            )
        except WatchdogError as exc:
            raise InfrastructureError(
                f"Ninja watchdog failed internally: {exc}"
            ) from exc
        current_rows, current_unique = _ninja_log_stats(progress_log)
        remaining_after_attempt = int(compiler_deadline - time.monotonic())
        if not (
            status == 1
            and is_empty_ninja_controller_exit(log)
            and controller_restarts < MAX_NINJA_CONTROLLER_RESTARTS_PER_SLICE
            and remaining_after_attempt > MIN_WINDOWS_COMPILER_SLICE_SECONDS
        ):
            break
        controller_restarts += 1
        result["ninja_controller_restarts"] = controller_restarts
        print(
            "::warning::Restarting Ninja on the same prepared output after an "
            "exact diagnostic-free controller exit: "
            f"restart={controller_restarts}/"
            f"{MAX_NINJA_CONTROLLER_RESTARTS_PER_SLICE}; "
            f"rows={attempt_rows}->{current_rows}; "
            f"unique_outputs={attempt_unique}->{current_unique}; "
            f"remaining_seconds={remaining_after_attempt}"
        )
    after, unique_after = _ninja_log_stats(progress_log)
    result["ninja_entries_after"] = after
    result["ninja_unique_outputs_after"] = unique_after
    durable_progress = unique_after > unique_before
    observed_status = status
    status = normalize_empty_ninja_controller_status(
        status,
        durable_progress=durable_progress,
        build_log=log,
    )
    if status != observed_status:
        if status == NINJA_CONTROLLER_ROTATION_EXIT_CODE:
            print(
                "::warning::Rotating an exact diagnostic-free Ninja controller "
                f"exit into checkpoint status {NINJA_CONTROLLER_ROTATION_EXIT_CODE}; "
                "durable progress will be preserved."
            )
        else:
            print(
                "::warning::An exact diagnostic-free Ninja controller exit made "
                "no new unique-output progress; preserving the checkpoint for a "
                "bounded fresh-runner retry."
            )
    result["status"] = status
    if status == NINJA_CONTROLLER_RETRY_EXIT_CODE:
        result["failure_class"] = "infrastructure"
        # Controller retries are bounded by CHROMIUM_WINDOWS_RUNNER_RETRIES.
        # Preserve (rather than increment) the compiler no-progress streak so
        # an interrupted controller is not mislabeled as two healthy slices
        # that ran to their timeout without producing work.
        result["no_progress_streak"] = prior_streak
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(
            json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
        )
        raise InfrastructureError(
            "Diagnostic-free Windows Ninja controller exit requires a bounded "
            "fresh-runner retry"
        )
    if status == 0:
        required = (
            out / "chrome.exe",
            out / "mini_installer.exe",
            out / "chrome.7z",
        )
        missing = [str(path) for path in required if not path.is_file() or path.stat().st_size <= 0]
        if missing:
            result["failure_class"] = "deterministic_build"
            result["no_progress_streak"] = 0 if durable_progress else min(prior_streak + 1, MAX_NO_PROGRESS_STREAK)
            result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            raise WindowsPipelineError(
                "Ninja returned success without required Windows package outputs: "
                + ", ".join(missing)
            )
        result["complete"] = True
        result["no_progress_streak"] = 0
    elif status in {
        TIMEOUT_EXIT_CODE,
        STALL_EXIT_CODE,
        NINJA_CONTROLLER_ROTATION_EXIT_CODE,
    }:
        streak = 0 if durable_progress else min(prior_streak + 1, MAX_NO_PROGRESS_STREAK)
        result["no_progress_streak"] = streak
        if streak >= MAX_NO_PROGRESS_STREAK:
            result["failure_class"] = "deterministic_build"
            result_file.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            raise WindowsPipelineError(
                "Two consecutive Windows compiler slices made no durable Ninja progress"
            )
        print(
            f"Compiler slice rotated at status {status}; Ninja entries {before}->{after}; "
            f"unique outputs {unique_before}->{unique_after}; "
            f"no-progress streak {streak}/{MAX_NO_PROGRESS_STREAK}"
        )
    else:
        result["failure_class"] = classify_build_log(log)
        result["no_progress_streak"] = 0 if durable_progress else min(prior_streak + 1, MAX_NO_PROGRESS_STREAK)
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
        ("manifest_schema", "5"),
        ("version", version),
        ("target_cpu", "x86"),
        ("target_os", "win"),
        ("source_tarball", source_download_url(version)),
        ("source_tar_sha256", state.source_sha256),
        ("chromium_commit", state.chromium_commit),
        ("chromium_commit_position", state.chromium_commit_position),
        ("chromium_commit_timestamp", str(state.chromium_commit_timestamp)),
        ("windows_build_timestamp", str(state.windows_build_timestamp)),
        ("package_sha256", package_sha),
        ("github_sha", github_sha),
        ("github_run_id", run_id),
        ("clang_revision", state.clang_revision),
        ("gn_version", state.gn_version),
        ("ninja_package", state.ninja_package),
        ("ninja_version", state.ninja_version),
        ("cpython3_version", state.cpython3_version),
        ("windows_cipd_tools_sha256", state.windows_cipd_tools_sha256),
        ("windows_gcs_tools_sha256", state.windows_gcs_tools_sha256),
        ("windows_git_tools_sha256", state.windows_git_tools_sha256),
        ("depot_tools_revision", state.depot_tools_revision),
        ("windows_sdk_family", state.sdk_family),
        ("windows_sdk_servicing", state.sdk_servicing),
        ("visual_studio_year", state.visual_studio_year),
        ("visual_studio_version", state.visual_studio_version),
        ("windows_resume_input_epoch", str(WINDOWS_RESUME_INPUT_EPOCH)),
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


def prepared_source_cache_key(cache_dir: Path, version: str) -> str:
    version = validate_version(version)
    tarball = cache_dir / f"chromium-{version}.tar.xz"
    marker = cache_dir / f"chromium-{version}.validated.json"
    metadata_path = cache_dir / f"chromium-{version}.source-object.json"
    stats_path = cache_dir / f"chromium-{version}.source-archive-stats.json"
    for path in (tarball, marker, metadata_path, stats_path):
        if not path.is_file() or path.is_symlink():
            raise WindowsPipelineError(
                f"Prepared source cache lacks trusted regular file {path.name}"
            )
    metadata = _read_json_object(metadata_path, "prepared source metadata")
    validate_source_metadata(version, metadata)
    source_sha = validate_sha256(
        str(metadata.get("sha256", "")), "prepared source SHA-256"
    )
    expected_bytes = metadata.get("content_length")
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool):
        raise WindowsPipelineError("Prepared source metadata content length is malformed")
    if tarball.stat().st_size != expected_bytes:
        raise WindowsPipelineError("Prepared source tarball length changed before cache save")
    if not marker_matches(
        marker, version=version, metadata=metadata, sha256=source_sha
    ):
        raise WindowsPipelineError(
            "Prepared source cache lacks exact safe-archive and Gitiles identity proof"
        )
    if not _source_stats_usable(
        stats_path,
        version=version,
        source_sha256=source_sha,
        max_members=DEFAULT_SOURCE_MAX_MEMBERS,
        max_unpacked_gib=DEFAULT_SOURCE_MAX_UNPACKED_GIB,
    ):
        raise WindowsPipelineError("Prepared source archive stats are absent or stale")
    return source_cache_key(version, metadata)


def write_stage_summary(
    *,
    work_root: Path,
    version: str,
    stage: str,
    attempt: str,
    result_file: Path | None,
) -> None:
    summary_path = _runner_command_file("GITHUB_STEP_SUMMARY", "step_summary_")
    if summary_path is None:
        return
    result: dict[str, object] = {}
    if result_file is not None and result_file.is_file():
        result = _read_json_object(result_file, "build result")
    free = shutil.disk_usage(work_root).free / 1024**3
    with summary_path.open("a", encoding="utf-8", newline="\n") as handle:  # lgtm [py/path-injection]
        handle.write("## Chromium Windows i686 stage summary\n\n")
        handle.write("| Field | Value |\n| --- | --- |\n")
        for key, value in (
            ("Chromium", version),
            ("Stage", stage),
            ("Attempt", attempt),
            ("Complete", str(result.get("complete", "unknown"))),
            ("Failure class", str(result.get("failure_class", "none") or "none")),
            ("Ninja entries", f"{result.get('ninja_entries_before', '?')} -> {result.get('ninja_entries_after', '?')}"),
            (
                "Unique Ninja outputs",
                f"{result.get('ninja_unique_outputs_before', '?')} -> "
                f"{result.get('ninja_unique_outputs_after', '?')}",
            ),
            ("No-progress streak", str(result.get("no_progress_streak", "?"))),
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

    cache_key = subparsers.add_parser("prepared-source-cache-key")
    cache_key.add_argument("--cache-dir", type=Path, required=True)
    cache_key.add_argument("--version", required=True)

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
    elif args.command == "prepared-source-cache-key":
        print(prepared_source_cache_key(args.cache_dir, args.version))
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
