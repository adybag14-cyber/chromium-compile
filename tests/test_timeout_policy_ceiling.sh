#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
export RUNNER_TEMP="$(mktemp -d)"
trap 'rm -rf "${RUNNER_TEMP}"' EXIT
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

validate_timeout_seconds 1 test_timeout 10
validate_timeout_seconds 10 test_timeout 10
for bad in 0 abc 11 999999999999999999999; do
  if validate_timeout_seconds "${bad}" test_timeout 10 >/dev/null 2>&1; then
    echo "expected invalid timeout ${bad} to fail" >&2
    exit 1
  fi
done

child_calls=0
timeout() { child_calls=$((child_calls + 1)); return 0; }
swapon() { child_calls=$((child_calls + 1)); return 0; }

CHROMIUM_I686_HARD_MAX_EXTERNAL_TIMEOUT_SECONDS=3600
if bounded_external 3601 echo nope >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_GH_TIMEOUT_SECONDS=1201
if bounded_gh api rate_limit >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_REMOVE_TIMEOUT_SECONDS=901
if bounded_rm_rf /tmp/nope >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_SYSTEM_CLEANUP_TIMEOUT_SECONDS=601
if bounded_sudo_rm_rf /tmp/nope >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_SWAP_TIMEOUT_SECONDS=601
if ensure_swap >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_LDD_TIMEOUT_SECONDS=61
if bounded_ldd /bin/true >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS=601
if bounded_discovery echo nope >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]
if ensure_apt_file_i386_metadata >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_APT_FILE_SEARCH_TIMEOUT_SECONDS=61
if apt_file_search_i386 /usr/lib/i386-linux-gnu/libNope.so.1 >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_RUNTIME_SMOKE_TIMEOUT_SECONDS=301
if smoke_test_i686_runtime_bundle /tmp/nope 151.0.7922.108 >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS=3601
if prepare_chromium_source 151.0.7922.108 >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS=1801
if bounded_checkpoint_archive echo nope >/dev/null 2>&1; then exit 1; fi
[ "${child_calls}" -eq 0 ]

# Valid limits still reach the child wrapper.
CHROMIUM_I686_REMOVE_TIMEOUT_SECONDS=900
bounded_rm_rf /tmp/nope >/dev/null 2>&1
[ "${child_calls}" -eq 1 ]

echo "timeout policy ceiling contract tests passed"
