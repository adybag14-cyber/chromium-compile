#!/usr/bin/env python3
"""Dispatch a GitHub Actions workflow exactly once under client uncertainty."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Sequence

ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


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
                    "100",
                    "--json",
                    "databaseId,displayTitle,status,conclusion,createdAt",
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


def exact_active_exists(repository: str, workflow: str, expected_title: str) -> bool:
    return any(
        str(run.get("displayTitle", "")) == expected_title
        and str(run.get("status", "")) in ACTIVE
        for run in list_recent_runs(repository, workflow)
    )


def exact_recent_exists(
    repository: str,
    workflow: str,
    expected_title: str,
    not_before: datetime,
) -> bool:
    threshold = not_before.astimezone(timezone.utc) - timedelta(seconds=30)
    for run in list_recent_runs(repository, workflow):
        if str(run.get("displayTitle", "")) != expected_title:
            continue
        raw = str(run.get("createdAt", ""))
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= threshold:
            return True
    return False


def dispatch_once(
    repository: str,
    workflow: str,
    ref: str,
    expected_title: str,
    inputs: Sequence[str],
    *,
    confirm_attempts: int = 8,
) -> str:
    if exact_active_exists(repository, workflow, expected_title):
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
                if exact_recent_exists(repository, workflow, expected_title, started):
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
    args = parser.parse_args()
    result = dispatch_once(
        args.repository,
        args.workflow,
        args.ref,
        args.expected_title,
        args.input,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
