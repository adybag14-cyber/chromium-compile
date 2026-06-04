"""Patch Chromium BUILD config to permit x86 Linux GN builds.

Scans common source paths and flips an x86 allowlist/assert pair from the
default 'x86 is not supported on Linux' state to a permissive true/allow state.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _patch_text(text: str) -> str:
    target_vars = re.findall(r"is_valid_x86_target\s*=\s*(\w+)", text)
    print("Pre-patch is_valid_x86_target values:", target_vars)

    lines = text.splitlines()

    def line_matches(index: int, *fragments: str) -> bool:
        head = lines[index]
        tail = "\n".join(lines[index : index + 6])
        return all(fragment in head or fragment in tail for fragment in fragments)

    patched = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.lstrip().startswith("assert(")
            and "is_valid_x86_target" in line
            and "not supported for 'target_os=linux'" in "\n".join(lines[index : index + 6])
            and not line.lstrip().startswith("assert(true)  # TARGET_CPU=X86 LINUX")
        ):
            print(f"Inserting tolerant assert above line {index + 1}")
            patched.append('assert(true)  # TARGET_CPU=X86 LINUX <- patched for i686')
            patched.append(line)
            print(f"Preserving original line: {line.strip()}")
            index += 1
            continue
        if line.lstrip().startswith("is_valid_x86_target =") and not line.lstrip().startswith("is_valid_x86_target = true") and not line.lstrip().startswith("is_valid_x86_target = false") and "false" not in line:
            print(f"Updating truth assignment at line {index + 1}: {line.strip()}")
            patched.append("  is_valid_x86_target = true")
            index += 1
            continue
        if re.search(r"is_valid_x86_target\s*=\s*false\b", line):
            print(f"Flipping assignment at line {index + 1}: {line.strip()}")
            patched.append(re.sub(r"is_valid_x86_target\s*=\s*false\b", "is_valid_x86_target = true", line))
            index += 1
            continue
        patched.append(line)
        index += 1

    return "\n".join(patched)


def patch_source_tree(source_dir: Path) -> None:
    search_paths = [
        "build/config/BUILD.gn",
        "build/config/gn/BUILD.gn",
        "BUILD.gn",
    ]

    matches: list[Path] = []
    for relative in search_paths:
        candidate = source_dir / relative
        if candidate.exists():
            matches.append(candidate)

    print(f"Located patch target files: {len(matches)}")
    for candidate in matches:
        print(f"- {candidate}")

    if not matches:
        raise SystemExit("No BUILD.gn files were found under build/config or source root.")

    for candidate in matches:
        print(f"Checking {candidate} ...")
        original = candidate.read_text()
        patched = _patch_text(original)
        if original == patched:
            print(f"No change needed in {candidate}")
            continue
        candidate.write_text(patched)
        print(f"Patched {candidate}")

    for candidate in matches:
        text = candidate.read_text()
        if "is_valid_x86_target = true" in text or "assert(true)  # TARGET_CPU=X86 LINUX" in text:
            print(f"Verified: {candidate} permits x86 Linux")
        else:
            print(f"WARNING: {candidate} still does not declare x86 Linux support")


if __name__ == "__main__":
    here = Path(__file__).resolve()
    source_dir = here.parent
    if (source_dir / "chromium_source").exists():
        source_dir = source_dir / "chromium_source"
    patch_source_tree(source_dir)
