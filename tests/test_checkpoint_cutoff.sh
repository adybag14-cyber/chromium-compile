#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

validate_checkpoint_minutes 1 >/dev/null
validate_checkpoint_minutes 340 >/dev/null
now="$(date +%s)"
validate_job_started_at "${now}" "${now}" >/dev/null

for bad in 0 341 999 999999999999999999999 abc -1; do
  if validate_checkpoint_minutes "${bad}" >/dev/null 2>&1; then
    echo "checkpoint minutes unexpectedly accepted: ${bad}" >&2
    exit 1
  fi
done
for bad in '' 0 abc -1 999999999999999999999999; do
  if validate_job_started_at "${bad}" "${now}" >/dev/null 2>&1; then
    echo "job start unexpectedly accepted: ${bad}" >&2
    exit 1
  fi
done
future="$((now + 301))"
if validate_job_started_at "${future}" "${now}" >/dev/null 2>&1; then
  echo "future job start unexpectedly accepted" >&2
  exit 1
fi

echo "checkpoint cutoff contract tests passed"
