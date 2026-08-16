#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
bundle="${tmp}/bundle"
mkdir -p "${bundle}"

cat > "${bundle}/chrome" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${bundle}/chrome"
cat > "${bundle}/chrome-wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  printf 'Chromium %s%s\n' "${FAKE_CHROMIUM_VERSION:-151.0.7922.75}" "${FAKE_CHROMIUM_VERSION_SUFFIX:-}"
  if [ -n "${FAKE_CHROMIUM_VERSION_EXTRA_LINE:-}" ]; then
    printf '%s\n' "${FAKE_CHROMIUM_VERSION_EXTRA_LINE}"
  fi
  exit 0
fi
if printf '%s\n' "$@" | grep -qx -- '--dump-dom'; then
  if ! printf '%s\n' "$@" | grep -qx -- '--incognito'; then
    echo 'runtime smoke must isolate persistent profile storage with --incognito' >&2
    exit 22
  fi
  if [ "${FAKE_HEADLESS_STATUS:-0}" -ne 0 ]; then
    exit "${FAKE_HEADLESS_STATUS}"
  fi
  echo '<html><body>chromium-i686-runtime-smoke</body></html>'
  exit 0
fi
exit 2
EOF
chmod +x "${bundle}/chrome-wrapper"
printf 'fake egl\n' > "${bundle}/libEGL.so"
printf 'fake gles\n' > "${bundle}/libGLESv2.so"


REPAIR_CALLS=0
repair_missing_i386_runtime_for_binary() {
  REPAIR_CALLS=$((REPAIR_CALLS + 1))
  if [ "${FAKE_REPAIR_STATUS:-0}" -ne 0 ]; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS="${FAKE_REPAIR_CLASS:-deterministic_build}"
    return "${FAKE_REPAIR_STATUS}"
  fi
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  return 0
}

bounded_ldd() {
  if [ "${FAKE_LDD_MISSING:-0}" = 1 ]; then
    echo 'libmissing.so => not found'
  else
    echo 'libc.so.6 => /lib/i386-linux-gnu/libc.so.6 (0x00000000)'
  fi
}

smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null
[ -z "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" ]
[ "${REPAIR_CALLS}" -eq 3 ]

# Chromium 151 currently emits a trailing space in its version banner. Trailing
# whitespace is insignificant, but the normalized full output must still match.
FAKE_CHROMIUM_VERSION_SUFFIX=$' \t'
export FAKE_CHROMIUM_VERSION_SUFFIX
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null
[ -z "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" ]
unset FAKE_CHROMIUM_VERSION_SUFFIX

# Extra output must not be hidden by normalization; compare the entire banner.
FAKE_CHROMIUM_VERSION_EXTRA_LINE='unexpected diagnostic'
export FAKE_CHROMIUM_VERSION_EXTRA_LINE
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
extra_line_status=$?
set -e
[ "${extra_line_status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = deterministic_build ]
unset FAKE_CHROMIUM_VERSION_EXTRA_LINE

mv "${bundle}/libEGL.so" "${bundle}/libEGL.so.missing"
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
missing_gpu_status=$?
set -e
[ "${missing_gpu_status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = deterministic_build ]
mv "${bundle}/libEGL.so.missing" "${bundle}/libEGL.so"

FAKE_REPAIR_STATUS=1
FAKE_REPAIR_CLASS=infrastructure
export FAKE_REPAIR_STATUS FAKE_REPAIR_CLASS
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
repair_failure_status=$?
set -e
[ "${repair_failure_status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = infrastructure ]
unset FAKE_REPAIR_STATUS FAKE_REPAIR_CLASS

FAKE_LDD_MISSING=1
export FAKE_LDD_MISSING
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = deterministic_build ]
unset FAKE_LDD_MISSING

FAKE_CHROMIUM_VERSION=151.0.7922.74
export FAKE_CHROMIUM_VERSION
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = deterministic_build ]
unset FAKE_CHROMIUM_VERSION

# A prefix collision must not satisfy exact version-line validation.
FAKE_CHROMIUM_VERSION=151.0.7922.750
export FAKE_CHROMIUM_VERSION
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
prefix_status=$?
set -e
[ "${prefix_status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = deterministic_build ]
unset FAKE_CHROMIUM_VERSION

FAKE_HEADLESS_STATUS=3
export FAKE_HEADLESS_STATUS
set +e
smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" = deterministic_build ]
unset FAKE_HEADLESS_STATUS

[ "$(classify_runtime_smoke_status 124)" = infrastructure ]
[ "$(classify_runtime_smoke_status 137)" = infrastructure ]
[ "$(classify_runtime_smoke_status 143)" = infrastructure ]
[ "$(classify_runtime_smoke_status 3)" = deterministic_build ]

echo "release runtime smoke contract tests passed"
