#!/usr/bin/env python3
"""Detect unprocessed Chrome stable versions and dispatch i686 preflight builds."""

from __future__ import annotations

import argparse
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
from typing import Iterable, Sequence

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
ACTIVE_RUN_STATES = {"queued", "in_progress", "requested", "waiting", "pending"}
QUARANTINE_RUN_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
ACTIVE_QUERY_STATES = ("queued", "in_progress", "waiting", "pending", "requested")
PORT_WORKFLOWS = (
    "chromium-i686-preflight.yml",
    "chromium-i686.yml",
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


def load_baseline(path: Path) -> tuple[str, set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    minimum = data["minimum_version"]
    version_key(minimum)
    known = {
        entry["version"]
        for entry in data.get("verified_builds", [])
        if VERSION_RE.fullmatch(entry.get("version", ""))
    }
    return minimum, known


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


def list_workflow_runs(
    repository: str,
    workflow: str,
    *,
    created_after: datetime,
    max_pages: int = RUN_HISTORY_MAX_PAGES,
) -> list[dict[str, object]]:
    """Read a bounded, newest-first time window of one relevant workflow.

    Saturating the configured page horizon fails closed. API cost therefore stays
    bounded as repository history grows, without silently forgetting recent state.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    collected: list[dict[str, object]] = []
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    for page in range(1, max_pages + 1):
        params = {
            "per_page": "100",
            "page": str(page),
            "created": ">=" + created_after.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
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
        valid = [item for item in page_runs if isinstance(item, dict)]
        collected.extend(valid)
        if len(page_runs) < 100:
            return collected
    raise WatcherError(
        f"Workflow history horizon saturated for {workflow} at {max_pages * 100} "
        "runs in the quarantine window; refusing to guess about port state"
    )


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
    for page in range(1, max_pages + 1):
        payload = gh_json(
            [
                "api",
                f"repos/{repository}/{resource}{separator}per_page=100&page={page}",
            ]
        )
        if not isinstance(payload, list):
            raise WatcherError(f"Unexpected GitHub REST response for {resource}")
        found.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            return found
    raise WatcherError(
        f"GitHub REST horizon saturated for {resource} at {max_pages * 100} items; "
        "refusing to silently truncate port state"
    )


def list_release_versions(repository: str) -> set[str]:
    releases = list_rest_items(repository, "releases")
    found: set[str] = set()
    pattern = re.compile(r"^chromium-(\d+\.\d+\.\d+\.\d+)-linux-i686$")
    for release in releases:
        if bool(release.get("draft", False)):
            continue
        match = pattern.fullmatch(str(release.get("tag_name", "")))
        if match:
            found.add(match.group(1))
    return found


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


def list_port_run_state(repository: str) -> tuple[set[str], set[str]]:
    active: set[str] = set()
    quarantined: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RUN_HISTORY_DAYS)
    for workflow in PORT_WORKFLOWS:
        for run in list_workflow_runs(repository, workflow, created_after=cutoff):
            version = extract_port_version(str(run.get("display_title", "")))
            if not version:
                continue
            status = str(run.get("status", ""))
            conclusion = str(run.get("conclusion", ""))
            if status in ACTIVE_RUN_STATES:
                active.add(version)
            elif status == "completed" and conclusion in QUARANTINE_RUN_CONCLUSIONS:
                quarantined.add(version)
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


def _recent_exact_run_exists(
    repository: str,
    workflow: str,
    display_title: str,
    not_before: datetime,
) -> bool:
    payload = gh_json(
        [
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            workflow,
            "--limit",
            "50",
            "--json",
            "displayTitle,createdAt,status",
        ]
    )
    if not isinstance(payload, list):
        return False
    threshold = not_before.astimezone(timezone.utc) - timedelta(seconds=30)
    for item in payload:
        if not isinstance(item, dict) or item.get("displayTitle") != display_title:
            continue
        raw = str(item.get("createdAt", ""))
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= threshold:
            return True
    return False


def dispatch_preflight(repository: str, ref: str, version: str, dry_run: bool) -> None:
    display_title = f"Chromium i686 preflight {version}"
    command = [
        "workflow",
        "run",
        "chromium-i686-preflight.yml",
        "--repo",
        repository,
        "--ref",
        ref,
        "-f",
        f"version={version}",
        "-f",
        "dispatch_build=true",
    ]
    printable = "gh " + " ".join(command)
    if dry_run:
        print(f"DRY RUN: {printable}")
        return

    started = datetime.now(timezone.utc)
    try:
        run_gh(command, timeout=120)
    except WatcherError as dispatch_error:
        # A network timeout can occur after GitHub accepted workflow_dispatch.
        # Never blindly retry a non-idempotent dispatch. Confirm by run identity.
        for _ in range(6):
            time.sleep(3)
            try:
                if _recent_exact_run_exists(
                    repository,
                    "chromium-i686-preflight.yml",
                    display_title,
                    started,
                ):
                    print(
                        f"Dispatch client failed but Chromium {version} preflight "
                        "is visible in Actions; treating dispatch as accepted."
                    )
                    return
            except WatcherError:
                continue
        raise dispatch_error
    print(f"Dispatched Chromium {version} i686 compatibility preflight.")


def append_summary(lines: Iterable[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--baseline", type=Path, default=Path("support/baseline.json"))
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--max-new-builds", type=int, default=1)
    parser.add_argument("--force-version", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    minimum, known = load_baseline(args.baseline)
    state = PortState(known=known, released=set(), blocked=set(), active=set())
    issue_blocked: set[str] = set()
    run_quarantined: set[str] = set()

    if args.force_version:
        version_key(args.force_version)
        versions = [args.force_version]
        active, _run_quarantine = list_port_run_state(args.repository)
        state = PortState(known=known, released=set(), blocked=set(), active=active)
        candidates = [] if active else [args.force_version]
    else:
        versions = fetch_stable_versions(args.api_url, minimum)
        issue_blocked = list_blocked_versions(args.repository)
        active, run_quarantined = list_port_run_state(args.repository)
        state = PortState(
            known=known,
            released=list_release_versions(args.repository),
            blocked=issue_blocked | run_quarantined,
            active=active,
        )
        candidates = select_candidates(versions, minimum, state, args.max_new_builds)

    for version in candidates:
        dispatch_preflight(args.repository, args.ref, version, args.dry_run)

    summary = [
        "## Chromium i686 stable watcher",
        "",
        f"- Baseline: `{minimum}`",
        f"- Stable versions above baseline observed: `{len(versions)}`",
        f"- Active port runs: `{len(state.active)}`",
        f"- Open maintenance issues: `{len(issue_blocked)}`",
        f"- Recent terminal-run quarantines: `{len(run_quarantined)}`",
        f"- Total blocked versions: `{len(state.blocked)}`",
        f"- Candidate builds dispatched: `{len(candidates)}`",
    ]
    if candidates:
        summary.append(f"- Versions: `{', '.join(candidates)}`")
    else:
        summary.append("- Result: no unprocessed stable release was found.")
    append_summary(summary)
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WatcherError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
