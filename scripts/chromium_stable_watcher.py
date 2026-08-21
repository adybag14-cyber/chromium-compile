#!/usr/bin/env python3
"""Detect unprocessed Chrome stable versions and dispatch i686 preflight builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chromium_source_object import SourceObjectNotFound, fetch_metadata as fetch_source_metadata
from github_workflow_dispatch import DispatchError, dispatch_once

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_MANIFEST_MAX_BYTES = 64 * 1024
ACTIVE_RUN_STATES = {"queued", "in_progress", "requested", "waiting", "pending"}
QUARANTINE_RUN_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
ACTIVE_QUERY_STATES = ("queued", "in_progress", "waiting", "pending", "requested")
PORT_WORKFLOWS = (
    "chromium-i686-preflight.yml",
    "chromium-i686.yml",
    "publish-i686-release-handoff.yml",
    "publish-i686-release.yml",
)
GH_TIMEOUT_SECONDS = int(os.environ.get("CHROMIUM_I686_GH_TIMEOUT_SECONDS", "30"))
GH_READ_ATTEMPTS = int(os.environ.get("CHROMIUM_I686_GH_READ_ATTEMPTS", "3"))
RUN_HISTORY_DAYS = int(os.environ.get("CHROMIUM_I686_RUN_HISTORY_DAYS", "1095"))
RUN_HISTORY_MAX_PAGES = int(os.environ.get("CHROMIUM_I686_RUN_HISTORY_MAX_PAGES", "10"))
REST_MAX_PAGES = int(os.environ.get("CHROMIUM_I686_REST_MAX_PAGES", "10"))
VERSION_API_MAX_PAGES = int(os.environ.get("CHROMIUM_I686_VERSION_API_MAX_PAGES", "20"))
DEFAULT_API = (
    "https://versionhistory.googleapis.com/v1/"
    "chrome/platforms/linux/channels/stable/versions"
)


class WatcherError(RuntimeError):
    """Raised when stable-version discovery cannot safely continue."""


def version_key(version: str) -> tuple[int, int, int, int]:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid Chromium version: {version!r}")
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def load_baseline(path: Path) -> tuple[str, set[str], set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    minimum = data["minimum_version"]
    version_key(minimum)
    known = {
        entry["version"]
        for entry in data.get("verified_builds", [])
        if VERSION_RE.fullmatch(entry.get("version", ""))
    }
    raw_legacy_mutable = data.get("legacy_mutable_releases", [])
    if not isinstance(raw_legacy_mutable, list):
        raise ValueError("legacy_mutable_releases must be a JSON list")
    legacy_mutable: set[str] = set()
    for version in raw_legacy_mutable:
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise ValueError(f"Invalid legacy mutable release version: {version!r}")
        if version in legacy_mutable:
            raise ValueError(f"Duplicate legacy mutable release version: {version}")
        legacy_mutable.add(version)
    return minimum, known, legacy_mutable


def fetch_stable_versions(api_url: str, minimum: str, timeout: int = 30) -> list[str]:
    versions: set[str] = set()
    page_token = ""
    seen_tokens: set[str] = set()

    for _page in range(1, VERSION_API_MAX_PAGES + 1):
        params = {
            "filter": f"version>{minimum}",
            "order_by": "version asc",
            "page_size": "100",
        }
        if page_token:
            if page_token in seen_tokens:
                raise WatcherError("VersionHistory repeated a page token; refusing an infinite pagination loop")
            seen_tokens.add(page_token)
            params["page_token"] = page_token
        request = urllib.request.Request(
            f"{api_url}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "chromium-i686-port-watcher/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001 - preserve network context
            raise WatcherError(f"VersionHistory request failed: {exc}") from exc

        for item in payload.get("versions", []):
            version = item.get("version", "")
            if VERSION_RE.fullmatch(version) and version_key(version) > version_key(minimum):
                versions.add(version)

        page_token = str(payload.get("nextPageToken", ""))
        if not page_token:
            return sorted(versions, key=version_key)

    raise WatcherError(
        f"VersionHistory exceeded the configured {VERSION_API_MAX_PAGES}-page horizon; "
        "refusing to silently truncate stable versions"
    )


def run_gh(
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: int = GH_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = ["gh", *args]
    try:
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WatcherError(f"{' '.join(command)} timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown GitHub CLI error").strip()
        raise WatcherError(f"{' '.join(command)} failed: {detail}") from exc


def gh_json(args: Sequence[str], *, attempts: int = GH_READ_ATTEMPTS) -> object:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_error: WatcherError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = run_gh(args)
            try:
                return json.loads(result.stdout or "null")
            except json.JSONDecodeError as exc:
                raise WatcherError(
                    f"GitHub CLI returned invalid JSON for {' '.join(args)}"
                ) from exc
        except WatcherError as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(min(2 ** (attempt - 1), 5))
    assert last_error is not None
    raise last_error


def _github_not_found(detail: str) -> bool:
    return bool(re.search(r"(?:HTTP\s+404|Not Found\s*\(HTTP 404\)|status\s*404)", detail, re.I))


def gh_resource_text(
    args: Sequence[str], *, context: str, attempts: int = GH_READ_ATTEMPTS
) -> str:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_error: WatcherError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = run_gh(args, check=False)
        except WatcherError as exc:
            last_error = exc
        else:
            if result.returncode == 0:
                return result.stdout
            detail = (result.stderr or result.stdout or "unknown GitHub CLI error").strip()
            if _github_not_found(detail):
                raise ValueError(f"{context}: GitHub resource was not found")
            last_error = WatcherError(f"{context}: {detail}")
        if attempt < attempts:
            time.sleep(min(2 ** (attempt - 1), 5))
    assert last_error is not None
    raise last_error


def read_release_manifest_build_sha(
    repository: str, version: str, asset: dict[str, object]
) -> str:
    asset_id = asset.get("id")
    size = asset.get("size")
    digest = str(asset.get("digest", ""))
    if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
        raise ValueError("release manifest asset lacks a positive numeric id")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("release manifest asset lacks a positive numeric size")
    if size > RELEASE_MANIFEST_MAX_BYTES:
        raise ValueError(
            f"release manifest is {size} bytes, above {RELEASE_MANIFEST_MAX_BYTES}-byte watcher limit"
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("release manifest asset lacks a valid SHA-256 digest")

    text = gh_resource_text(
        [
            "api",
            "-H",
            "Accept: application/octet-stream",
            f"repos/{repository}/releases/assets/{asset_id}",
        ],
        context=f"Could not read Chromium {version} release manifest",
    )
    raw = text.encode("utf-8")
    if len(raw) != size:
        raise ValueError(f"release manifest byte length changed: metadata={size}, downloaded={len(raw)}")
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != digest:
        raise ValueError(
            f"release manifest digest changed: metadata={digest}, downloaded={actual_digest}"
        )

    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line == "packaged_files:":
            break
        if "=" not in line:
            required_seen = {"version", "target_cpu", "target_os", "github_sha"}.issubset(fields)
            if required_seen:
                # Pre-schema manifests begin the packaged-file list directly, without
                # a packaged_files: marker. Only accept that legacy transition after
                # every provenance field required by the watcher is already present.
                break
            raise ValueError(f"release manifest contains malformed metadata line: {line!r}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in fields:
            raise ValueError(f"release manifest contains invalid/duplicate key: {key!r}")
        fields[key] = value

    if fields.get("version") != version:
        raise ValueError(
            f"release manifest version does not match tag version: {fields.get('version')!r} != {version!r}"
        )
    if fields.get("target_cpu") != "x86" or fields.get("target_os") != "linux":
        raise ValueError("release manifest does not describe the Linux x86 target")
    build_sha = str(fields.get("github_sha", "")).lower()
    if not SHA1_RE.fullmatch(build_sha):
        raise ValueError(f"release manifest github_sha is missing or malformed: {build_sha!r}")
    return build_sha


def read_release_tag_commit(repository: str, version: str) -> str:
    tag = f"chromium-{version}-linux-i686"
    encoded_tag = urllib.parse.quote(tag, safe="")
    text = gh_resource_text(
        ["api", f"repos/{repository}/commits/{encoded_tag}"],
        context=f"Could not resolve release tag {tag}",
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WatcherError(f"GitHub returned invalid commit JSON for release tag {tag}") from exc
    if not isinstance(payload, dict):
        raise WatcherError(f"GitHub returned non-object commit JSON for release tag {tag}")
    commit = str(payload.get("sha", "")).lower()
    if not SHA1_RE.fullmatch(commit):
        raise WatcherError(f"GitHub returned invalid commit SHA for release tag {tag}: {commit!r}")
    return commit


def verify_release_provenance(
    repository: str, version: str, manifest_asset: dict[str, object]
) -> None:
    manifest_sha = read_release_manifest_build_sha(repository, version, manifest_asset)
    tag_sha = read_release_tag_commit(repository, version)
    if tag_sha != manifest_sha:
        raise ValueError(
            f"release tag resolves to {tag_sha}, but release manifest records build {manifest_sha}"
        )


def list_workflow_runs(
    repository: str,
    workflow: str,
    *,
    created_after: datetime,
    status_filter: str | None = None,
    max_pages: int = RUN_HISTORY_MAX_PAGES,
) -> list[dict[str, object]]:
    """Read one bounded relevant state/conclusion window for a workflow.

    Callers that use status_filter avoid letting successful high-volume compiler
    stages consume the three-year quarantine horizon. Every filtered response is
    verified locally so an ignored/changed GitHub filter fails closed.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    allowed_filters = ACTIVE_RUN_STATES | QUARANTINE_RUN_CONCLUSIONS
    if status_filter is not None and status_filter not in allowed_filters:
        raise ValueError(f"unsupported workflow status filter: {status_filter!r}")
    collected: list[dict[str, object]] = []
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    for page in range(1, max_pages + 2):
        params = {
            "per_page": "100",
            "page": str(page),
            "created": ">=" + created_after.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        if status_filter is not None:
            params["status"] = status_filter
        payload = gh_json(
            [
                "api",
                f"repos/{repository}/actions/workflows/{encoded_workflow}/runs?{urllib.parse.urlencode(params)}",
            ]
        )
        if not isinstance(payload, dict):
            raise WatcherError(f"Unexpected Actions response for {workflow}")
        page_runs = payload.get("workflow_runs", [])
        if not isinstance(page_runs, list):
            raise WatcherError(f"Actions response lacks workflow_runs for {workflow}")
        if page > max_pages:
            if page_runs:
                state = f" for status {status_filter}" if status_filter else ""
                raise WatcherError(
                    f"Workflow history horizon saturated for {workflow}{state} at {max_pages * 100} "
                    "runs in the quarantine window; refusing to guess about port state"
                )
            return collected
        valid = [item for item in page_runs if isinstance(item, dict)]
        if len(valid) != len(page_runs):
            raise WatcherError(f"Actions response contains malformed run metadata for {workflow}")
        if status_filter is not None:
            for item in valid:
                actual_status = str(item.get("status", ""))
                actual_conclusion = str(item.get("conclusion", ""))
                if status_filter in ACTIVE_RUN_STATES:
                    matches = actual_status == status_filter
                else:
                    matches = actual_status == "completed" and actual_conclusion == status_filter
                if not matches:
                    raise WatcherError(
                        f"GitHub ignored or changed workflow status filter {status_filter!r} for {workflow}; "
                        f"observed status={actual_status!r}, conclusion={actual_conclusion!r}"
                    )
        collected.extend(valid)
        if len(page_runs) < 100:
            return collected
    raise AssertionError("unreachable workflow pagination state")


def extract_port_version(title: str) -> str | None:
    match = re.search(r"Chromium i686(?: preflight)? (\d+\.\d+\.\d+\.\d+)", title)
    return match.group(1) if match else None


def flatten_pages(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        return []
    flattened: list[dict[str, object]] = []
    for item in payload:
        if isinstance(item, dict):
            flattened.append(item)
        elif isinstance(item, list):
            flattened.extend(entry for entry in item if isinstance(entry, dict))
    return flattened


def list_rest_items(
    repository: str,
    resource: str,
    *,
    max_pages: int = REST_MAX_PAGES,
) -> list[dict[str, object]]:
    """Read a bounded GitHub REST collection and fail closed on saturation."""
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    found: list[dict[str, object]] = []
    separator = "&" if "?" in resource else "?"
    for page in range(1, max_pages + 2):
        payload = gh_json(
            [
                "api",
                f"repos/{repository}/{resource}{separator}per_page=100&page={page}",
            ]
        )
        if not isinstance(payload, list):
            raise WatcherError(f"Unexpected GitHub REST response for {resource}")
        if page > max_pages:
            if payload:
                raise WatcherError(
                    f"GitHub REST horizon saturated for {resource} at {max_pages * 100} items; "
                    "refusing to silently truncate port state"
                )
            return found
        found.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            return found
    raise AssertionError("unreachable REST pagination state")


def list_release_health(
    repository: str, legacy_mutable_versions: set[str] | frozenset[str] = frozenset()
) -> tuple[set[str], set[str]]:
    releases = list_rest_items(repository, "releases")
    healthy: set[str] = set()
    broken: set[str] = set()
    pattern = re.compile(r"^chromium-(\d+\.\d+\.\d+\.\d+)-linux-i686$")
    digest_re = re.compile(r"^sha256:[0-9a-f]{64}$")
    for release in releases:
        match = pattern.fullmatch(str(release.get("tag_name", "")))
        if not match:
            continue
        version = match.group(1)
        expected_assets = {
            f"chromium-{version}-linux-i686.tar.xz",
            f"chromium-{version}-linux-i686.tar.xz.sha256",
            f"chromium-{version}-linux-i686-manifest.txt",
        }
        assets = release.get("assets", [])
        asset_map: dict[str, dict[str, object]] = {}
        duplicate = False
        if isinstance(assets, list):
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name", ""))
                if name in asset_map:
                    duplicate = True
                asset_map[name] = asset

        complete = not duplicate and expected_assets.issubset(asset_map)
        if complete:
            for name in expected_assets:
                asset = asset_map[name]
                if (
                    str(asset.get("state", "")) != "uploaded"
                    or int(asset.get("size", 0) or 0) <= 0
                    or not digest_re.fullmatch(str(asset.get("digest", "")))
                ):
                    complete = False
                    break

        immutable_ok = release.get("immutable") is True or version in legacy_mutable_versions
        healthy_candidate = not (
            bool(release.get("draft", False))
            or bool(release.get("prerelease", False))
            or not complete
            or not immutable_ok
        )
        if healthy_candidate:
            manifest_name = f"chromium-{version}-linux-i686-manifest.txt"
            try:
                verify_release_provenance(repository, version, asset_map[manifest_name])
            except ValueError as exc:
                print(f"Release provenance rejected for Chromium {version}: {exc}", file=sys.stderr)
                healthy_candidate = False
        if healthy_candidate:
            healthy.add(version)
        else:
            broken.add(version)
    return healthy, broken


def list_release_versions(
    repository: str, legacy_mutable_versions: set[str] | frozenset[str] = frozenset()
) -> set[str]:
    return list_release_health(repository, legacy_mutable_versions)[0]


def list_blocked_versions(repository: str) -> set[str]:
    issues = list_rest_items(repository, "issues?state=open")
    found: set[str] = set()
    pattern = re.compile(
        r"^\[i686-port\] Chromium (\d+\.\d+\.\d+\.\d+) requires maintenance$"
    )
    for issue in issues:
        if "pull_request" in issue:
            continue
        match = pattern.fullmatch(str(issue.get("title", "")))
        if match:
            found.add(match.group(1))
    return found


def list_port_run_state(
    repository: str, production_ref: str | None = None
) -> tuple[set[str], set[str]]:
    active: set[str] = set()
    quarantined: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RUN_HISTORY_DAYS)

    def consume(workflow: str, run: dict[str, object]) -> None:
        status = str(run.get("status", ""))
        conclusion = str(run.get("conclusion", ""))
        title = str(run.get("display_title", ""))
        version = extract_port_version(title)
        if not version:
            if status in ACTIVE_RUN_STATES:
                raise WatcherError(
                    f"Active {workflow} run has an unparseable display title {title!r}; "
                    "refusing to assume the global port queue is free"
                )
            # Legacy completed runs predate the explicit versioned run-name contract.
            return
        if status in ACTIVE_RUN_STATES:
            active.add(version)
            return
        if status == "completed" and conclusion in QUARANTINE_RUN_CONCLUSIONS:
            if production_ref is None:
                quarantined.add(version)
                return
            head_branch = str(run.get("head_branch", ""))
            if not head_branch:
                raise WatcherError(
                    f"Terminal {workflow} run for Chromium {version} lacks head_branch; "
                    "refusing to guess whether it belongs to the production ref"
                )
            if head_branch == production_ref:
                quarantined.add(version)

    for workflow in PORT_WORKFLOWS:
        for state in sorted(ACTIVE_RUN_STATES):
            for run in list_workflow_runs(
                repository, workflow, created_after=cutoff, status_filter=state
            ):
                consume(workflow, run)
        for conclusion in sorted(QUARANTINE_RUN_CONCLUSIONS):
            for run in list_workflow_runs(
                repository, workflow, created_after=cutoff, status_filter=conclusion
            ):
                consume(workflow, run)
    return active, quarantined


def list_active_versions(repository: str) -> set[str]:
    return list_port_run_state(repository)[0]


def list_quarantined_run_versions(repository: str) -> set[str]:
    return list_port_run_state(repository)[1]


@dataclass(frozen=True)
class PortState:
    known: set[str]
    released: set[str]
    blocked: set[str]
    active: set[str]


def select_candidates(
    versions: Iterable[str],
    minimum: str,
    state: PortState,
    limit: int,
) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be at least 1")

    # A queued or running preflight/build/publisher owns the port queue.
    if state.active:
        return []

    selected: list[str] = []
    for version in sorted(set(versions), key=version_key):
        if version_key(version) <= version_key(minimum):
            continue
        if version in state.known or version in state.released:
            continue
        # An open maintenance issue records this exact version as processed.
        # Later stable versions must still receive their own compatibility attempt.
        if version in state.blocked:
            continue
        selected.append(version)
        if len(selected) >= limit:
            break
    return selected


def source_object_is_ready(version: str) -> bool:
    """Return whether the authoritative GCS source object exists and is valid."""
    try:
        fetch_source_metadata(version, timeout=30)
    except SourceObjectNotFound:
        return False
    except ValueError as exc:
        raise WatcherError(
            f"Could not verify Chromium {version} source publication readiness: {exc}"
        ) from exc
    return True


def dispatch_preflight(
    repository: str,
    ref: str,
    version: str,
    dry_run: bool,
    expected_head_sha: str | None = None,
) -> None:
    display_title = f"Chromium i686 preflight {version}"
    inputs = [f"version={version}", "dispatch_build=true"]
    if dry_run:
        printable = (
            f"gh workflow run chromium-i686-preflight.yml --repo {repository} "
            f"--ref {ref} -f version={version} -f dispatch_build=true"
        )
        print(f"DRY RUN: {printable}")
        return

    try:
        dispatch_kwargs: dict[str, object] = {}
        if expected_head_sha is not None:
            dispatch_kwargs["expected_head_sha"] = expected_head_sha
        result = dispatch_once(
            repository,
            "chromium-i686-preflight.yml",
            ref,
            display_title,
            inputs,
            **dispatch_kwargs,
        )
    except (DispatchError, ValueError) as exc:
        raise WatcherError(f"Preflight dispatch could not be established safely: {exc}") from exc

    if result == "already-active":
        print(f"Chromium {version} preflight became active before dispatch; no duplicate was sent.")
    elif result == "accepted-after-client-error":
        print(f"Dispatch client failed but Chromium {version} preflight is visible in Actions; treating dispatch as accepted.")
    else:
        print(f"Dispatched Chromium {version} i686 compatibility preflight.")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument(
        "--expected-head-sha",
        default="",
        help="Optional immutable workflow commit that the dispatch ref must still resolve to",
    )
    parser.add_argument("--baseline", type=Path, default=Path("support/baseline.json"))
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--max-new-builds", type=int, default=1)
    parser.add_argument("--force-version", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    minimum, known, legacy_mutable_releases = load_baseline(args.baseline)
    state = PortState(known=known, released=set(), blocked=set(), active=set())
    issue_blocked: set[str] = set()
    run_quarantined: set[str] = set()
    superseded_run_quarantines: set[str] = set()

    if args.force_version:
        version_key(args.force_version)
        versions = [args.force_version]
        active, _run_quarantine = list_port_run_state(args.repository, args.ref)
        released, broken_releases = list_release_health(args.repository, legacy_mutable_releases)
        unattended_broken = broken_releases - active
        if unattended_broken:
            formatted = ", ".join(sorted(unattended_broken, key=version_key))
            raise WatcherError(
                "Published/draft Chromium i686 release state is incomplete or unverifiable for: "
                f"{formatted}. Refusing to force a build around a broken immutable publication record."
            )
        if broken_releases & active:
            print(
                "Ignoring transient incomplete release state for active port owner(s): "
                + ", ".join(sorted(broken_releases & active, key=version_key))
            )
        state = PortState(known=known, released=released, blocked=set(), active=active)
        if active:
            candidates = []
        elif args.force_version in released:
            candidates = []
            print(
                f"Chromium {args.force_version} already has a healthy release accepted by repository immutability policy; "
                "force_version cannot launch a replacement build."
            )
        elif version_key(args.force_version) <= version_key(minimum):
            raise WatcherError(
                f"Chromium {args.force_version} is not newer than baseline {minimum}; "
                "use the manual preflight workflow for historical testing."
            )
        else:
            candidates = [args.force_version]
    else:
        versions = fetch_stable_versions(args.api_url, minimum)
        issue_blocked = list_blocked_versions(args.repository)
        active, run_quarantined = list_port_run_state(args.repository, args.ref)
        released, broken_releases = list_release_health(args.repository, legacy_mutable_releases)
        superseded_run_quarantines = run_quarantined & released
        run_quarantined = run_quarantined - released
        unattended_broken = broken_releases - active
        if unattended_broken:
            formatted = ", ".join(sorted(unattended_broken, key=version_key))
            raise WatcherError(
                "Published/draft Chromium i686 release state is incomplete or unverifiable for: "
                f"{formatted}. Refusing to rebuild around a broken immutable publication record."
            )
        if broken_releases & active:
            print(
                "Ignoring transient incomplete release state for active port owner(s): "
                + ", ".join(sorted(broken_releases & active, key=version_key))
            )
        state = PortState(
            known=known,
            released=released,
            blocked=(issue_blocked | run_quarantined) - released,
            active=active,
        )
        candidates = select_candidates(versions, minimum, state, args.max_new_builds)

    source_pending: list[str] = []
    ready_candidates: list[str] = []
    if candidates:
        # Source archives can lag Chrome VersionHistory. Scan eligible versions in
        # order and dispatch only versions whose authoritative GCS object exists.
        if args.force_version:
            eligible_candidates = candidates
        else:
            eligible_candidates = select_candidates(
                versions, minimum, state, max(len(set(versions)), args.max_new_builds)
            )
        for version in eligible_candidates:
            if source_object_is_ready(version):
                ready_candidates.append(version)
                if len(ready_candidates) >= args.max_new_builds:
                    break
            else:
                source_pending.append(version)
                print(
                    f"Chromium {version} is stable but its authoritative source object is not published yet; "
                    "deferring compatibility preflight."
                )
        candidates = ready_candidates

    for version in candidates:
        dispatch_preflight(
            args.repository,
            args.ref,
            version,
            args.dry_run,
            args.expected_head_sha or None,
        )

    summary = [
        "## Chromium i686 stable watcher",
        "",
        f"- Baseline: `{minimum}`",
        f"- Stable versions above baseline observed: `{len(versions)}`",
        f"- Active port runs: `{len(state.active)}`",
        f"- Open maintenance issues: `{len(issue_blocked)}`",
        f"- Recent terminal-run quarantines: `{len(run_quarantined)}`",
        f"- Historical run quarantines superseded by healthy releases: `{len(superseded_run_quarantines)}`",
        f"- Total blocked versions: `{len(state.blocked)}`",
        f"- Candidate builds dispatched: `{len(candidates)}`",
        f"- Stable versions waiting for source publication: `{len(source_pending)}`",
    ]
    if source_pending:
        summary.append(f"- Source pending: `{', '.join(source_pending)}`")
    if candidates:
        summary.append(f"- Versions: `{', '.join(candidates)}`")
    else:
        summary.append("- Result: no unprocessed stable release was found.")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WatcherError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
