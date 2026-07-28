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

  # Ninja can track directories as regeneration inputs. Touch them last so
  # extraction-created directory mtimes do not invalidate build.ninja.stamp.
  find "${CHROMIUM_SRC}" \
    -path "${CHROMIUM_SRC}/out" -prune -o \
    -type d -print0 \
    | xargs -0 -r -n 512 touch -d "@${epoch}" --

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

# Override the original one-off BUILD.gn edit with the maintained downstream
# patch layer. The source version is read from Chromium itself so existing
# composite-action callers do not need another input.
patch_build_gn_for_x86_linux() {
  local version_file="${CHROMIUM_SRC}/chrome/VERSION"
  if [ ! -s "${version_file}" ]; then
    echo "::error::Cannot determine Chromium version from ${version_file}."
    return 1
  fi

  local major minor build patch version
  major="$(awk -F= '$1 == "MAJOR" {print $2}' "${version_file}")"
  minor="$(awk -F= '$1 == "MINOR" {print $2}' "${version_file}")"
  build="$(awk -F= '$1 == "BUILD" {print $2}' "${version_file}")"
  patch="$(awk -F= '$1 == "PATCH" {print $2}' "${version_file}")"
  version="${major}.${minor}.${build}.${patch}"

  source "${GITHUB_WORKSPACE}/.github/scripts/chromium_i686_port.sh"
  apply_i686_port_patches "${version}"
}

verify_i386_runtime_dependencies() {
  local binary="${OUT_DIR}/v8_context_snapshot_generator"

  if [ ! -e "${binary}" ]; then
    echo "The i386 V8 snapshot generator is not present yet; runtime verification will occur in a later stage."
    return 0
  fi

  if [ ! -x "${binary}" ]; then
    echo "::error::The restored i386 snapshot generator is not executable."
    return 1
  fi

  echo "Checking runtime dependencies for ${binary}:"
  file "${binary}" || true

  local ldd_output
  local ldd_status
  set +e
  ldd_output="$(ldd "${binary}" 2>&1)"
  ldd_status=$?
  set -e
  printf '%s\n' "${ldd_output}"

  if [ "${ldd_status}" -ne 0 ] || grep -q 'not found' <<<"${ldd_output}"; then
    echo "::error::The restored i386 snapshot generator still has unresolved runtime libraries."
    return 1
  fi
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
