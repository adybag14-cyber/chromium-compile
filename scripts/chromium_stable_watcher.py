#!/usr/bin/env python3
"""Detect unprocessed Chrome stable versions and dispatch i686 preflight builds."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
ACTIVE_RUN_STATES = {"queued", "in_progress", "requested", "waiting", "pending"}
QUARANTINE_RUN_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
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


def fetch_stable_versions(api_url: str, minimum: str, timeout: int = 60) -> list[str]:
    versions: set[str] = set()
    page_token = ""

    while True:
        params = {
            "filter": f"version>{minimum}",
            "order_by": "version asc",
            "page_size": "100",
        }
        if page_token:
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
            break

    return sorted(versions, key=version_key)


def run_gh(args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["gh", *args]
    try:
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown GitHub CLI error").strip()
        raise WatcherError(f"{' '.join(command)} failed: {detail}") from exc


def gh_json(args: Sequence[str]) -> object:
    result = run_gh(args)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise WatcherError(f"GitHub CLI returned invalid JSON for {' '.join(args)}") from exc


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


def list_release_versions(repository: str) -> set[str]:
    payload = gh_json(
        [
            "api",
            f"repos/{repository}/releases?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    releases = flatten_pages(payload)
    found: set[str] = set()
    pattern = re.compile(r"^chromium-(\d+\.\d+\.\d+\.\d+)-linux-i686$")
    for release in releases:
        match = pattern.fullmatch(str(release.get("tag_name", "")))
        if match:
            found.add(match.group(1))
    return found


def list_blocked_versions(repository: str) -> set[str]:
    payload = gh_json(
        [
            "api",
            f"repos/{repository}/issues?state=open&per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    issues = flatten_pages(payload)
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


def list_active_versions(repository: str) -> set[str]:
    payload = gh_json(
        [
            "api",
            f"repos/{repository}/actions/runs?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    pages = payload if isinstance(payload, list) else [payload]
    runs: list[object] = []
    for page in pages:
        if isinstance(page, dict):
            page_runs = page.get("workflow_runs", [])
            if isinstance(page_runs, list):
                runs.extend(page_runs)

    found: set[str] = set()
    pattern = re.compile(r"Chromium i686(?: preflight)? (\d+\.\d+\.\d+\.\d+)")
    for run in runs:
        if not isinstance(run, dict) or run.get("status") not in ACTIVE_RUN_STATES:
            continue
        match = pattern.search(str(run.get("display_title", "")))
        if match:
            found.add(match.group(1))
    return found


def list_quarantined_run_versions(repository: str) -> set[str]:
    """Return versions with a completed failed/cancelled port run.

    Run history is an independent safety record: even if issue reporting fails,
    the stable watcher must not redispatch the same broken version automatically.
    A manual --force-version retry intentionally bypasses this quarantine.
    """
    payload = gh_json(
        [
            "api",
            f"repos/{repository}/actions/runs?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    pages = payload if isinstance(payload, list) else [payload]
    runs: list[object] = []
    for page in pages:
        if isinstance(page, dict):
            page_runs = page.get("workflow_runs", [])
            if isinstance(page_runs, list):
                runs.extend(page_runs)

    found: set[str] = set()
    pattern = re.compile(r"Chromium i686(?: preflight)? (\d+\.\d+\.\d+\.\d+)")
    allowed_workflow_paths = {
        ".github/workflows/chromium-i686-preflight.yml",
        ".github/workflows/chromium-i686.yml",
    }
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("status") != "completed":
            continue
        if str(run.get("conclusion", "")) not in QUARANTINE_RUN_CONCLUSIONS:
            continue
        if str(run.get("path", "")) not in allowed_workflow_paths:
            continue
        match = pattern.search(str(run.get("display_title", "")))
        if match:
            found.add(match.group(1))
    return found


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


def dispatch_preflight(repository: str, ref: str, version: str, dry_run: bool) -> None:
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
    run_gh(command)
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
        candidates = [args.force_version]
    else:
        versions = fetch_stable_versions(args.api_url, minimum)
        issue_blocked = list_blocked_versions(args.repository)
        run_quarantined = list_quarantined_run_versions(args.repository)
        state = PortState(
            known=known,
            released=list_release_versions(args.repository),
            blocked=issue_blocked | run_quarantined,
            active=list_active_versions(args.repository),
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
        f"- Failed/cancelled run quarantines: `{len(run_quarantined)}`",
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
