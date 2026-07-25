#!/usr/bin/env bash
set -euo pipefail

log_runner_storage() {
  local label="${1:-snapshot}"
  echo "::group::Runner storage diagnostics: ${label}"
  echo "Timestamp: $(date --iso-8601=seconds)"
  echo "Runner: ${RUNNER_NAME:-unknown} (${RUNNER_OS:-unknown}/${RUNNER_ARCH:-unknown})"
  echo "--- Filesystem capacity ---"
  df -hT || true
  echo "--- Inode capacity ---"
  df -i || true
  echo "--- Workspace top-level usage ---"
  if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -d "${GITHUB_WORKSPACE}" ]; then
    du -xhd1 "${GITHUB_WORKSPACE}" 2>/dev/null | sort -h || true
  fi
  echo "--- Swap ---"
  swapon --show || true
  echo "::endgroup::"
}
