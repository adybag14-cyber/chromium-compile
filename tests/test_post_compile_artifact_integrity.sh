#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"
source "${ROOT}/.github/scripts/chromium_i686_resume.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
WORKSPACE="${tmp}"
CHROMIUM_SRC="${tmp}/chromium"
OUT_DIR="${tmp}/out"
CHECKPOINT_DIR="${tmp}/checkpoints"
CHECKPOINT_ARCHIVE="${CHECKPOINT_DIR}/out-Release_x86.tar.zst"
CHECKPOINT_SHA256="${CHECKPOINT_ARCHIVE}.sha256"
CHECKPOINT_MANIFEST="${CHECKPOINT_DIR}/checkpoint-manifest.json"
mkdir -p "${CHROMIUM_SRC}" "${OUT_DIR}" "${CHECKPOINT_DIR}"

# Packaging: caller uses `if ! package...`; function must explicitly return and classify.
bounded_external() { return 2; }
set +e
package_chromium_i686 151.0.7922.108 >/dev/null 2>&1
package_runtime_bad=$?
set -e
[ "${package_runtime_bad}" -ne 0 ]
[ "${CHROMIUM_PACKAGE_FAILURE_CLASS}" = deterministic_build ]

bounded_external() { return 124; }
set +e
package_chromium_i686 151.0.7922.108 >/dev/null 2>&1
package_runtime_timeout=$?
set -e
[ "${package_runtime_timeout}" -ne 0 ]
[ "${CHROMIUM_PACKAGE_FAILURE_CLASS}" = infrastructure ]

# Simulate a successful runtime collector, then fail filesystem cleanup.
bounded_external() {
  local _timeout="$1"
  shift
  if [[ "$*" == *chromium_linux_runtime.py* ]]; then
    local output=""
    local prev=""
    for arg in "$@"; do
      if [ "${prev}" = "--output-list" ]; then output="${arg}"; break; fi
      prev="${arg}"
    done
    printf '%s\n' chrome > "${output}"
    return 0
  fi
  return 0
}
rm() { return 1; }
set +e
package_chromium_i686 151.0.7922.108 >/dev/null 2>&1
package_cleanup_status=$?
set -e
[ "${package_cleanup_status}" -ne 0 ]
[ "${CHROMIUM_PACKAGE_FAILURE_CLASS}" = infrastructure ]
unset -f rm

# Checkpoint graph absence is deterministic.
rm -f "${OUT_DIR}/build.ninja" "${OUT_DIR}/args.gn"
set +e
create_out_checkpoint 151.0.7922.108 3 >/dev/null 2>&1
checkpoint_graph_status=$?
set -e
[ "${checkpoint_graph_status}" -ne 0 ]
[ "${CHECKPOINT_CREATE_FAILURE_CLASS}" = deterministic_build ]

printf 'ninja\n' > "${OUT_DIR}/build.ninja"
printf 'args\n' > "${OUT_DIR}/args.gn"
ensure_build_disk_space() { return 1; }
set +e
create_out_checkpoint 151.0.7922.108 3 >/dev/null 2>&1
checkpoint_disk_status=$?
set -e
[ "${checkpoint_disk_status}" -ne 0 ]
[ "${CHECKPOINT_CREATE_FAILURE_CLASS}" = infrastructure ]
unset -f ensure_build_disk_space

# Archive timeout is infrastructure.
ensure_build_disk_space() { return 0; }
bounded_external() { return 124; }
set +e
create_out_checkpoint 151.0.7922.108 3 >/dev/null 2>&1
checkpoint_archive_timeout=$?
set -e
[ "${checkpoint_archive_timeout}" -ne 0 ]
[ "${CHECKPOINT_CREATE_FAILURE_CLASS}" = infrastructure ]

# A produced archive rejected by the structural validator is deterministic.
call_index=0
bounded_external() {
  call_index=$((call_index + 1))
  if [ "${call_index}" -eq 1 ]; then
    printf 'synthetic checkpoint archive\n' > "${CHECKPOINT_ARCHIVE}"
    return 0
  fi
  if [ "${call_index}" -eq 2 ]; then return 2; fi
  return 0
}
set +e
create_out_checkpoint 151.0.7922.108 3 >/dev/null 2>&1
checkpoint_validator_status=$?
set -e
[ "${checkpoint_validator_status}" -ne 0 ]
[ "${CHECKPOINT_CREATE_FAILURE_CLASS}" = deterministic_build ]

echo "post-compile artifact integrity contract tests passed"
