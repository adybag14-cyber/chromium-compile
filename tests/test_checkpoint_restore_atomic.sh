#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"
source "${ROOT}/.github/scripts/chromium_i686_resume.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
CHROMIUM_SRC="${tmp}/chromium"
OUT_DIR="${CHROMIUM_SRC}/out/Release_x86"
mkdir -p "${OUT_DIR}"
printf 'old-state\n' > "${OUT_DIR}/old.marker"
printf 'old-ninja\n' > "${OUT_DIR}/build.ninja"
printf 'old-args\n' > "${OUT_DIR}/args.gn"

bad_archive="${tmp}/bad.tar.zst"
good_archive="${tmp}/good.tar.zst"
printf 'not-empty\n' > "${bad_archive}"
printf 'not-empty\n' > "${good_archive}"
printf '{"member_count":3,"unpacked_bytes":64}\n' > "${tmp}/checkpoint-archive-stats.json"

EXTRACT_MODE=fail
bounded_rm_rf() { rm -rf -- "$1"; }
bounded_external() {
  local _timeout="$1"
  shift
  if [ "${1:-}" = tar ]; then
    if [ "${EXTRACT_MODE}" = fail ]; then
      return 1
    fi
    local target=""
    local i
    for ((i=1; i<=$#; i++)); do
      if [ "${!i}" = "-C" ]; then
        local next=$((i + 1))
        target="${!next}"
        break
      fi
    done
    test -n "${target}"
    mkdir -p "${target}/Release_x86"
    printf 'new-ninja\n' > "${target}/Release_x86/build.ninja"
    printf 'new-args\n' > "${target}/Release_x86/args.gn"
    return 0
  fi
  command "$@"
}

# Caller discipline is not sufficient: the fast path must have matching validation state.
clear_checkpoint_validation_state
set +e
restore_out_checkpoint "${bad_archive}" 151.0.7922.108 3 true >/dev/null 2>&1
unvalidated_status=$?
set -e
[ "${unvalidated_status}" -ne 0 ]
[ "$(cat "${OUT_DIR}/old.marker")" = "old-state" ]

mark_checkpoint_bundle_validated "${bad_archive}" 151.0.7922.108 3
set +e
restore_out_checkpoint "${bad_archive}" 151.0.7922.108 3 true >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "$(cat "${OUT_DIR}/old.marker")" = "old-state" ]
[ "$(cat "${OUT_DIR}/build.ninja")" = "old-ninja" ]

EXTRACT_MODE=success
mark_checkpoint_bundle_validated "${good_archive}" 151.0.7922.108 3
restore_out_checkpoint "${good_archive}" 151.0.7922.108 3 true >/dev/null
[ ! -e "${OUT_DIR}/old.marker" ]
[ "$(cat "${OUT_DIR}/build.ninja")" = "new-ninja" ]
[ "$(cat "${OUT_DIR}/args.gn")" = "new-args" ]
[ "${CHECKPOINT_REQUIRES_GN_REFRESH}" = true ]

echo "checkpoint atomic restore contract tests passed"
