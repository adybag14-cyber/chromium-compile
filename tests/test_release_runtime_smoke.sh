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
  echo "Chromium ${FAKE_CHROMIUM_VERSION:-151.0.7922.75}"
  exit 0
fi
if printf '%s\n' "$@" | grep -qx -- '--dump-dom'; then
  if [ "${FAKE_HEADLESS_STATUS:-0}" -ne 0 ]; then
    exit "${FAKE_HEADLESS_STATUS}"
  fi
  echo '<html><body>chromium-i686-runtime-smoke</body></html>'
  exit 0
fi
exit 2
EOF
chmod +x "${bundle}/chrome-wrapper"

bounded_ldd() {
  if [ "${FAKE_LDD_MISSING:-0}" = 1 ]; then
    echo 'libmissing.so => not found'
  else
    echo 'libc.so.6 => /lib/i386-linux-gnu/libc.so.6 (0x00000000)'
  fi
}

smoke_test_i686_runtime_bundle "${bundle}" 151.0.7922.75 >/dev/null
[ -z "${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS}" ]

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
