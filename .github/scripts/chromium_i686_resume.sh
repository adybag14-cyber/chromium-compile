#!/usr/bin/env bash
set -euo pipefail

# Helpers for carrying a live Ninja build graph between fresh GitHub-hosted
# runners. Source chromium_i686_common.sh before sourcing this file.

normalize_chromium_resume_inputs() {
  local epoch="${CHROMIUM_RESUME_INPUT_EPOCH:-946684800}"
  local started_at
  started_at="$(date +%s)"

  if [ ! -d "${CHROMIUM_SRC:-}" ]; then
    echo "::error::Chromium source directory is unavailable for timestamp normalization."
    return 1
  fi

  echo "Normalizing non-output Chromium source/toolchain mtimes to @${epoch}."
  echo "This prevents files recreated on a fresh runner from appearing newer than restored Ninja outputs."

  find "${CHROMIUM_SRC}" \
    -path "${CHROMIUM_SRC}/out" -prune -o \
    -type f -print0 \
    | xargs -0 -r -n 512 touch -d "@${epoch}" --

  find "${CHROMIUM_SRC}" \
    -path "${CHROMIUM_SRC}/out" -prune -o \
    -type l -print0 \
    | xargs -0 -r -n 512 touch -h -d "@${epoch}" --

  echo "Timestamp normalization finished in $(( $(date +%s) - started_at )) seconds."
}

configure_gn_from_checkpoint_or_fresh() {
  local checkpoint_restored="${1:-false}"

  if [ "${checkpoint_restored}" = "true" ] \
      && [ -s "${OUT_DIR}/build.ninja" ] \
      && [ -s "${OUT_DIR}/args.gn" ]; then
    echo "Reusing build.ninja and args.gn from the restored checkpoint."
    echo "Ninja will regenerate the graph itself only if a stable input genuinely changed."
    return 0
  fi

  echo "No reusable GN graph was restored; generating a fresh Ninja graph."
  configure_gn
}

report_ninja_resume_state() {
  if [ ! -s "${OUT_DIR}/build.ninja" ]; then
    echo "No restored build.ninja exists; skipping Ninja resume diagnostics."
    return 0
  fi

  echo "Restored Ninja metadata:"
  stat -c '%y %s %n' \
    "${OUT_DIR}/build.ninja" \
    "${OUT_DIR}/.ninja_log" \
    "${OUT_DIR}/.ninja_deps" 2>/dev/null || true

  echo "First Ninja dirty-state explanations (dry run only):"
  (
    set +e
    set +o pipefail
    export NINJA_STATUS='[%f/%t] '
    timeout 45s ninja -C "${OUT_DIR}" -n -d explain chrome 2>&1 | head -n 120
    exit 0
  )
}

# Override the common implementation after this file is sourced. GNU tar's
# usual default archive format stores mtimes at whole-second resolution. Ninja
# records higher-resolution timestamps in .ninja_log/.ninja_deps, so rounding
# restored outputs can make valid work look stale. POSIX/PAX archives retain
# subsecond mtimes.
create_out_checkpoint() {
  if [ ! -d "${OUT_DIR}" ]; then
    echo "::error::Expected build output directory not found: ${OUT_DIR}"
    exit 1
  fi

  mkdir -p "${CHECKPOINT_DIR}"
  rm -f "${CHECKPOINT_ARCHIVE}"

  echo "Creating nanosecond-preserving Ninja output checkpoint..."
  du -sh "${OUT_DIR}" || true
  stat -c '%y %s %n' \
    "${OUT_DIR}/build.ninja" \
    "${OUT_DIR}/.ninja_log" \
    "${OUT_DIR}/.ninja_deps" 2>/dev/null || true

  tar \
    --format=posix \
    --pax-option='delete=atime,delete=ctime' \
    -C "${CHROMIUM_SRC}/out" \
    -I 'zstd -T0 -1' \
    -cf "${CHECKPOINT_ARCHIVE}" \
    Release_x86

  ls -lh "${CHECKPOINT_ARCHIVE}"
}
