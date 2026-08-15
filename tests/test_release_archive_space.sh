#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
stats="${tmp}/stats.json"
target="${tmp}/target"
printf '{"member_count":12,"unpacked_bytes":1000}\n' > "${stats}"
CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB=0

df() {
  printf 'Filesystem 1-blocks Used Available Capacity Mounted on\n'
  printf '/dev/fake 10000 1000 %s 10%% /fake\n' "${FAKE_AVAILABLE_BYTES:-9000}"
}

FAKE_AVAILABLE_BYTES=9000
ensure_release_archive_extract_space "${stats}" "${target}" >/dev/null

FAKE_AVAILABLE_BYTES=999
set +e
ensure_release_archive_extract_space "${stats}" "${target}" >/dev/null 2>&1
low_status=$?
set -e
[ "${low_status}" -ne 0 ]

printf '{"member_count":12,"unpacked_bytes":"bad"}\n' > "${stats}"
FAKE_AVAILABLE_BYTES=9000
set +e
ensure_release_archive_extract_space "${stats}" "${target}" >/dev/null 2>&1
bad_stats_status=$?
set -e
[ "${bad_stats_status}" -ne 0 ]

CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB=not-a-number
printf '{"member_count":12,"unpacked_bytes":1000}\n' > "${stats}"
set +e
ensure_release_archive_extract_space "${stats}" "${target}" >/dev/null 2>&1
bad_reserve_status=$?
set -e
[ "${bad_reserve_status}" -ne 0 ]

echo "release archive extraction-space contract tests passed"
