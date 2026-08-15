#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
export RUNNER_TEMP="$(mktemp -d)"
trap 'rm -rf "${RUNNER_TEMP}"' EXIT

source "${ROOT}/.github/scripts/chromium_i686_common.sh"
# Keep an alias to the real mirror-rewrite function before policy mocks replace it.
eval "$(declare -f normalize_ubuntu_archive_mirrors | sed '1s/normalize_ubuntu_archive_mirrors/real_normalize_ubuntu_archive_mirrors/')"
eval "$(declare -f _run_sudo_apt_get_with_timeout | sed '1s/_run_sudo_apt_get_with_timeout/real_run_sudo_apt_get_with_timeout/')"

# Fast mirror-failure detection applies only to index refreshes; package installs keep the more tolerant transport policy.
apt_policy_log="${RUNNER_TEMP}/apt-policy.log"
timeout() {
  printf '%s\n' "$*" > "${apt_policy_log}"
  return 0
}
real_run_sudo_apt_get_with_timeout 180 update
grep -q 'Acquire::Retries=2' "${apt_policy_log}"
grep -q 'Acquire::http::Timeout=20' "${apt_policy_log}"
grep -q 'Acquire::https::Timeout=20' "${apt_policy_log}"
real_run_sudo_apt_get_with_timeout 900 install -y example
grep -q 'Acquire::Retries=3' "${apt_policy_log}"
grep -q 'Acquire::http::Timeout=30' "${apt_policy_log}"
grep -q 'Acquire::https::Timeout=30' "${apt_policy_log}"
unset -f timeout

# Timeout policy is bounded before any external command is run.
validate_apt_timeout_seconds 180 CHROMIUM_I686_APT_UPDATE_TIMEOUT_SECONDS 300
if validate_apt_timeout_seconds 301 CHROMIUM_I686_APT_UPDATE_TIMEOUT_SECONDS 300 >/dev/null 2>&1; then
  echo "expected oversized update timeout to fail" >&2
  exit 1
fi
if validate_apt_timeout_seconds 1801 CHROMIUM_I686_APT_TIMEOUT_SECONDS 1800 >/dev/null 2>&1; then
  echo "expected oversized install timeout to fail" >&2
  exit 1
fi

# Update gets exactly one canonical-mirror retry after a first failure.
calls=0
normalized=0
_run_sudo_apt_get_with_timeout() {
  calls=$((calls + 1))
  [ "${1}" = "180" ] || return 90
  shift
  [ "${1}" = update ] || return 91
  [ "${calls}" -eq 2 ]
}
normalize_ubuntu_archive_mirrors() {
  normalized=$((normalized + 1))
  return 0
}
CHROMIUM_I686_APT_UPDATE_TIMEOUT_SECONDS=180
bounded_sudo_apt_get update
[ "${calls}" -eq 2 ]
[ "${normalized}" -eq 1 ]

# Non-update commands never replay an uncertain package mutation.
calls=0
normalized=0
_run_sudo_apt_get_with_timeout() {
  calls=$((calls + 1))
  return 1
}
normalize_ubuntu_archive_mirrors() {
  normalized=$((normalized + 1))
  return 0
}
if bounded_sudo_apt_get install -y example >/dev/null 2>&1; then
  echo "expected failed install to stay failed" >&2
  exit 1
fi
[ "${calls}" -eq 1 ]
[ "${normalized}" -eq 0 ]

# If mirror normalization cannot be proven, update also fails after one attempt.
calls=0
normalized=0
_run_sudo_apt_get_with_timeout() {
  calls=$((calls + 1))
  return 1
}
normalize_ubuntu_archive_mirrors() {
  normalized=$((normalized + 1))
  return 1
}
if bounded_sudo_apt_get update >/dev/null 2>&1; then
  echo "expected update without safe mirror rewrite to fail" >&2
  exit 1
fi
[ "${calls}" -eq 1 ]
[ "${normalized}" -eq 1 ]

# Exercise the real Azure -> canonical Ubuntu rewrite against a temporary APT tree.
apt_root="${RUNNER_TEMP}/apt-root"
mkdir -p "${apt_root}/etc/apt/sources.list.d"
printf 'ID=ubuntu\nVERSION_ID="22.04"\n' > "${RUNNER_TEMP}/os-release"
printf 'http://azure.archive.ubuntu.com/ubuntu\nhttps://archive.ubuntu.com/ubuntu\n' > "${apt_root}/etc/apt/apt-mirrors.txt"
printf 'deb http://azure.archive.ubuntu.com/ubuntu jammy main universe\n' > "${apt_root}/etc/apt/sources.list"
printf 'URIs: http://azure.archive.ubuntu.com/ubuntu\nSuites: jammy\n' > "${apt_root}/etc/apt/sources.list.d/ubuntu.sources"
export CHROMIUM_I686_OS_RELEASE_FILE="${RUNNER_TEMP}/os-release"
export CHROMIUM_I686_APT_ROOT="${apt_root}"
sudo() { "$@"; }
real_normalize_ubuntu_archive_mirrors
grep -q 'archive.ubuntu.com/ubuntu' "${apt_root}/etc/apt/apt-mirrors.txt"
grep -q 'archive.ubuntu.com/ubuntu' "${apt_root}/etc/apt/sources.list"
grep -q 'archive.ubuntu.com/ubuntu' "${apt_root}/etc/apt/sources.list.d/ubuntu.sources"
if grep -R -q 'azure.archive.ubuntu.com' "${apt_root}/etc/apt"; then
  echo "Azure Ubuntu mirror remained after normalization" >&2
  exit 1
fi

echo "APT mirror resilience contract tests passed"
