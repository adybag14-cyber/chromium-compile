#!/usr/bin/env python3
"""Bridge a completed Chromium build run to the release publisher exactly once."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import github_workflow_dispatch as dispatch

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
BUILD_TITLE_RES = {
    "linux": re.compile(r"^Chromium i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) - stage ([1-9][0-9]*) - attempt ([0-9]+)$"),
    "windows": re.compile(r"^Chromium Windows i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) - stage ([1-9][0-9]*) - attempt ([0-9]+)$"),
}
BUILD_WORKFLOWS = {
    "linux": ".github/workflows/chromium-i686.yml",
    "windows": ".github/workflows/chromium-windows-i686.yml",
}
PUBLISH_WORKFLOWS = {
    "linux": "publish-i686-release.yml",
    "windows": "publish-windows-i686-release.yml",
}
ACTIVE_STATUSES = frozenset(("queued", "in_progress", "waiting", "pending", "requested"))


class HandoffError(RuntimeError):
    pass


def validate_inputs(repository: str, run_id: str, version: str, branch: str, expected_sha: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise HandoffError(f"invalid repository: {repository!r}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise HandoffError(f"invalid build run ID: {run_id!r}")
    if not VERSION_RE.fullmatch(version):
        raise HandoffError(f"invalid Chromium version: {version!r}")
    if not BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise HandoffError(f"invalid default branch: {branch!r}")
    try:
        return dispatch.normalize_expected_sha(expected_sha)
    except ValueError as exc:
        raise HandoffError(str(exc)) from exc


def read_build_run(repository: str, run_id: str) -> dict[str, object]:
    result = dispatch.run_gh(["api", f"repos/{repository}/actions/runs/{run_id}"], timeout=90)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HandoffError("build run API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HandoffError("build run API returned non-object JSON")
    return payload


def validate_build_identity(
    payload: dict[str, object], repository: str, version: str, branch: str, expected_sha: str,
    *, lane: str = "linux"
) -> str:
    if lane not in BUILD_TITLE_RES:
        raise HandoffError(f"unsupported release lane: {lane!r}")
    workflow_path = str(payload.get("path", "")).split("@", 1)[0]
    head_repo = payload.get("head_repository")
    head_repo_name = str(head_repo.get("full_name", "")) if isinstance(head_repo, dict) else ""
    head_branch = str(payload.get("head_branch", ""))
    head_sha = str(payload.get("head_sha", "")).lower()
    display_title = str(payload.get("display_title", ""))
    event = str(payload.get("event", ""))
    match = BUILD_TITLE_RES[lane].fullmatch(display_title)
    if workflow_path != BUILD_WORKFLOWS[lane]:
        raise HandoffError(f"run is not the trusted Chromium build workflow: {workflow_path!r}")
    if head_repo_name != repository:
        raise HandoffError(f"run originated from {head_repo_name!r}, not {repository!r}")
    if head_branch != branch:
        raise HandoffError(f"run branch {head_branch!r} does not match {branch!r}")
    if head_sha != expected_sha:
        raise HandoffError(f"run head SHA {head_sha!r} does not match immutable lineage {expected_sha}")
    if event != "workflow_dispatch":
        raise HandoffError(f"run event {event!r} is not workflow_dispatch")
    if match is None or match.group(1) != version:
        raise HandoffError(f"run title does not identify Chromium {version}: {display_title!r}")
    return display_title


def wait_for_successful_build(
    repository: str,
    run_id: str,
    version: str,
    branch: str,
    expected_sha: str,
    *,
    attempts: int = 60,
    delay_seconds: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
    lane: str = "linux",
) -> dict[str, object]:
    for attempt in range(attempts):
        payload = read_build_run(repository, run_id)
        validate_build_identity(
            payload, repository, version, branch, expected_sha, lane=lane
        )
        status = str(payload.get("status", ""))
        conclusion = payload.get("conclusion")
        if status == "completed":
            if conclusion != "success":
                raise HandoffError(f"build run {run_id} completed with conclusion {conclusion!r}")
            return payload
        if status not in ACTIVE_STATUSES:
            raise HandoffError(f"build run {run_id} returned unexpected status {status!r}")
        if attempt + 1 < attempts:
            sleeper(delay_seconds)
    raise HandoffError(f"build run {run_id} did not become terminal within the handoff wait budget")


def wait_for_legacy_publisher(
    repository: str,
    build_title: str,
    branch: str,
    expected_sha: str,
    parent_started: datetime,
    *,
    attempts: int = 4,
    delay_seconds: int = 2,
    sleeper: Callable[[float], None] = time.sleep,
    lane: str = "linux",
) -> bool:
    legacy_title = f"Publish {build_title}"
    for attempt in range(attempts):
        if dispatch.exact_exists_since(
            repository,
            PUBLISH_WORKFLOWS[lane],
            legacy_title,
            branch,
            parent_started,
            grace_seconds=30,
            expected_head_sha=expected_sha,
        ):
            return True
        if attempt + 1 < attempts:
            sleeper(delay_seconds)
    return False


def handoff_release(
    repository: str,
    run_id: str,
    version: str,
    branch: str,
    expected_sha: str,
    *,
    lane: str = "linux",
) -> str:
    if lane not in BUILD_TITLE_RES:
        raise HandoffError(f"unsupported release lane: {lane!r}")
    normalized_sha = validate_inputs(repository, run_id, version, branch, expected_sha)
    payload = wait_for_successful_build(
        repository, run_id, version, branch, normalized_sha, lane=lane
    )
    build_title = validate_build_identity(
        payload, repository, version, branch, normalized_sha, lane=lane
    )
    parent_started = dispatch.workflow_run_created_at(repository, run_id)

    # workflow_run is still preferred when GitHub emits it. Give that event a
    # short materialization window before falling back to workflow_dispatch, so
    # normal runs do not create redundant serialized publisher jobs.
    if wait_for_legacy_publisher(
        repository, build_title, branch, normalized_sha, parent_started, lane=lane
    ):
        return "workflow-run-publisher-present"

    label = "Chromium i686" if lane == "linux" else "Chromium Windows i686"
    expected_title = f"Publish {label} {version} from build run {run_id}"
    publisher_inputs = [f"build_run_id={run_id}", f"version={version}"]
    if lane == "windows":
        publisher_inputs.append(f"build_sha={normalized_sha}")
    return dispatch.dispatch_once(
        repository,
        PUBLISH_WORKFLOWS[lane],
        branch,
        expected_title,
        publisher_inputs,
        dedupe_completed=True,
        dedupe_since_run_id=run_id,
        expected_head_sha=normalized_sha,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--build-run-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--expected-build-sha", required=True)
    parser.add_argument("--lane", choices=tuple(BUILD_TITLE_RES), default="linux")
    args = parser.parse_args()
    try:
        result = handoff_release(
            args.repository,
            args.build_run_id,
            args.version,
            args.default_branch,
            args.expected_build_sha,
            lane=args.lane,
        )
    except (HandoffError, dispatch.DispatchError, ValueError) as exc:
        parser.error(str(exc))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
