#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

validate_bounded_positive_policy 1 TEST_POLICY 32 >/dev/null
validate_bounded_positive_policy 32 TEST_POLICY 32 >/dev/null
for bad in 0 33 999999999999999999999 abc -1; do
  if validate_bounded_positive_policy "${bad}" TEST_POLICY 32 >/dev/null 2>&1; then
    echo "policy unexpectedly accepted: ${bad}" >&2
    exit 1
  fi
done

validate_artifact_size_bytes 1073741824 1 TEST_ARTIFACT_GIB 8 >/dev/null
set +e
validate_artifact_size_bytes 1073741825 1 TEST_ARTIFACT_GIB 8 >/dev/null 2>&1
oversized=$?
validate_artifact_size_bytes bad 1 TEST_ARTIFACT_GIB 8 >/dev/null 2>&1
malformed=$?
validate_artifact_size_bytes 1 9 TEST_ARTIFACT_GIB 8 >/dev/null 2>&1
policy_bad=$?
set -e
[ "${oversized}" -eq 2 ]
[ "${malformed}" -eq 1 ]
[ "${policy_bad}" -eq 2 ]

# Cached source stats must obey the current bounded policy, not just the SHA/version binding.
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
stats="${tmp}/stats.json"
cat > "${stats}" <<'JSON'
{"version":"151.0.7922.108","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","member_count":10,"unpacked_bytes":1024}
JSON
CHROMIUM_I686_MAX_SOURCE_MEMBERS=10
CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB=1
source_archive_stats_are_usable "${stats}" 151.0.7922.108 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
CHROMIUM_I686_MAX_SOURCE_MEMBERS=4000001
set +e
source_archive_stats_are_usable "${stats}" 151.0.7922.108 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
source_policy_status=$?
set -e
[ "${source_policy_status}" -ne 0 ]

echo "resource policy ceiling contract tests passed"
