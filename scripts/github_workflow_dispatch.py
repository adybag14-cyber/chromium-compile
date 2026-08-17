#!/usr/bin/env python3
"""Dispatch a GitHub Actions workflow exactly once under client uncertainty."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Sequence

ACTIVE_STATUSES = ("in_progress", "queued", "waiting", "pending", "requested")
ACTIVE = frozenset(ACTIVE_STATUSES)
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


RUN_LOOKUP_LIMIT = _bounded_int_env(
    "CHROMIUM_I686_DISPATCH_LOOKUP_LIMIT", 1000, 100, 5000
)
CONFIRM_DELAYS_SECONDS = (2, 3, 5, 8, 12, 15, 20, 25)


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


def _created_filter_value(created_after: datetime) -> str:
    if created_after.tzinfo is None:
        raise ValueError("created_after must be timezone-aware")
    utc = created_after.astimezone(timezone.utc).replace(microsecond=0)
    return ">=" + utc.isoformat().replace("+00:00", "Z")


def list_recent_runs(
    repository: str,
    workflow: str,
    *,
    attempts: int = 3,
    branch: str | None = None,
    commit: str | None = None,
    created_after: datetime | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    if commit is not None:
        commit = normalize_expected_sha(commit)
    last: DispatchError | None = None
    for attempt in range(1, attempts + 1):
        try:
            command = [
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                workflow,
            ]
            if branch is not None:
                command.append(f"--branch={branch}")
            if commit is not None:
                command.append(f"--commit={commit}")
            if created_after is not None:
                command.append(f"--created={_created_filter_value(created_after)}")
            if status is not None:
                command.append(f"--status={status}")
            command.extend(
                [
                    "--limit",
                    str(RUN_LOOKUP_LIMIT),
                    "--json",
                    "databaseId,displayTitle,headBranch,headSha,status,conclusion,createdAt",
                ]
            )
            result = run_gh(command)
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


def normalize_expected_sha(value: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise ValueError(f"Expected workflow head SHA must be exactly 40 hexadecimal characters: {value!r}")
    return value.lower()


def resolve_ref_sha(repository: str, ref: str) -> str:
    encoded_ref = urllib.parse.quote(ref, safe="")
    result = run_gh(
        ["api", f"repos/{repository}/commits/{encoded_ref}", "--jq", ".sha"]
    )
    sha = (result.stdout or "").strip()
    if not SHA_RE.fullmatch(sha):
        raise DispatchError(f"GitHub returned an invalid commit SHA for ref {ref!r}: {sha!r}")
    return sha.lower()


def _exact_runs_or_fail_closed(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    expected_head_sha: str | None = None,
    created_after: datetime | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    runs = list_recent_runs(
        repository,
        workflow,
        branch=expected_ref,
        commit=expected_head_sha,
        created_after=created_after,
        status=status,
    )
    matches = [
        run
        for run in runs
        if str(run.get("displayTitle", "")) == expected_title
        and str(run.get("headBranch", "")) == expected_ref
        and (
            expected_head_sha is None
            or str(run.get("headSha", "")).lower() == expected_head_sha.lower()
        )
        and (status is None or str(run.get("status", "")) == status)
    ]
    if not matches and len(runs) >= RUN_LOOKUP_LIMIT:
        raise DispatchError(
            f"Workflow run lookup saturated at {RUN_LOOKUP_LIMIT} entries for {workflow}; "
            f"refusing to assume {expected_title!r} is absent"
        )
    return matches


def recent_dispatch_head_state(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    expected_head_sha: str,
    not_before: datetime,
) -> str:
    threshold = not_before.astimezone(timezone.utc) - timedelta(seconds=30)
    # Do not commit-filter this post-dispatch view: a same-title/ref run at the
    # wrong SHA is the race condition this function must detect.
    runs = list_recent_runs(
        repository, workflow, branch=expected_ref, created_after=threshold
    )
    title_ref_matches = [
        run
        for run in runs
        if str(run.get("displayTitle", "")) == expected_title
        and str(run.get("headBranch", "")) == expected_ref
    ]
    if not title_ref_matches and len(runs) >= RUN_LOOKUP_LIMIT:
        raise DispatchError(
            f"Workflow run lookup saturated at {RUN_LOOKUP_LIMIT} entries for {workflow}; "
            f"refusing to assume {expected_title!r} is absent"
        )
    saw_expected = False
    saw_other = False
    for run in title_ref_matches:
        raw = str(run.get("createdAt", ""))
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DispatchError(
                f"Workflow run for {expected_title!r} returned invalid createdAt metadata: {raw!r}"
            ) from exc
        if created.tzinfo is None:
            raise DispatchError(
                f"Workflow run for {expected_title!r} returned timezone-naive createdAt metadata: {raw!r}"
            )
        created = created.astimezone(timezone.utc)
        if created < threshold:
            continue
        observed_sha = str(run.get("headSha", "")).lower()
        if observed_sha == expected_head_sha.lower():
            saw_expected = True
        else:
            saw_other = True
    if saw_expected:
        return "matched"
    return "mismatch" if saw_other else "absent"


def confirm_expected_dispatch_head(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    expected_head_sha: str,
    started: datetime,
    *,
    confirm_attempts: int,
) -> bool:
    for attempt in range(confirm_attempts):
        try:
            state = recent_dispatch_head_state(
                repository,
                workflow,
                expected_title,
                expected_ref,
                expected_head_sha,
                started,
            )
        except DispatchError:
            state = "absent"
        if state == "mismatch":
            raise DispatchError(
                f"Workflow {workflow} materialized at a different head SHA than the immutable lineage "
                f"{expected_head_sha}; refusing to continue"
            )
        if state == "matched":
            return True
        if attempt + 1 < confirm_attempts:
            delay = CONFIRM_DELAYS_SECONDS[min(attempt, len(CONFIRM_DELAYS_SECONDS) - 1)]
            time.sleep(delay)
    return False


def exact_active_exists(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    *,
    expected_head_sha: str | None = None,
) -> bool:
    # Query only incomplete states. Historical completed runs cannot consume the
    # bounded lookup budget even if one workflow commit remains deployed for years.
    for status in ACTIVE_STATUSES:
        if _exact_runs_or_fail_closed(
            repository,
            workflow,
            expected_title,
            expected_ref,
            expected_head_sha,
            status=status,
        ):
            return True
    return False


def exact_any_exists(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    *,
    expected_head_sha: str | None = None,
) -> bool:
    return bool(
        _exact_runs_or_fail_closed(
            repository, workflow, expected_title, expected_ref, expected_head_sha
        )
    )


def exact_exists_since(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    not_before: datetime,
    *,
    grace_seconds: int = 0,
    expected_head_sha: str | None = None,
) -> bool:
    threshold = not_before.astimezone(timezone.utc) - timedelta(seconds=grace_seconds)
    for run in _exact_runs_or_fail_closed(
        repository,
        workflow,
        expected_title,
        expected_ref,
        expected_head_sha,
        created_after=threshold,
    ):
        raw = str(run.get("createdAt", ""))
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DispatchError(
                f"Workflow run for {expected_title!r} returned invalid createdAt metadata: {raw!r}"
            ) from exc
        if created.tzinfo is None:
            raise DispatchError(
                f"Workflow run for {expected_title!r} returned timezone-naive createdAt metadata: {raw!r}"
            )
        created = created.astimezone(timezone.utc)
        if created >= threshold:
            return True
    return False


def exact_recent_exists(
    repository: str,
    workflow: str,
    expected_title: str,
    expected_ref: str,
    not_before: datetime,
    expected_head_sha: str | None = None,
) -> bool:
    return exact_exists_since(
        repository,
        workflow,
        expected_title,
        expected_ref,
        not_before,
        grace_seconds=30,
        expected_head_sha=expected_head_sha,
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
    expected_head_sha: str | None = None,
) -> str:
    normalized_head_sha = (
        normalize_expected_sha(expected_head_sha) if expected_head_sha is not None else None
    )
    for value in inputs:
        if "=" not in value:
            raise ValueError(f"Workflow input must be key=value: {value!r}")

    if dedupe_completed:
        if not dedupe_since_run_id:
            raise ValueError("dedupe_completed requires dedupe_since_run_id")
        parent_started = workflow_run_created_at(repository, dedupe_since_run_id)
        if exact_exists_since(
            repository,
            workflow,
            expected_title,
            ref,
            parent_started,
            expected_head_sha=normalized_head_sha,
        ):
            return "already-seen"
    elif exact_active_exists(
        repository, workflow, expected_title, ref, expected_head_sha=normalized_head_sha
    ):
        return "already-active"

    if normalized_head_sha is not None:
        current_ref_sha = resolve_ref_sha(repository, ref)
        if current_ref_sha != normalized_head_sha:
            raise DispatchError(
                f"Ref {ref!r} now resolves to {current_ref_sha}, not immutable lineage "
                f"{normalized_head_sha}; refusing workflow dispatch"
            )

    command = ["workflow", "run", workflow, "--repo", repository, "--ref", ref]
    for value in inputs:
        command.extend(["-f", value])

    started = datetime.now(timezone.utc)
    try:
        run_gh(command, timeout=120)
    except DispatchError as dispatch_error:
        # Do not blindly retry a non-idempotent workflow_dispatch call: GitHub may
        # have accepted it just before the client/network failed. Check immediately,
        # then use bounded backoff so Actions has time to materialize a delayed run.
        if normalized_head_sha is not None:
            if confirm_expected_dispatch_head(
                repository,
                workflow,
                expected_title,
                ref,
                normalized_head_sha,
                started,
                confirm_attempts=confirm_attempts,
            ):
                return "accepted-after-client-error"
            raise dispatch_error
        for attempt in range(confirm_attempts):
            try:
                if exact_recent_exists(repository, workflow, expected_title, ref, started):
                    return "accepted-after-client-error"
            except DispatchError:
                pass
            if attempt + 1 < confirm_attempts:
                delay = CONFIRM_DELAYS_SECONDS[min(attempt, len(CONFIRM_DELAYS_SECONDS) - 1)]
                time.sleep(delay)
        raise dispatch_error

    if normalized_head_sha is None:
        return "accepted"
    if confirm_expected_dispatch_head(
        repository,
        workflow,
        expected_title,
        ref,
        normalized_head_sha,
        started,
        confirm_attempts=confirm_attempts,
    ):
        return "accepted-confirmed"
    raise DispatchError(
        f"Workflow dispatch returned success but no run at immutable lineage SHA "
        f"{normalized_head_sha} became visible"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--expected-title", required=True)
    parser.add_argument("--expected-head-sha")
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
        expected_head_sha=args.expected_head_sha,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
