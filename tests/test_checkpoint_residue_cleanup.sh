#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
CHROMIUM_SRC="${tmp}/chromium"
OUT_DIR="${CHROMIUM_SRC}/out/Release_x86"
mkdir -p "${OUT_DIR}"
mkdir -p "${CHROMIUM_SRC}/out/.checkpoint-restore-old"
mkdir -p "${CHROMIUM_SRC}/out/.Release_x86-before-restore-old"
mkdir -p "${CHROMIUM_SRC}/out/.unrelated-hidden"
bounded_rm_rf() { rm -rf -- "$@"; }
cleanup_stale_checkpoint_residue >/dev/null
[ ! -e "${CHROMIUM_SRC}/out/.checkpoint-restore-old" ]
[ ! -e "${CHROMIUM_SRC}/out/.Release_x86-before-restore-old" ]
[ -d "${CHROMIUM_SRC}/out/.unrelated-hidden" ]

rm -rf "${OUT_DIR}"
mkdir -p "${CHROMIUM_SRC}/out/.checkpoint-restore-preserve"
mkdir -p "${CHROMIUM_SRC}/out/.Release_x86-before-restore-preserve"
cleanup_stale_checkpoint_residue >/dev/null
[ -d "${CHROMIUM_SRC}/out/.checkpoint-restore-preserve" ]
[ -d "${CHROMIUM_SRC}/out/.Release_x86-before-restore-preserve" ]

echo "checkpoint residue cleanup contract tests passed"
