#!/usr/bin/env python3
"""Resolve reproducible host-tool pins from a Chromium source DEPS file."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

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
    if not cpython3_version:
        raise ValueError(f"Could not resolve CPython 3 CIPD pin from {path}")
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
    resolved_cpython3_version = cpython3_version.group(1)
    if not re.fullmatch(
        r"version:[1-9][0-9]*@[A-Za-z0-9._+-]+", resolved_cpython3_version
    ):
        raise ValueError(
            f"Unsupported or mutable cpython3_version in {path}: "
            f"{resolved_cpython3_version!r}"
        )
    return {
        "gn_version": gn_version,
        "depot_tools_revision": depot.group(1),
        "ninja_package": resolved_ninja_package,
        "ninja_version": resolved_ninja_version,
        "cpython3_version": resolved_cpython3_version,
    }


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
        print(pins[args.field])
    else:
        print(json.dumps(pins, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
