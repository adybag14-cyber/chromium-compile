"""Test harness for the BUILD.gn x86 Linux patch."""

from __future__ import annotations

import shutil
from pathlib import Path

from patch_build_gn import patch_source_tree


def main() -> None:
    root = Path(__file__).resolve().parent / "chromium_source"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    build_config = root / "build" / "config"
    build_config.mkdir(parents=True, exist_ok=True)

    buildgn = build_config / "BUILD.gn"
    buildgn.write_text(
        "\n".join(
            [
                "# simulated BUILD.gn",
                "assert(",
                '  is_valid_x86_target && target_cpu != "x86" || v8_target_cpu == "arm",',
                "  'target_cpu=x86' is not supported for 'target_os=linux'.",
                ")",
                "is_valid_x86_target = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    patch_source_tree(root)

    patched = buildgn.read_text(encoding="utf-8")
    expected_assert = "assert(true)  # TARGET_CPU=X86 LINUX"
    expected_assignment = "is_valid_x86_target = true"

    if expected_assert not in patched:
        raise SystemExit("assert block was not patched")
    if expected_assignment not in patched:
        raise SystemExit("variable was not flipped to true")
    if "not supported" in patched:
        raise SystemExit("patched file still contains the old unsupported message")

    print("patch_build_gn test passed")


if __name__ == "__main__":
    main()
