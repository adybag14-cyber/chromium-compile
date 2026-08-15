#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
bundle="${tmp}/runtime"
mkdir -p "${bundle}/locales"
printf 'locale\n' > "${bundle}/locales/en-US.pak"
printf 'data\n' > "${bundle}/icudtl.dat"
printf 'resources\n' > "${bundle}/resources.pak"
printf 'egl\n' > "${bundle}/libEGL.so"
printf 'gles\n' > "${bundle}/libGLESv2.so"
for executable in chrome chrome-wrapper chrome_crashpad_handler chrome_management_service chrome_sandbox; do
  cat > "${bundle}/${executable}" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod 0755 "${bundle}/${executable}"
done

validate_i686_runtime_bundle "${bundle}" >/dev/null
chmod 0644 "${bundle}/chrome_crashpad_handler"
set +e
validate_i686_runtime_bundle "${bundle}" >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
chmod 0755 "${bundle}/chrome_crashpad_handler"
validate_i686_runtime_bundle "${bundle}" >/dev/null

# Sandbox must be executable, but portable archives intentionally do not require
# setuid/root ownership; deployment policy decides whether to enable setuid sandbox.
chmod 0755 "${bundle}/chrome_sandbox"
validate_i686_runtime_bundle "${bundle}" >/dev/null

echo "runtime executable permission contract tests passed"
