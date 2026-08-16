#!/usr/bin/env bash
set -euo pipefail

export GITHUB_WORKSPACE="$(pwd)"
export RUNNER_TEMP="$(mktemp -d)"
trap 'rm -rf "${RUNNER_TEMP}"' EXIT
source .github/scripts/chromium_i686_common.sh
# Preserve the real metadata helper so the contract can inject a timeout before
# replacing it with the no-network stub used by provider-resolution tests.
eval "$(declare -f ensure_apt_file_i386_metadata | sed '1s/ensure_apt_file_i386_metadata/real_ensure_apt_file_i386_metadata/')"

fail() {
  echo "runtime repair contract failure: $*" >&2
  exit 1
}

# Platform detection must never leak generic os-release variables into its caller.
# This reproduces the regression that turned Chromium 151.0.7922.108 into
# "22.04.5 LTS (Jammy Jellyfish)" during source URL construction.
OS_RELEASE_FIXTURE="${RUNNER_TEMP}/os-release"
cat > "${OS_RELEASE_FIXTURE}" <<'EOF'
ID=ubuntu
VERSION_ID="99.04"
VERSION="99.04 LTS (Future Fixture)"
EOF
VERSION="151.0.7922.108"
ID="caller-id"
VERSION_ID="caller-version-id"
CHROMIUM_I686_OS_RELEASE_FILE="${OS_RELEASE_FIXTURE}"
dpkg-architecture() { printf '%s\n' i386-linux-gnu; }
detect_runner_platform >/dev/null
[ "${VERSION}" = "151.0.7922.108" ] || fail "platform detection clobbered caller VERSION"
[ "${ID}" = "caller-id" ] || fail "platform detection clobbered caller ID"
[ "${VERSION_ID}" = "caller-version-id" ] || fail "platform detection clobbered caller VERSION_ID"
[ "${RUNNER_DISTRO_ID}" = ubuntu ] || fail "platform detection did not read distro ID"
[ "${RUNNER_DISTRO_VERSION_ID}" = 99.04 ] || fail "platform detection did not read distro version"
unset CHROMIUM_I686_OS_RELEASE_FILE
unset -f dpkg-architecture

# A bounded apt-file metadata refresh timeout is transient runner/repository
# infrastructure, not a deterministic Chromium build failure.
(
  apt-file() { return 0; }
  timeout() { return 124; }
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  if real_ensure_apt_file_i386_metadata >/dev/null 2>&1; then
    fail "timed-out apt-file metadata refresh unexpectedly succeeded"
  fi
  [ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = infrastructure ] \
    || fail "apt-file metadata timeout was not infrastructure"
)

# Avoid network access. Each test controls apt-file/apt-cache behavior explicitly.
APT_FILE_METADATA_CALLS=0
ensure_apt_file_i386_metadata() {
  APT_FILE_METADATA_CALLS=$((APT_FILE_METADATA_CALLS + 1))
  return 0
}

apt_file_search_i386() {
  case "$*" in
    *libMysteryProvider.so.7*) printf '%s\n' libunique ;;
    *libMysteryAmbiguous.so.7*) printf '%s\n' libalpha libbeta ;;
    *libMysteryUnavailable.so.7*) printf '%s\n' libunavailable ;;
    *libMysteryTimeout.so.7*) return 124 ;;
    *libMysteryInvalid.so.7*) return 2 ;;
  esac
}

apt-cache() {
  [ "${1:-}" = policy ] || return 2
  case "${2:-}" in
    libqt5widgets5:i386|libunique:i386|libalpha:i386|libbeta:i386|libstuck:i386|libglib2.0-0t64:i386|libatk1.0-0:i386|libatk-bridge2.0-0:i386|libatspi2.0-0:i386|libcups2:i386|libcairo2:i386|libpango-1.0-0:i386|libxcomposite1:i386|libxdamage1:i386|libxfixes3:i386|libxrandr2:i386|libxtst6:i386)
      printf '%s\n' "${2}:" '  Candidate: 1.0' ;;
    libunavailable:i386)
      printf '%s\n' "${2}:" '  Candidate: (none)' ;;
    *)
      printf '%s\n' "${2:-unknown}:" '  Candidate: (none)' ;;
  esac
}

resolve_i386_package_for_soname libQt5Widgets.so.5
[ "${I386_RESOLVED_PACKAGE}" = libqt5widgets5:i386 ] || fail "derived provider was not selected"

# Chromium's standard packaged desktop runtime must resolve from bounded package
# metadata without paying the large apt-file Contents refresh penalty.
standard_runtime_mappings=(
  'libatk-1.0.so.0=libatk1.0-0:i386'
  'libatk-bridge-2.0.so.0=libatk-bridge2.0-0:i386'
  'libatspi.so.0=libatspi2.0-0:i386'
  'libcups.so.2=libcups2:i386'
  'libcairo.so.2=libcairo2:i386'
  'libpango-1.0.so.0=libpango-1.0-0:i386'
  'libXcomposite.so.1=libxcomposite1:i386'
  'libXdamage.so.1=libxdamage1:i386'
  'libXfixes.so.3=libxfixes3:i386'
  'libXrandr.so.2=libxrandr2:i386'
  'libXtst.so.6=libxtst6:i386'
)
for mapping in "${standard_runtime_mappings[@]}"; do
  soname="${mapping%%=*}"
  expected="${mapping#*=}"
  before="${APT_FILE_METADATA_CALLS}"
  resolve_i386_package_for_soname "${soname}"
  [ "${I386_RESOLVED_PACKAGE}" = "${expected}" ] || fail "preferred provider mismatch for ${soname}"
  [ "${APT_FILE_METADATA_CALLS}" -eq "${before}" ] || fail "standard runtime mapping ${soname} fell into apt-file metadata"
done

# Ubuntu 24.04-style time64 package renames must be discovered from a stale preferred mapping
# The host running this contract test must not satisfy the stale package through its own dpkg database.
dpkg-query() { return 1; }
# without falling into apt-file Contents metadata.
I386_SONAME_PACKAGES[libglib-2.0.so.0]=libglib2.0-0:i386
resolve_i386_package_for_soname libglib-2.0.so.0
[ "${I386_RESOLVED_PACKAGE}" = libglib2.0-0t64:i386 ] || fail "time64 provider variant was not selected"

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


set +e
resolve_i386_package_for_soname libMysteryTimeout.so.7 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ] || fail "timed-out apt-file search should fail resolution"
[ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = infrastructure ] || fail "apt-file timeout should be infrastructure"

set +e
resolve_i386_package_for_soname libMysteryInvalid.so.7 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ] || fail "invalid apt-file invocation should fail resolution"
[ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = deterministic_build ] || fail "apt-file invalid invocation should be deterministic"

# A provider that installs successfully but leaves the same SONAME unresolved must
# stop after one install rather than repeating identical apt cycles.
I386_SONAME_PACKAGES[libStuck.so]=libstuck:i386
bounded_ldd() { printf '%s\n' 'libStuck.so => not found'; }
dpkg-query() { return 1; }
SUDO_CALLS="${RUNNER_TEMP}/sudo-calls"
: > "${SUDO_CALLS}"
bounded_apt_get_simulate() { return 0; }
bounded_sudo_apt_get() {
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

# A normal loader rejection is deterministic; bounded timeout/exec failures are
# infrastructure so a healthy fresh runner may recover.
(
  bounded_ldd() { printf '%s\n' 'ldd probe failed' >&2; return 1; }
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  if repair_missing_i386_runtime_for_binary "${RUNNER_TEMP}/fake-binary" >/dev/null 2>&1; then
    fail "runtime repair succeeded despite failed bounded ldd probe"
  fi
  [ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = deterministic_build ] \
    || fail "ordinary failed bounded ldd probe was not deterministic_build"
)
(
  bounded_ldd() { return 124; }
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  if repair_missing_i386_runtime_for_binary "${RUNNER_TEMP}/fake-binary" >/dev/null 2>&1; then
    fail "runtime repair succeeded despite timed-out bounded ldd probe"
  fi
  [ "${I386_RUNTIME_REPAIR_FAILURE_CLASS}" = infrastructure ] \
    || fail "timed-out bounded ldd probe was not infrastructure"
)

# Commands invoked inside the reported-tool loop must not be able to consume the
# loop's pending stdin and skip later tools.
TOOL_DIR="${RUNNER_TEMP}/out"
mkdir -p "${TOOL_DIR}"
: > "${TOOL_DIR}/tool-one"
: > "${TOOL_DIR}/tool-two"
: > "${TOOL_DIR}/libtarget-shim.so"
chmod +x "${TOOL_DIR}/tool-one" "${TOOL_DIR}/tool-two" "${TOOL_DIR}/libtarget-shim.so"
OUT_DIR="${TOOL_DIR}"
file() {
  case "$1" in
    *libtarget-shim.so) printf '%s\n' "$1: ELF 32-bit LSB shared object, Intel 80386" ;;
    *) printf '%s\n' "$1: ELF 32-bit LSB pie executable, Intel 80386" ;;
  esac
}
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
[ "$(wc -l < "${CALLS}")" -eq 2 ] || fail "reported-tool loop skipped a tool or scanned a shared target object"
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
