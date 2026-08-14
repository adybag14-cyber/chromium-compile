#!/usr/bin/env python3
"""Idempotently create or update a maintenance issue under GitHub API uncertainty."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Sequence


class IssueError(RuntimeError):
    """Raised when maintenance issue state cannot be established safely."""


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
                    "1000",
                    "--json",
                    "number,title",
                ]
            )
            payload = json.loads(result.stdout or "[]")
            if not isinstance(payload, list):
                raise IssueError("gh issue list returned non-list JSON")
            matches = [
                int(item["number"])
                for item in payload
                if isinstance(item, dict) and item.get("title") == title and "number" in item
            ]
            if len(matches) > 1:
                raise IssueError(f"Multiple open issues have the exact maintenance title: {title}")
            return matches[0] if matches else None
        except (IssueError, json.JSONDecodeError, TypeError, ValueError) as exc:
            last = exc if isinstance(exc, IssueError) else IssueError(str(exc))
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    args = parser.parse_args()
    action, number = upsert_issue(args.repository, args.title, args.body_file)
    print(f"{action}:{number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
