#!/usr/bin/env bash
set -euo pipefail

export GITHUB_WORKSPACE="$(pwd)"
export RUNNER_TEMP="$(mktemp -d)"
trap 'rm -rf "${RUNNER_TEMP}"' EXIT
source .github/scripts/chromium_i686_common.sh

fail() {
  echo "runtime repair contract failure: $*" >&2
  exit 1
}

# Avoid network access. Each test controls apt-file/apt-cache behavior explicitly.
ensure_apt_file_i386_metadata() { return 0; }

apt-file() {
  case "$*" in
    *libMysteryProvider.so.7*) printf '%s\n' libunique ;;
    *libMysteryAmbiguous.so.7*) printf '%s\n' libalpha libbeta ;;
    *libMysteryUnavailable.so.7*) printf '%s\n' libunavailable ;;
  esac
}

apt-cache() {
  [ "${1:-}" = policy ] || return 2
  case "${2:-}" in
    libqt5widgets5:i386|libunique:i386|libalpha:i386|libbeta:i386)
      printf '%s\n' "${2}:" '  Candidate: 1.0' ;;
    libunavailable:i386)
      printf '%s\n' "${2}:" '  Candidate: (none)' ;;
    *)
      printf '%s\n' "${2:-unknown}:" '  Candidate: (none)' ;;
  esac
}

resolve_i386_package_for_soname libQt5Widgets.so.5
[ "${I386_RESOLVED_PACKAGE}" = libqt5widgets5:i386 ] || fail "derived provider was not selected"

resolve_i386_package_for_soname libMysteryProvider.so.7
[ "${I386_RESOLVED_PACKAGE}" = libunique:i386 ] || fail "apt-file fallback provider was not selected"

set +e
resolve_i386_package_for_soname libMysteryAmbiguous.so.7 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ] || fail "ambiguous provider should fail"
[ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = deterministic_build ] || fail "ambiguous provider should be deterministic"

set +e
resolve_i386_package_for_soname libMysteryUnavailable.so.7 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ] || fail "unavailable provider should fail"
[ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = deterministic_build ] || fail "unavailable provider should be deterministic"

# A provider that installs successfully but leaves the same SONAME unresolved must
# stop after one install rather than repeating identical apt cycles.
I386_SONAME_PACKAGES[libStuck.so]=libstuck:i386
ldd() { printf '%s\n' 'libStuck.so => not found'; }
dpkg-query() { return 1; }
SUDO_CALLS="${RUNNER_TEMP}/sudo-calls"
: > "${SUDO_CALLS}"
apt-get() { return 0; }
sudo() {
  printf '%s\n' "$*" >> "${SUDO_CALLS}"
  return 0
}
set +e
repair_missing_i386_runtime_for_binary "${RUNNER_TEMP}/fake-binary" >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ] || fail "no-progress repair should fail"
[ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = deterministic_build ] || fail "no-progress repair should be deterministic"
[ "$(wc -l < "${SUDO_CALLS}")" -eq 1 ] || fail "no-progress repair repeated the package install"
grep -q 'libstuck:i386' "${SUDO_CALLS}" || fail "expected package was not selected"

# Commands invoked inside the reported-tool loop must not be able to consume the
# loop's pending stdin and skip later tools.
TOOL_DIR="${RUNNER_TEMP}/out"
mkdir -p "${TOOL_DIR}"
: > "${TOOL_DIR}/tool-one"
: > "${TOOL_DIR}/tool-two"
chmod +x "${TOOL_DIR}/tool-one" "${TOOL_DIR}/tool-two"
OUT_DIR="${TOOL_DIR}"
file() { printf '%s\n' "$1: ELF 32-bit LSB pie executable, Intel 80386"; }
CALLS="${RUNNER_TEMP}/repair-calls"
: > "${CALLS}"
repair_missing_i386_runtime_for_binary() {
  # Deliberately try to read stdin. </dev/null at the caller must isolate this.
  read -r _ignored || true
  printf '%s\n' "$1" >> "${CALLS}"
  I386_RUNTIME_REPAIR_CHANGED=true
  return 0
}
LOG="${RUNNER_TEMP}/build.log"
printf '%s\n' \
  './tool-one: error while loading shared libraries: libOne.so: cannot open shared object file' \
  './tool-two: error while loading shared libraries: libTwo.so: cannot open shared object file' > "${LOG}"
repair_i386_runtime_from_build_log "${LOG}"
[ "$(wc -l < "${CALLS}")" -eq 2 ] || fail "reported-tool loop skipped a tool"
sed -n '1p' "${CALLS}" | grep -q 'tool-one$' || fail "tool-one repair order changed"
sed -n '2p' "${CALLS}" | grep -q 'tool-two$' || fail "tool-two repair order changed"

# A successful scan with no installed package change must not claim that the host
# changed; the build loop uses this flag to avoid pointless retries.
repair_missing_i386_runtime_for_binary() {
  I386_RUNTIME_REPAIR_CHANGED=false
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  return 0
}
verify_or_repair_i386_runtime_dependencies
[ "${I386_RUNTIME_REPAIR_CHANGED}" = false ] || fail "no-change scan incorrectly requested a build retry"

echo "i386 runtime repair contract tests passed"
