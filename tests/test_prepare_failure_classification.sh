#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"
source "${ROOT}/.github/scripts/chromium_i686_resume.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
CHROMIUM_SRC="${tmp}/chromium"
DEPOT_TOOLS="${tmp}/depot_tools"
GN_BINARY="${tmp}/gn/gn"
CCACHE_DIR="${tmp}/ccache"
mkdir -p "${CHROMIUM_SRC}"

[ "$(classify_prepare_command_status 124 deterministic_build)" = infrastructure ]
[ "$(classify_prepare_command_status 137 deterministic_build)" = infrastructure ]
[ "$(classify_prepare_command_status 143 deterministic_build)" = infrastructure ]
[ "$(classify_prepare_command_status 2 deterministic_build)" = deterministic_build ]
[ "$(classify_prepare_command_status 2 infrastructure)" = infrastructure ]

# Invalid/mutable source-declared tooling is deterministic and must never reach network setup.
printf '%s\n' "vars = {'gn_version': 'latest', 'depot_tools_revision': 'main'}" > "${CHROMIUM_SRC}/DEPS"
set +e
install_depot_tools >/dev/null 2>&1
depot_status=$?
set -e
[ "${depot_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = deterministic_build ]

# A successful toolchain installer that omits its promised outputs is deterministic contract drift.
bounded_external() { return 0; }
set +e
install_chromium_clang >/dev/null 2>&1
clang_status=$?
set -e
[ "${clang_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = deterministic_build ]

set +e
install_i386_sysroot >/dev/null 2>&1
sysroot_status=$?
set -e
[ "${sysroot_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = deterministic_build ]

# An unsupported host architecture is deterministic before any CIPD mutation.
chromium_gn_version() { printf '%s\n' 'git_revision:0123456789abcdef0123456789abcdef01234567'; }
uname() { printf '%s\n' mips64; }
set +e
install_gn_from_cipd >/dev/null 2>&1
gn_host_status=$?
set -e
[ "${gn_host_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = deterministic_build ]
unset -f uname

# Once GN itself is installed, graph rejection is deterministic; timeout statuses remain infrastructure.
mkdir -p "$(dirname "${GN_BINARY}")"
cat > "${GN_BINARY}" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
  echo fake-gn
  exit 0
fi
exit 2
EOF
chmod +x "${GN_BINARY}"
install_gn_from_cipd() { CHROMIUM_PREPARE_FAILURE_CLASS=""; return 0; }
bounded_external() { local _timeout="$1"; shift; "$@"; }
chromium_i686_gn_args() { printf '%s\n' 'target_os="linux" target_cpu="x86"'; }
set +e
configure_gn >/dev/null 2>&1
gn_graph_status=$?
set -e
[ "${gn_graph_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = deterministic_build ]

# Timestamp I/O failure is a runner/filesystem problem.
find() { return 1; }
set +e
normalize_chromium_resume_inputs >/dev/null 2>&1
normalize_status=$?
set -e
[ "${normalize_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = infrastructure ]
unset -f find

# Missing version metadata makes the maintained port patch layer deterministically incompatible.
rm -f "${CHROMIUM_SRC}/chrome/VERSION"
set +e
patch_build_gn_for_x86_linux >/dev/null 2>&1
patch_status=$?
set -e
[ "${patch_status}" -ne 0 ]
[ "${CHROMIUM_PREPARE_FAILURE_CLASS}" = deterministic_build ]

# The checkpoint fast path cannot skip validation by caller assertion alone.
archive="${tmp}/checkpoint.tar.zst"
printf 'not-empty\n' > "${archive}"
clear_checkpoint_validation_state
set +e
restore_out_checkpoint "${archive}" 151.0.7922.108 3 true \
  1111111111111111111111111111111111111111 3 12345 1 >/dev/null 2>&1
restore_status=$?
set -e
[ "${restore_status}" -ne 0 ]
[ "${CHECKPOINT_RESTORE_FAILURE_CLASS}" = deterministic_build ]

# Checkpoint validation timeouts are infrastructure; ordinary invalid bytes are deterministic.
bundle="${tmp}/bundle.tar.zst"
printf 'bytes\n' > "${bundle}"
bounded_external() { return 124; }
set +e
checkpoint_bundle_is_usable "${bundle}" 151.0.7922.108 3 >/dev/null 2>&1
bundle_timeout_status=$?
set -e
[ "${bundle_timeout_status}" -ne 0 ]
[ "${CHECKPOINT_BUNDLE_FAILURE_CLASS}" = infrastructure ]

bounded_external() { return 2; }
set +e
checkpoint_bundle_is_usable "${bundle}" 151.0.7922.108 3 >/dev/null 2>&1
bundle_invalid_status=$?
set -e
[ "${bundle_invalid_status}" -ne 0 ]
[ "${CHECKPOINT_BUNDLE_FAILURE_CLASS}" = deterministic_build ]

echo "prepare failure classification contract tests passed"
