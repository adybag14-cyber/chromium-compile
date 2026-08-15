#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
stats="${tmp}/source-stats.json"
target="${tmp}/source"
version=151.0.7922.108
sha="$(printf 'a%.0s' {1..64})"
printf '{"version":"%s","source_sha256":"%s","member_count":42,"unpacked_bytes":1000}\n' "${version}" "${sha}" > "${stats}"

source_archive_stats_are_usable "${stats}" "${version}" "${sha}"
set +e
source_archive_stats_are_usable "${stats}" "${version}" "$(printf 'b%.0s' {1..64})"
wrong_sha_status=$?
set -e
[ "${wrong_sha_status}" -ne 0 ]

CHROMIUM_I686_MAX_SOURCE_MEMBERS=41
set +e
source_archive_stats_are_usable "${stats}" "${version}" "${sha}"
over_member_status=$?
set -e
[ "${over_member_status}" -ne 0 ]
CHROMIUM_I686_MAX_SOURCE_MEMBERS=2000000

CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB=1
printf '{"version":"%s","source_sha256":"%s","member_count":42,"unpacked_bytes":1073741825}\n' "${version}" "${sha}" > "${stats}"
set +e
source_archive_stats_are_usable "${stats}" "${version}" "${sha}"
over_bytes_status=$?
set -e
[ "${over_bytes_status}" -ne 0 ]
CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB=80
printf '{"version":"%s","source_sha256":"%s","member_count":42,"unpacked_bytes":1000}\n' "${version}" "${sha}" > "${stats}"

CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB=0
df() {
  printf 'Filesystem 1-blocks Used Available Capacity Mounted on\n'
  printf '/dev/fake 10000 1000 %s 10%% /fake\n' "${FAKE_AVAILABLE_BYTES:-9000}"
}
FAKE_AVAILABLE_BYTES=9000
ensure_source_archive_extract_space "${stats}" "${target}" >/dev/null

FAKE_AVAILABLE_BYTES=999
set +e
ensure_source_archive_extract_space "${stats}" "${target}" >/dev/null 2>&1
low_status=$?
set -e
[ "${low_status}" -ne 0 ]

printf '{"version":"%s","source_sha256":"%s","member_count":0,"unpacked_bytes":1000}\n' "${version}" "${sha}" > "${stats}"
set +e
source_archive_stats_are_usable "${stats}" "${version}" "${sha}"
bad_member_status=$?
set -e
[ "${bad_member_status}" -ne 0 ]

CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB=bad
printf '{"version":"%s","source_sha256":"%s","member_count":42,"unpacked_bytes":1000}\n' "${version}" "${sha}" > "${stats}"
set +e
ensure_source_archive_extract_space "${stats}" "${target}" >/dev/null 2>&1
bad_reserve_status=$?
set -e
[ "${bad_reserve_status}" -ne 0 ]

echo "source archive stats/capacity contract tests passed"
