#!/usr/bin/env python3
"""Resolve reproducible host-tool pins from a Chromium source DEPS file."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from pathlib import PurePosixPath

GN_RE = re.compile(r'[\'"]gn_version[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]')
NINJA_PACKAGE_RE = re.compile(r'[\'"]ninja_package[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]')
NINJA_VERSION_RE = re.compile(r'[\'"]ninja_version[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]')
CPYTHON3_VERSION_RE = re.compile(
    r'[\'"]cpython3_version[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]'
)
DEPOT_RE = re.compile(
    r"""
    [\'"]src/third_party/depot_tools[\'"]\s*:\s*
    Var\([\'"]chromium_git[\'"]\)\s*\+\s*
    [\'"]/chromium/tools/depot_tools\.git[\'"]\s*\+\s*
    [\'"]@[\'"]\s*\+\s*
    [\'"]([0-9a-f]{40})[\'"]
    """,
    re.VERBOSE,
)

WINDOWS_GCS_TOOL_DEPENDENCIES = (
    "src/buildtools/win-format",
    "src/third_party/node/win",
    "src/third_party/node/node_modules",
    "src/third_party/rust-toolchain",
    "src/third_party/llvm-libclang",
)
GCS_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
GCS_OBJECT_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
WINDOWS_GCS_BUCKETS = {
    "src/buildtools/win-format": "chromium-clang-format",
    "src/third_party/node/win": "chromium-nodejs",
    "src/third_party/node/node_modules": "chromium-nodejs",
    "src/third_party/rust-toolchain": "chromium-browser-clang",
    "src/third_party/llvm-libclang": "chromium-browser-clang",
}
WINDOWS_GCS_OUTPUT_FILES = {
    "src/buildtools/win-format": "clang-format.exe",
    "src/third_party/node/win": "node.exe",
    "src/third_party/node/node_modules": "node_modules.tar.gz",
}
WINDOWS_CIPD_TOOL_DEPENDENCIES = (
    "src/third_party/typescript/windows-amd64/src",
    "src/third_party/devtools-frontend/src/third_party/esbuild",
    "src/third_party/devtools-frontend/src/third_party/rollup_libs",
)
WINDOWS_CIPD_TOOL_POLICIES = {
    "src/third_party/typescript/windows-amd64/src": {
        "mapping": "src/third_party/typescript/windows-amd64/src",
        "package_template": "chromium/third_party/typescript/windows-amd64",
        "package": "chromium/third_party/typescript/windows-amd64",
        "conditions": {
            "checkout_win and non_git_source",
            "non_git_source and checkout_win",
        },
    },
    "src/third_party/devtools-frontend/src/third_party/esbuild": {
        "mapping": "third_party/esbuild",
        "package_template": "infra/3pp/tools/esbuild/${{platform}}",
        "package": "infra/3pp/tools/esbuild/windows-amd64",
        "conditions": {"non_git_source"},
    },
    "src/third_party/devtools-frontend/src/third_party/rollup_libs": {
        "mapping": "third_party/rollup_libs",
        "package_template": "infra/3pp/tools/rollup_libs/${{platform}}",
        "package": "infra/3pp/tools/rollup_libs/windows-amd64",
        "conditions": {"non_git_source"},
    },
}


@dataclass(frozen=True)
class GcsObjectPin:
    dependency: str
    bucket: str
    object_name: str
    sha256: str
    size_bytes: int
    generation: str
    output_file: str


@dataclass(frozen=True)
class CipdPackagePin:
    dependency: str
    package_template: str
    package: str
    version: str


def _balanced_region(text: str, start: int, opener: str, closer: str) -> str:
    """Return one balanced DEPS mapping/list without evaluating the DEPS file."""
    if start < 0 or start >= len(text) or text[start] != opener:
        raise ValueError(f"Expected {opener!r} at DEPS offset {start}")
    depth = 0
    quote = ""
    escaped = False
    comment = False
    for index in range(start, len(text)):
        character = text[index]
        if comment:
            if character == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character == "#":
            comment = True
        elif character in "'\"":
            quote = character
        elif character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
            if depth < 0:
                break
    raise ValueError(f"Unbalanced {opener}{closer} region in Chromium DEPS")


def _literal_field(mapping: str, field: str) -> str:
    matches = list(re.finditer(rf"['\"]{re.escape(field)}['\"]\s*:\s*", mapping))
    values: list[str] = []
    for match in matches:
        start = match.end()
        if start >= len(mapping) or mapping[start] not in "'\"":
            continue
        quote = mapping[start]
        escaped = False
        for end in range(start + 1, len(mapping)):
            character = mapping[end]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                try:
                    value = ast.literal_eval(mapping[start : end + 1])
                except (SyntaxError, ValueError) as exc:
                    raise ValueError(
                        f"Malformed literal {field!r} field in Chromium DEPS"
                    ) from exc
                if not isinstance(value, str) or not value:
                    raise ValueError(
                        f"Chromium DEPS {field!r} must be a non-empty string"
                    )
                values.append(value)
                break
    if len(values) != 1:
        raise ValueError(
            f"Expected exactly one literal {field!r} field in Chromium DEPS"
        )
    return values[0]


def _integer_field(mapping: str, field: str) -> int:
    matches = re.findall(rf"['\"]{re.escape(field)}['\"]\s*:\s*([0-9]+)", mapping)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one integer {field!r} field in Chromium DEPS"
        )
    return int(matches[0])


def _dependency_mapping(text: str, dependency: str) -> str:
    matches = list(
        re.finditer(
            rf"(?m)^[ \t]+(['\"]){re.escape(dependency)}\1\s*:\s*\{{",
            text,
        )
    )
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {dependency!r} dependency mapping in Chromium DEPS"
        )
    brace = text.find("{", matches[0].start())
    return _balanced_region(text, brace, "{", "}")


def resolve_windows_gcs_object(path: Path, dependency: str) -> GcsObjectPin:
    """Resolve one exact Windows first-class GCS object from Chromium DEPS."""
    if dependency not in WINDOWS_GCS_TOOL_DEPENDENCIES:
        raise ValueError(f"Unsupported Windows GCS tool dependency: {dependency!r}")
    text = path.read_text(encoding="utf-8")
    mapping = _dependency_mapping(text, dependency)
    if _literal_field(mapping, "dep_type") != "gcs":
        raise ValueError(f"Chromium dependency {dependency!r} is no longer first-class GCS")
    bucket = _literal_field(mapping, "bucket")
    expected_bucket = WINDOWS_GCS_BUCKETS[dependency]
    if bucket != expected_bucket or not GCS_BUCKET_RE.fullmatch(bucket):
        raise ValueError(f"Unsupported GCS bucket for {dependency!r}: {bucket!r}")

    objects_match = re.search(r"['\"]objects['\"]\s*:\s*\[", mapping)
    if not objects_match:
        raise ValueError(f"Chromium dependency {dependency!r} lacks a GCS objects list")
    list_start = mapping.find("[", objects_match.start())
    objects_region = _balanced_region(mapping, list_start, "[", "]")
    object_mappings: list[str] = []
    index = 1
    while index < len(objects_region) - 1:
        if objects_region[index] == "{":
            item = _balanced_region(objects_region, index, "{", "}")
            object_mappings.append(item)
            index += len(item)
        else:
            index += 1

    selected: list[GcsObjectPin] = []
    for item in object_mappings:
        object_name = _literal_field(item, "object_name")
        output_file = ""
        if dependency in WINDOWS_GCS_OUTPUT_FILES:
            mapping_head = mapping[:objects_match.start()]
            condition = _literal_field(mapping_head, "condition")
            normalized_condition = re.sub(r"\s+", " ", condition).strip()
            windows_non_git_conditions = {
                'host_os == "win" and non_git_source',
                "host_os == 'win' and non_git_source",
                'non_git_source and host_os == "win"',
                "non_git_source and host_os == 'win'",
            }
            allowed_conditions = (
                {"non_git_source"}
                if dependency == "src/third_party/node/node_modules"
                else windows_non_git_conditions
            )
            if normalized_condition not in allowed_conditions:
                raise ValueError(
                    f"Windows object for {dependency!r} has an unexpected condition: "
                    f"{condition!r}"
                )
            output_file = _literal_field(item, "output_file")
            if output_file != WINDOWS_GCS_OUTPUT_FILES[dependency]:
                raise ValueError(
                    f"Windows GCS tool output path changed in Chromium DEPS: "
                    f"{output_file!r}"
                )
        else:
            if not object_name.startswith("Win/"):
                continue
            condition = _literal_field(item, "condition")
            if not re.fullmatch(r"host_os\s*==\s*(['\"])win\1", condition):
                raise ValueError(
                    f"Windows object for {dependency!r} has an unexpected condition: "
                    f"{condition!r}"
                )
        sha256 = _literal_field(item, "sha256sum").lower()
        size_bytes = _integer_field(item, "size_bytes")
        generation = str(_integer_field(item, "generation"))
        object_path = PurePosixPath(object_name)
        expected_parts = 1 if output_file else 2
        if (
            not GCS_OBJECT_RE.fullmatch(object_name)
            or object_path.is_absolute()
            or len(object_path.parts) != expected_parts
            or ".." in object_path.parts
            or (not output_file and object_path.parts[:1] != ("Win",))
            or (not output_file and not object_name.endswith(".tar.xz"))
        ):
            raise ValueError(f"Unsafe Windows GCS object name: {object_name!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"Mutable or malformed GCS SHA-256 for {dependency!r}")
        if not 1 <= size_bytes <= 2 * 1024**3:
            raise ValueError(
                f"GCS object size is outside the 2 GiB hard bound: {size_bytes}"
            )
        if not re.fullmatch(r"[1-9][0-9]{0,19}", generation):
            raise ValueError(f"Malformed GCS generation for {dependency!r}: {generation!r}")
        selected.append(
            GcsObjectPin(
                dependency=dependency,
                bucket=bucket,
                object_name=object_name,
                sha256=sha256,
                size_bytes=size_bytes,
                generation=generation,
                output_file=output_file,
            )
        )
    if len(selected) != 1:
        raise ValueError(
            f"Expected exactly one unconditional Windows GCS object for {dependency!r}, "
            f"found {len(selected)}"
        )
    return selected[0]


def resolve_windows_gcs_tool_pins(path: Path) -> tuple[GcsObjectPin, ...]:
    return tuple(
        resolve_windows_gcs_object(path, dependency)
        for dependency in WINDOWS_GCS_TOOL_DEPENDENCIES
    )


def windows_gcs_tool_descriptor_sha256(pins: tuple[GcsObjectPin, ...]) -> str:
    if tuple(pin.dependency for pin in pins) != WINDOWS_GCS_TOOL_DEPENDENCIES:
        raise ValueError(
            "Windows GCS tool descriptors are missing or out of canonical order"
        )
    payload = json.dumps(
        [asdict(pin) for pin in pins],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def resolve_windows_cipd_package(path: Path, dependency: str) -> CipdPackagePin:
    if dependency not in WINDOWS_CIPD_TOOL_DEPENDENCIES:
        raise ValueError(f"Unsupported Windows CIPD tool dependency: {dependency!r}")
    policy = WINDOWS_CIPD_TOOL_POLICIES[dependency]
    text = path.read_text(encoding="utf-8")
    mapping = _dependency_mapping(text, str(policy["mapping"]))
    if _literal_field(mapping, "dep_type") != "cipd":
        raise ValueError(f"Chromium dependency {dependency!r} is no longer CIPD")
    condition = _literal_field(mapping, "condition")
    normalized_condition = re.sub(r"\s+", " ", condition).strip()
    if normalized_condition not in policy["conditions"]:
        raise ValueError(
            f"Windows CIPD dependency {dependency!r} has an unexpected condition: "
            f"{condition!r}"
        )
    packages_match = re.search(r"['\"]packages['\"]\s*:\s*\[", mapping)
    if not packages_match:
        raise ValueError(f"Chromium dependency {dependency!r} lacks a CIPD packages list")
    list_start = mapping.find("[", packages_match.start())
    packages_region = _balanced_region(mapping, list_start, "[", "]")
    package_mappings: list[str] = []
    index = 1
    while index < len(packages_region) - 1:
        if packages_region[index] == "{":
            item = _balanced_region(packages_region, index, "{", "}")
            package_mappings.append(item)
            index += len(item)
        else:
            index += 1
    if len(package_mappings) != 1:
        raise ValueError(
            f"Expected exactly one CIPD package for {dependency!r}, "
            f"found {len(package_mappings)}"
        )
    package_template = _literal_field(package_mappings[0], "package")
    version = _literal_field(package_mappings[0], "version")
    if package_template != policy["package_template"]:
        raise ValueError(
            f"Unexpected Windows CIPD package for {dependency!r}: {package_template!r}"
        )
    if not re.fullmatch(r"version:[1-9][0-9]*@[A-Za-z0-9._+-]+", version):
        raise ValueError(f"Mutable or malformed Windows TypeScript CIPD pin: {version!r}")
    return CipdPackagePin(
        dependency=dependency,
        package_template=package_template,
        package=str(policy["package"]),
        version=version,
    )


def resolve_windows_cipd_tool_pins(
    root_deps: Path,
    devtools_deps: Path,
) -> tuple[CipdPackagePin, ...]:
    return tuple(
        resolve_windows_cipd_package(
            (
                devtools_deps
                if dependency.startswith("src/third_party/devtools-frontend/src/")
                else root_deps
            ),
            dependency,
        )
        for dependency in WINDOWS_CIPD_TOOL_DEPENDENCIES
    )


def windows_cipd_tool_descriptor_sha256(pins: tuple[CipdPackagePin, ...]) -> str:
    if tuple(pin.dependency for pin in pins) != WINDOWS_CIPD_TOOL_DEPENDENCIES:
        raise ValueError(
            "Windows CIPD tool descriptors are missing or out of canonical order"
        )
    payload = json.dumps(
        [asdict(pin) for pin in pins],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()



def resolve_pins(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    gn = GN_RE.search(text)
    ninja_package = NINJA_PACKAGE_RE.search(text)
    ninja_version = NINJA_VERSION_RE.search(text)
    cpython3_version = CPYTHON3_VERSION_RE.search(text)
    depot = DEPOT_RE.search(text)
    if not gn:
        raise ValueError(f"Could not resolve gn_version from {path}")
    if not depot:
        raise ValueError(f"Could not resolve depot_tools revision from {path}")
    if not ninja_package or not ninja_version:
        raise ValueError(f"Could not resolve Ninja CIPD pin from {path}")
    gn_version = gn.group(1)
    if not re.fullmatch(r"git_revision:[0-9a-f]{40}", gn_version):
        raise ValueError(f"Unsupported or mutable gn_version in {path}: {gn_version!r}")
    resolved_ninja_package = ninja_package.group(1)
    resolved_ninja_version = ninja_version.group(1)
    if resolved_ninja_package != "infra/3pp/tools/ninja/":
        raise ValueError(
            f"Unsupported Ninja CIPD package in {path}: {resolved_ninja_package!r}"
        )
    if not re.fullmatch(r"version:[1-9][0-9]*@[A-Za-z0-9._+-]+", resolved_ninja_version):
        raise ValueError(
            f"Unsupported or mutable ninja_version in {path}: {resolved_ninja_version!r}"
        )
    result = {
        "gn_version": gn_version,
        "depot_tools_revision": depot.group(1),
        "ninja_package": resolved_ninja_package,
        "ninja_version": resolved_ninja_version,
    }
    if cpython3_version:
        resolved_cpython3_version = cpython3_version.group(1)
        if not re.fullmatch(
            r"version:[1-9][0-9]*@[A-Za-z0-9._+-]+", resolved_cpython3_version
        ):
            raise ValueError(
                f"Unsupported or mutable cpython3_version in {path}: "
                f"{resolved_cpython3_version!r}"
            )
        result["cpython3_version"] = resolved_cpython3_version
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deps", type=Path, required=True)
    parser.add_argument(
        "--field",
        choices=(
            "gn_version",
            "depot_tools_revision",
            "ninja_package",
            "ninja_version",
            "cpython3_version",
        ),
    )
    args = parser.parse_args()
    pins = resolve_pins(args.deps)
    if args.field:
        if args.field not in pins:
            raise ValueError(f"Could not resolve {args.field} from {args.deps}")
        print(pins[args.field])
    else:
        print(json.dumps(pins, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
