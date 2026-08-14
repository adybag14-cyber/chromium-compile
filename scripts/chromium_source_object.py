#!/usr/bin/env python3
"""Fetch and verify immutable metadata for an official Chromium source object."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import urllib.request
from pathlib import Path


def fetch_metadata(url: str, timeout: int = 60) -> dict[str, object]:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "chromium-i686-source-verifier/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        headers = response.headers
        hashes = headers.get_all("x-goog-hash") or []
        tokens: dict[str, str] = {}
        for header in hashes:
            for part in header.split(","):
                if "=" in part:
                    key, value = part.strip().split("=", 1)
                    tokens[key] = value
        md5 = tokens.get("md5", "")
        generation = headers.get("x-goog-generation", "")
        length = headers.get("x-goog-stored-content-length") or headers.get("Content-Length") or ""
        etag = headers.get("ETag", "").strip('"')
    if not generation.isdigit():
        raise ValueError(f"GCS source object lacks numeric generation: {generation!r}")
    if not str(length).isdigit():
        raise ValueError(f"GCS source object lacks numeric content length: {length!r}")
    if not md5:
        raise ValueError("GCS source object lacks x-goog-hash md5 metadata")
    try:
        raw_md5 = base64.b64decode(md5, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid GCS md5 metadata: {md5!r}") from exc
    if len(raw_md5) != 16:
        raise ValueError(f"Unexpected GCS md5 length: {len(raw_md5)}")
    if etag and etag.lower() != raw_md5.hex():
        raise ValueError("GCS ETag disagrees with x-goog-hash md5")
    return {
        "url": url,
        "generation": generation,
        "content_length": int(length),
        "md5_base64": md5,
        "etag": etag,
    }


def verify_file(path: Path, metadata: dict[str, object]) -> dict[str, str]:
    size = path.stat().st_size
    expected_size = int(metadata["content_length"])
    if size != expected_size:
        raise ValueError(f"Source object size mismatch: local {size}, GCS {expected_size}")
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            md5.update(chunk)
            sha256.update(chunk)
    actual_md5 = base64.b64encode(md5.digest()).decode("ascii")
    if actual_md5 != metadata["md5_base64"]:
        raise ValueError(
            f"Source object MD5 mismatch: local {actual_md5}, GCS {metadata['md5_base64']}"
        )
    return {"md5_base64": actual_md5, "sha256": sha256.hexdigest()}


def marker_matches(path: Path, *, version: str, metadata: dict[str, object], sha256: str) -> bool:
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        marker.get("schema") == 1
        and marker.get("version") == version
        and marker.get("generation") == metadata.get("generation")
        and marker.get("content_length") == metadata.get("content_length")
        and marker.get("md5_base64") == metadata.get("md5_base64")
        and marker.get("sha256") == sha256
        and marker.get("safe_archive") is True
        and marker.get("gitiles_identity") is True
    )


def write_marker(path: Path, *, version: str, metadata: dict[str, object], sha256: str) -> None:
    payload = {
        "schema": 1,
        "version": version,
        "generation": metadata["generation"],
        "content_length": metadata["content_length"],
        "md5_base64": metadata["md5_base64"],
        "etag": metadata.get("etag", ""),
        "sha256": sha256,
        "safe_archive": True,
        "gitiles_identity": True,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--metadata-out", type=Path)
    parser.add_argument("--metadata-in", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--check-marker", action="store_true")
    parser.add_argument("--write-marker", action="store_true")
    args = parser.parse_args()

    if args.metadata_in:
        metadata = json.loads(args.metadata_in.read_text(encoding="utf-8"))
    else:
        if not args.url:
            parser.error("--url is required unless --metadata-in is used")
        metadata = fetch_metadata(args.url)

    result: dict[str, object] = dict(metadata)
    if args.file:
        result.update(verify_file(args.file, metadata))
    if args.metadata_out:
        args.metadata_out.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")

    if args.check_marker:
        if not args.marker or not args.version or "sha256" not in result:
            parser.error("--check-marker requires --marker, --version, and verified --file metadata")
        if not marker_matches(args.marker, version=args.version, metadata=result, sha256=str(result["sha256"])):
            print(json.dumps(result, sort_keys=True))
            return 3

    if args.write_marker:
        if not args.marker or not args.version or "sha256" not in result:
            parser.error("--write-marker requires --marker, --version, and metadata containing sha256")
        write_marker(args.marker, version=args.version, metadata=result, sha256=str(result["sha256"]))

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
