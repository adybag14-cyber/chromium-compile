#!/usr/bin/env python3
"""Derive the Chromium Linux runtime bundle from Chromium's installer definition."""
from __future__ import annotations

import argparse
import re
from pathlib import Path, PurePosixPath

REQUIRED_RUNTIME = {
    "chrome",
    "chrome_crashpad_handler",
    "chrome_management_service",
    "chrome_sandbox",
    "libEGL.so",
    "libGLESv2.so",
    "icudtl.dat",
    "resources.pak",
    "locales",
}
EXTRA_IF_PRESENT = {
    "chrome-wrapper",
    "chrome_100_percent.pak",
    "chrome_200_percent.pak",
    "headless_command_resources.pak",
    "snapshot_blob.bin",
    "v8_context_snapshot.bin",
    "product_logo_48.png",
}


def _safe_rel(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe runtime path in Chromium installer definition: {value!r}")
    return path.as_posix()


def installer_runtime_candidates(source_root: Path) -> set[str]:
    build_gn = source_root / "chrome" / "installer" / "linux" / "BUILD.gn"
    text = build_gn.read_text(encoding="utf-8")
    start = text.find("packaging_files_executables = [")
    end = text.find('action_foreach("calculate_deb_dependencies")')
    if start < 0 or end <= start:
        raise ValueError("Chromium Linux installer packaging section changed; review runtime collector")
    region = text[start:end]
    found: set[str] = set()
    for match in re.finditer(r'"\$root_out_dir/([^"$]+)"', region):
        rel = _safe_rel(match.group(1))
        if rel.startswith("locales/"):
            found.add("locales")
        else:
            found.add(rel)
    missing_definition = {
        item for item in ("chrome", "chrome_crashpad_handler", "chrome_management_service", "chrome_sandbox", "libEGL.so", "libGLESv2.so")
        if item not in found
    }
    if missing_definition:
        raise ValueError(
            "Chromium Linux installer definition no longer exposes required runtime entries: "
            + ", ".join(sorted(missing_definition))
        )
    return found


def collect_runtime(source_root: Path, out_dir: Path) -> list[str]:
    candidates = installer_runtime_candidates(source_root) | REQUIRED_RUNTIME | EXTRA_IF_PRESENT
    for pattern in ("*.pak", "*.bin", "*.dat"):
        for path in out_dir.glob(pattern):
            candidates.add(path.name)
    for dirname in ("MEIPreload", "PrivacySandboxAttestationsPreloaded"):
        if (out_dir / dirname).exists():
            candidates.add(dirname)

    selected: set[str] = set()
    for rel in candidates:
        rel = _safe_rel(rel)
        path = out_dir / rel
        if not path.exists():
            continue
        if path.is_symlink():
            resolved = path.resolve()
            try:
                resolved.relative_to(out_dir.resolve())
            except ValueError as exc:
                raise ValueError(f"Runtime symlink escapes output directory: {rel} -> {resolved}") from exc
        selected.add(rel)

    missing = sorted(item for item in REQUIRED_RUNTIME if item not in selected)
    if missing:
        raise ValueError("Required Chromium Linux runtime output is missing: " + ", ".join(missing))
    return sorted(selected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--output-list", type=Path)
    parser.add_argument("--validate-definition", action="store_true")
    args = parser.parse_args()
    candidates = installer_runtime_candidates(args.source_root)
    if args.validate_definition:
        print(f"Chromium Linux installer runtime definition validated ({len(candidates)} literal outputs).")
        return 0
    if args.out_dir is None or args.output_list is None:
        parser.error("--out-dir and --output-list are required unless --validate-definition is used")
    files = collect_runtime(args.source_root, args.out_dir)
    args.output_list.write_text("\n".join(files) + "\n", encoding="utf-8")
    print(f"Collected {len(files)} Chromium Linux runtime paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
