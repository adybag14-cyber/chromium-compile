#!/usr/bin/env python3
"""Dispatch a GitHub Actions workflow exactly once under client uncertainty."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Sequence

ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
RUN_LOOKUP_LIMIT = int(os.environ.get("CHROMIUM_I686_DISPATCH_LOOKUP_LIMIT", "1000"))


class DispatchError(RuntimeError):
    """Raised when a workflow dispatch cannot be proven accepted."""


def run_gh(args: Sequence[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    command = ["gh", *args]
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(f"{' '.join(command)} timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown GitHub CLI error").strip()
        raise DispatchError(f"{' '.join(command)} failed: {detail}") from exc


def list_recent_runs(repository: str, workflow: str, *, attempts: int = 3) -> list[dict[str, object]]:
    last: DispatchError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = run_gh(
                [
                    "run",
                    "list",
                    "--repo",
                    repository,
                    "--workflow",
                    workflow,
                    "--limit",
                    str(RUN_LOOKUP_LIMIT),
                    "--json",
                    "databaseId,displayTitle,headBranch,status,conclusion,createdAt",
                ]
            )
            payload = json.loads(result.stdout or "[]")
            if not isinstance(payload, list):
                raise DispatchError("gh run list returned non-list JSON")
            return [item for item in payload if isinstance(item, dict)]
        except (DispatchError, json.JSONDecodeError) as exc:
            last = exc if isinstance(exc, DispatchError) else DispatchError(str(exc))
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
    assert last is not None
    raise last


def workflow_run_created_at(repository: str, run_id: str) -> datetime:
    if not run_id.isdigit():
        raise ValueError(f"Run ID must be numeric: {run_id!r}")
    result = run_gh(
        [
            "api",
            f"repos/{repository}/actions/runs/{run_id}",
            "--jq",
            ".created_at",
        ]
    )
    raw = (result.stdout or "").strip()
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DispatchError(f"Run {run_id} returned invalid created_at: {raw!r}") from exc
    if created.tzinfo is None:
        raise DispatchError(f"Run {run_id} returned timezone-naive created_at: {raw!r}")
    return created.astimezone(timezone.utc)


def _exact_runs_or_fail_closed(
    repository: str, workflow: str, expected_title: str, expected_ref: str
) -> list[dict[str, object]]:
    runs = list_recent_runs(repository, workflow)
    matches = [
        run
        for run in runs
        if str(run.get("displayTitle", "")) == expected_title
        and str(run.get("headBranch", "")) == expected_ref
    ]
    if not matches and len(runs) >= RUN_LOOKUP_LIMIT:
        raise DispatchError(
            f"Workflow run lookup saturated at {RUN_LOOKUP_LIMIT} entries for {workflow}; "
            f"refusing to assume {expected_title!r} is absent"
        )
    return matches


def exact_active_exists(
    repository: str, workflow: str, expected_title: str, expected_ref: str
) -> bool:
    return any(
        str(run.get("status", "")) in ACTIVE
        for run in _exact_runs_or_fail_closed(repository, workflow, expected_title, expected_ref)
    )


def exact_any_exists(
    repository: str, workflow: str, expected_title: str, expected_ref: str
) -> bool:
    return bool(_exact_runs_or_fail_closed(repository, workflow, expected_title, expected_ref))


def exact_exists_since(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    not_before: datetime,
    *,
    grace_seconds: int = 0,
) -> bool:
    threshold = not_before.astimezone(timezone.utc) - timedelta(seconds=grace_seconds)
    for run in _exact_runs_or_fail_closed(repository, workflow, expected_title, expected_ref):
        raw = str(run.get("createdAt", ""))
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= threshold:
            return True
    return False


def exact_recent_exists(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    not_before: datetime,
) -> bool:
    return exact_exists_since(
        repository, workflow, expected_title, expected_ref, not_before, grace_seconds=30
    )


def dispatch_once(
    repository: str,
    workflow: str,
    ref: str,
    expected_title: str,
    inputs: Sequence[str],
    *,
    confirm_attempts: int = 8,
    dedupe_completed: bool = False,
    dedupe_since_run_id: str | None = None,
) -> str:
    if dedupe_completed:
        if not dedupe_since_run_id:
            raise ValueError("dedupe_completed requires dedupe_since_run_id")
        parent_started = workflow_run_created_at(repository, dedupe_since_run_id)
        if exact_exists_since(repository, workflow, expected_title, ref, parent_started):
            return "already-seen"
    elif exact_active_exists(repository, workflow, expected_title, ref):
        return "already-active"

    command = ["workflow", "run", workflow, "--repo", repository, "--ref", ref]
    for value in inputs:
        if "=" not in value:
            raise ValueError(f"Workflow input must be key=value: {value!r}")
        command.extend(["-f", value])

    started = datetime.now(timezone.utc)
    try:
        run_gh(command, timeout=120)
        return "accepted"
    except DispatchError as dispatch_error:
        # Do not blindly retry a non-idempotent workflow_dispatch call: GitHub may
        # have accepted it just before the client/network failed.
        for _ in range(confirm_attempts):
            time.sleep(3)
            try:
                if exact_recent_exists(repository, workflow, expected_title, ref, started):
                    return "accepted-after-client-error"
            except DispatchError:
                continue
        raise dispatch_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--expected-title", required=True)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--dedupe-completed", action="store_true")
    parser.add_argument("--dedupe-since-run-id")
    args = parser.parse_args()
    result = dispatch_once(
        args.repository,
        args.workflow,
        args.ref,
        args.expected_title,
        args.input,
        dedupe_completed=args.dedupe_completed,
        dedupe_since_run_id=args.dedupe_since_run_id,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
