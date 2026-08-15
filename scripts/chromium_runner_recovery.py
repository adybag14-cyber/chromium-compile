#!/usr/bin/env python3
"""Validate and compute bounded fresh-runner recovery attempts."""
from __future__ import annotations

import argparse
import re

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
INTEGER_RE = re.compile(r"^(0|[1-9][0-9]?)$")
HARD_MAX_STAGE = 50
HARD_MAX_RETRIES = 10


class RecoveryPolicyError(ValueError):
    pass


def _bounded_int(value: str, name: str, maximum: int, *, minimum: int = 0) -> int:
    if not INTEGER_RE.fullmatch(value):
        raise RecoveryPolicyError(f"{name} must be a short non-negative integer")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise RecoveryPolicyError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def next_retry(version: str, stage: str, retry_count: str, max_retries: str) -> int:
    if not VERSION_RE.fullmatch(version):
        raise RecoveryPolicyError(f"invalid Chromium version: {version!r}")
    _bounded_int(stage, "stage", HARD_MAX_STAGE, minimum=1)
    current = _bounded_int(retry_count, "retry_count", HARD_MAX_RETRIES)
    maximum = _bounded_int(max_retries, "max_retries", HARD_MAX_RETRIES)
    if current >= maximum:
        raise RecoveryPolicyError(
            f"fresh-runner recovery budget exhausted ({current}/{maximum})"
        )
    return current + 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--retry-count", required=True)
    parser.add_argument("--max-retries", required=True)
    args = parser.parse_args()
    try:
        value = next_retry(args.version, args.stage, args.retry_count, args.max_retries)
    except RecoveryPolicyError as exc:
        parser.error(str(exc))
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
