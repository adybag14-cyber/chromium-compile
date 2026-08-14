#!/usr/bin/env python3
"""Idempotently create or update a maintenance issue under GitHub API uncertainty."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Sequence


ISSUE_LIST_LIMIT = 1000


class IssueError(RuntimeError):
    """Raised when maintenance issue state cannot be established safely."""


class IssueStateError(IssueError):
    """Raised for deterministic issue-state/schema ambiguity that retries cannot fix."""


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
        raise IssueError(f"{' '.join(command)} timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown GitHub CLI error").strip()
        raise IssueError(f"{' '.join(command)} failed: {detail}") from exc


def find_issue(repository: str, title: str, *, attempts: int = 3) -> int | None:
    last: IssueError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = run_gh(
                [
                    "issue",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "open",
                    "--limit",
                    str(ISSUE_LIST_LIMIT),
                    "--json",
                    "number,title",
                ]
            )
        except IssueError as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
                continue
            raise

        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise IssueStateError("gh issue list returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise IssueStateError("gh issue list returned non-list JSON")
        if len(payload) >= ISSUE_LIST_LIMIT:
            raise IssueStateError(
                f"Open issue lookup saturated at {ISSUE_LIST_LIMIT} entries; "
                "refusing to guess whether an exact maintenance issue exists beyond the horizon"
            )
        try:
            matches = [
                int(item["number"])
                for item in payload
                if isinstance(item, dict) and item.get("title") == title and "number" in item
            ]
        except (TypeError, ValueError) as exc:
            raise IssueStateError("gh issue list returned an invalid issue number") from exc
        if len(matches) > 1:
            raise IssueStateError(f"Multiple open issues have the exact maintenance title: {title}")
        return matches[0] if matches else None
    assert last is not None
    raise last


def upsert_issue(repository: str, title: str, body_file: str) -> tuple[str, int]:
    existing = find_issue(repository, title)
    if existing is not None:
        # A failed comment write does not destroy quarantine: the issue itself is
        # already the durable state, so never create a duplicate as fallback.
        try:
            run_gh(
                [
                    "issue",
                    "comment",
                    str(existing),
                    "--repo",
                    repository,
                    "--body-file",
                    body_file,
                ]
            )
        except IssueError as exc:
            print(f"warning: maintenance issue exists but comment failed: {exc}")
        return "updated", existing

    try:
        result = run_gh(
            [
                "issue",
                "create",
                "--repo",
                repository,
                "--title",
                title,
                "--body-file",
                body_file,
            ],
            timeout=120,
        )
        # gh create prints the issue URL; resolve by exact title so callers receive
        # stable structured state regardless of output formatting.
        del result
    except IssueError as create_error:
        for _ in range(6):
            time.sleep(3)
            try:
                confirmed = find_issue(repository, title, attempts=1)
            except IssueError:
                continue
            if confirmed is not None:
                return "created-after-client-error", confirmed
        raise create_error

    confirmed = find_issue(repository, title)
    if confirmed is None:
        raise IssueError("gh issue create returned success but the exact issue is not visible")
    return "created", confirmed


def close_issue_if_open(repository: str, title: str) -> tuple[str, int | None]:
    existing = find_issue(repository, title)
    if existing is None:
        return "already-closed", None
    try:
        run_gh(
            [
                "issue",
                "close",
                str(existing),
                "--repo",
                repository,
                "--reason",
                "completed",
            ]
        )
    except IssueError as exc:
        # Closing is idempotent; confirm state before reporting failure.
        confirmed = find_issue(repository, title, attempts=1)
        if confirmed is None:
            return "closed-after-client-error", existing
        raise exc
    return "closed", existing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file")
    parser.add_argument("--close-if-open", action="store_true")
    args = parser.parse_args()
    if args.close_if_open:
        action, number = close_issue_if_open(args.repository, args.title)
    else:
        if not args.body_file:
            parser.error("--body-file is required unless --close-if-open is used")
        action, number = upsert_issue(args.repository, args.title, args.body_file)
    print(f"{action}:{'' if number is None else number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
