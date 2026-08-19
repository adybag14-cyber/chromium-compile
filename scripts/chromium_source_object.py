#!/usr/bin/env python3
"""Fetch and verify immutable metadata for an official Chromium source object."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
GENERATION_RE = re.compile(r"^[1-9][0-9]{0,39}$")
SOURCE_BUCKET = "chromium-browser-official"
GCS_METADATA_HOST = "storage.googleapis.com"
GCS_DOWNLOAD_HOST = "commondatastorage.googleapis.com"
SOURCE_DOWNLOAD_TEMPLATE = (
    "https://commondatastorage.googleapis.com/chromium-browser-official/"
    "chromium-{version}.tar.xz"
)
SOURCE_METADATA_TEMPLATE = (
    "https://storage.googleapis.com/storage/v1/b/chromium-browser-official/o/"
    "chromium-{version}.tar.xz?fields=bucket,name,generation,size,md5Hash,crc32c,etag"
)
DEFAULT_MAX_SOURCE_ARCHIVE_GIB = 16
HARD_MAX_SOURCE_ARCHIVE_GIB = 32


class SourceObjectNotFound(ValueError):
    """Raised when the authoritative Chromium source object is not published yet."""


def validate_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid Chromium version: {version!r}")
    return version


def source_download_url(version: str) -> str:
    return SOURCE_DOWNLOAD_TEMPLATE.format(version=validate_version(version))


def source_cache_key(version: str, metadata: dict[str, object]) -> str:
    version = validate_version(version)
    generation = str(metadata.get("generation", ""))
    if not GENERATION_RE.fullmatch(generation):
        raise ValueError(f"Invalid GCS source generation for cache identity: {generation!r}")
    expected_url = source_download_url(version)
    if metadata.get("url") != expected_url:
        raise ValueError(
            f"Source cache metadata URL does not match Chromium {version}: {metadata.get('url')!r}"
        )
    return f"chromium-src-v4-{version}-{generation}"


def source_metadata_url(version: str) -> str:
    return SOURCE_METADATA_TEMPLATE.format(version=validate_version(version))


def _bounded_positive_env(name: str, default: int, hard_max: int) -> int:
    raw = os.environ.get(name, str(default))
    if not re.fullmatch(r"[1-9][0-9]{0,2}", raw):
        raise ValueError(f"{name} must be a short positive integer, got {raw!r}")
    value = int(raw)
    if value > hard_max:
        raise ValueError(f"{name} must not exceed hard maximum {hard_max} GiB")
    return value


def validate_source_metadata(version: str, metadata: dict[str, object]) -> dict[str, object]:
    version = validate_version(version)
    expected_url = source_download_url(version)
    if metadata.get("url") != expected_url:
        raise ValueError(f"Source metadata URL does not match Chromium {version}: {metadata.get('url')!r}")
    generation = str(metadata.get("generation", ""))
    if not GENERATION_RE.fullmatch(generation):
        raise ValueError(f"Invalid GCS source generation: {generation!r}")
    length = metadata.get("content_length")
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        raise ValueError(f"Invalid GCS source content length: {length!r}")
    max_gib = _bounded_positive_env(
        "CHROMIUM_I686_MAX_SOURCE_ARCHIVE_GIB",
        DEFAULT_MAX_SOURCE_ARCHIVE_GIB,
        HARD_MAX_SOURCE_ARCHIVE_GIB,
    )
    max_bytes = max_gib * 1024**3
    if length > max_bytes:
        raise ValueError(
            f"Chromium {version} source object is {length} bytes, above configured {max_gib} GiB compressed limit"
        )
    md5 = str(metadata.get("md5_base64", ""))
    try:
        raw_md5 = base64.b64decode(md5, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid GCS md5 metadata: {md5!r}") from exc
    if len(raw_md5) != 16:
        raise ValueError(f"Unexpected GCS md5 length: {len(raw_md5)}")
    return metadata


def validate_effective_https_host(url: str, expected_host: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError(
            f"Refusing redirected source trust endpoint {url!r}; expected https://{expected_host}/"
        )


def fetch_metadata(version: str, timeout: int = 60) -> dict[str, object]:
    version = validate_version(version)
    metadata_url = source_metadata_url(version)
    try:
        result = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--connect-timeout",
                "20",
                "--max-time",
                str(timeout),
                "--write-out",
                "\n%{http_code}\n%{url_effective}",
                metadata_url,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"Could not read Chromium {version} GCS metadata: {exc}") from exc
    try:
        payload_text, http_code, effective_url = (result.stdout or "").rsplit("\n", 2)
    except ValueError as exc:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "curl failed").strip()
            raise ValueError(f"Could not read Chromium {version} GCS metadata: {detail}") from exc
        raise ValueError(
            f"GCS metadata response omitted HTTP/effective-URL metadata for Chromium {version}"
        ) from exc
    validate_effective_https_host(effective_url.strip(), GCS_METADATA_HOST)
    if http_code == "404":
        raise SourceObjectNotFound(f"Chromium {version} source object is not published yet")
    if result.returncode != 0:
        detail = (result.stderr or payload_text or "curl failed").strip()
        raise ValueError(f"Could not read Chromium {version} GCS metadata: {detail}")
    if http_code != "200":
        raise ValueError(f"GCS metadata request for Chromium {version} returned HTTP {http_code}")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"GCS returned invalid metadata JSON for Chromium {version}") from exc
    if payload.get("bucket") != SOURCE_BUCKET:
        raise ValueError(f"GCS metadata returned unexpected bucket: {payload.get('bucket')!r}")
    if payload.get("name") != f"chromium-{version}.tar.xz":
        raise ValueError(f"GCS metadata returned unexpected object: {payload.get('name')!r}")
    generation = str(payload.get("generation", ""))
    length = str(payload.get("size", ""))
    md5 = str(payload.get("md5Hash", ""))
    crc32c = str(payload.get("crc32c", ""))
    etag = str(payload.get("etag", ""))
    if not GENERATION_RE.fullmatch(generation):
        raise ValueError(f"GCS source object lacks bounded numeric generation: {generation!r}")
    if not length.isdigit():
        raise ValueError(f"GCS source object lacks numeric content length: {length!r}")
    metadata = {
        "url": source_download_url(version),
        "generation": generation,
        "content_length": int(length),
        "md5_base64": md5,
        "crc32c": crc32c,
        "etag": etag,
    }
    return validate_source_metadata(version, metadata)


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


def write_marker(path: Path, *, version: str, metadata: dict[str, object], sha256: str, safe_archive: bool, gitiles_identity: bool) -> None:
    validate_version(version)
    if not (safe_archive and gitiles_identity):
        raise ValueError("Refusing to write source trust marker without both validation proofs")
    payload = {
        "schema": 1,
        "version": version,
        "generation": metadata["generation"],
        "content_length": metadata["content_length"],
        "md5_base64": metadata["md5_base64"],
        "etag": metadata.get("etag", ""),
        "sha256": sha256,
        "safe_archive": safe_archive,
        "gitiles_identity": gitiles_identity,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--metadata-out", type=Path)
    parser.add_argument("--metadata-in", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--check-marker", action="store_true")
    parser.add_argument("--write-marker", action="store_true")
    parser.add_argument("--safe-archive-verified", action="store_true")
    parser.add_argument("--gitiles-identity-verified", action="store_true")
    parser.add_argument("--cache-key-only", action="store_true")
    parser.add_argument("--availability-only", action="store_true")
    args = parser.parse_args()
    version = validate_version(args.version)
    if args.availability_only and args.metadata_in:
        parser.error("--availability-only cannot be combined with --metadata-in")
    if args.metadata_in:
        metadata = json.loads(args.metadata_in.read_text(encoding="utf-8"))
        metadata = validate_source_metadata(version, metadata)
    else:
        try:
            metadata = fetch_metadata(version)
        except SourceObjectNotFound:
            if args.availability_only:
                print("pending")
                return 3
            raise
    if args.availability_only:
        print("available")
        return 0
    result: dict[str, object] = dict(metadata)
    if args.file:
        result.update(verify_file(args.file, metadata))
    if args.metadata_out:
        args.metadata_out.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    if args.cache_key_only:
        print(source_cache_key(version, result))
        return 0
    if args.check_marker:
        if not args.marker or "sha256" not in result:
            parser.error("--check-marker requires --marker and verified --file metadata")
        if not marker_matches(args.marker, version=version, metadata=result, sha256=str(result["sha256"])):
            print(json.dumps(result, sort_keys=True))
            return 3
    if args.write_marker:
        if not args.marker or "sha256" not in result:
            parser.error("--write-marker requires --marker and metadata containing sha256")
        write_marker(args.marker, version=version, metadata=result, sha256=str(result["sha256"]), safe_archive=args.safe_archive_verified, gitiles_identity=args.gitiles_identity_verified)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
