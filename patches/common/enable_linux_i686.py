#!/usr/bin/env python3
"""Apply the minimal semantic GN guard change needed for Linux i686."""

from __future__ import annotations

import argparse
from pathlib import Path

OLD = 'is_valid_x86_target || target_cpu != "x86" || v8_target_cpu == "arm",'
NEW = (
    'is_valid_x86_target || target_cpu != "x86" || '
    'v8_target_cpu == "arm" || target_os == "linux",'
)


def patch_build_gn(source_root: Path, version: str) -> str:
    path = source_root / "BUILD.gn"
    if not path.is_file():
        raise SystemExit(f"Chromium {version}: missing {path}")

    text = path.read_text(encoding="utf-8")
    if NEW in text:
        return "already-applied"
    if OLD not in text:
        nearby = "\n".join(
            line for line in text.splitlines() if "is_valid_x86_target" in line
        )
        raise SystemExit(
            "Chromium's x86 target guard changed upstream. "
            "The Linux i686 semantic patch requires maintenance.\n"
            f"Version: {version}\n"
            f"Observed matching lines:\n{nearby or '(none)'}"
        )

    path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    return "applied"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    result = patch_build_gn(args.source_root, args.version)
    print(f"Linux i686 GN guard patch: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
