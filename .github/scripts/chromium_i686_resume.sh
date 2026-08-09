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
  verify_or_repair_i386_runtime_dependencies
}

checkpoint_bundle_is_usable() {
  local archive="${1:?checkpoint archive is required}"
  local expected_version="${2:?expected version is required}"
  local current_stage="${3:?current stage is required}"
  local bundle_dir
  bundle_dir="$(dirname "${archive}")"
  local checksum="${bundle_dir}/$(basename "${CHECKPOINT_SHA256}")"
  local manifest="${bundle_dir}/$(basename "${CHECKPOINT_MANIFEST}")"

  if [ ! -s "${archive}" ]; then
    return 1
  fi

  echo "Validating checkpoint compression stream: ${archive}"
  if ! zstd -q -t "${archive}"; then
    echo "::error::Checkpoint compression stream is corrupt: ${archive}"
    return 1
  fi

  if [ ! -s "${manifest}" ] || [ ! -s "${checksum}" ]; then
    if [ "${ALLOW_LEGACY_CHECKPOINT_VERSION:-}" = "${expected_version}" ]; then
      echo "::warning::Legacy checkpoint accepted only because ALLOW_LEGACY_CHECKPOINT_VERSION explicitly permits Chromium ${expected_version}; the next checkpoint will regenerate integrity metadata."
      return 0
    fi
    echo "::error::Legacy checkpoint lacks integrity metadata and no version-scoped migration opt-in is active."
    return 1
  fi

  if ! (
    cd "${bundle_dir}"
    sha256sum -c "$(basename "${checksum}")"
  ); then
    echo "::error::Checkpoint archive SHA-256 verification failed."
    return 1
  fi

  if ! EXPECTED_VERSION="${expected_version}" CURRENT_STAGE="${current_stage}" \
  CHECKPOINT_MANIFEST_PATH="${manifest}" python3 - <<'PY'
import json
import os
from pathlib import Path

manifest = json.loads(Path(os.environ["CHECKPOINT_MANIFEST_PATH"]).read_text())
if manifest.get("schema_version") != 1:
    raise SystemExit("Unsupported checkpoint manifest schema")
if manifest.get("chromium_version") != os.environ["EXPECTED_VERSION"]:
    raise SystemExit("Checkpoint Chromium version does not match requested version")
stage = int(manifest.get("checkpoint_stage", -1))
current = int(os.environ["CURRENT_STAGE"])
if stage not in {current, max(1, current - 1)}:
    raise SystemExit(f"Checkpoint stage {stage} is incompatible with current stage {current}")
if manifest.get("target_os") != "linux" or manifest.get("target_cpu") != "x86":
    raise SystemExit("Checkpoint target tuple is not linux/x86")
PY
  then
    echo "::error::Checkpoint manifest compatibility validation failed."
    return 1
  fi

  local source_checksum_file="${WORKSPACE}/.chromium-source-cache/chromium-${expected_version}.tar.xz.sha256"
  if [ -s "${source_checksum_file}" ]; then
    local current_source_sha manifest_source_sha
    current_source_sha="$(awk 'NR == 1 {print $1}' "${source_checksum_file}")"
    manifest_source_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_tar_sha256"])' "${manifest}")"
    if [ "${current_source_sha}" != "${manifest_source_sha}" ]; then
      echo "::error::Checkpoint source tarball checksum does not match the prepared Chromium source."
      return 1
    fi
  fi

  local current_clang manifest_clang
  current_clang="$(cat "${CHROMIUM_SRC}/third_party/llvm-build/Release+Asserts/cr_build_revision")"
  manifest_clang="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["clang_revision"])' "${manifest}")"
  if [ "${current_clang}" != "${manifest_clang}" ]; then
    echo "::error::Checkpoint clang revision does not match the prepared Chromium toolchain."
    return 1
  fi

  local current_port_hash manifest_port_hash
  current_port_hash="$(compute_port_config_sha256 "${expected_version}")"
  manifest_port_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port_config_sha256"])' "${manifest}")"
  if [ "${current_port_hash}" != "${manifest_port_hash}" ]; then
    echo "::error::Checkpoint port configuration differs from the current GN/patch configuration."
    return 1
  fi
}

restore_out_checkpoint() {
  local archive="${1:-}"
  local expected_version="${2:-}"
  local current_stage="${3:-1}"
  local already_validated="${4:-false}"
  mkdir -p "${CHROMIUM_SRC}/out"

  if [ -z "${archive}" ] || [ ! -s "${archive}" ]; then
    echo "No previous output checkpoint found; continuing with ccache and a fresh out directory."
    mkdir -p "${OUT_DIR}"
    return 0
  fi

  if [ "${already_validated}" != "true" ]; then
    checkpoint_bundle_is_usable "${archive}" "${expected_version}" "${current_stage}"
  fi
  echo "Restoring previous Ninja output checkpoint from ${archive}"
  rm -rf "${OUT_DIR}"
  tar -I 'zstd -T0 -d' -xf "${archive}" -C "${CHROMIUM_SRC}/out"
  du -sh "${OUT_DIR}" || true

  local bundle_dir manifest
  bundle_dir="$(dirname "${archive}")"
  manifest="${bundle_dir}/$(basename "${CHECKPOINT_MANIFEST}")"
  if [ -s "${manifest}" ]; then
    local args_hash ninja_hash expected_args_hash expected_ninja_hash
    args_hash="$(sha256sum "${OUT_DIR}/args.gn" | awk '{print $1}')"
    ninja_hash="$(sha256sum "${OUT_DIR}/build.ninja" | awk '{print $1}')"
    expected_args_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["args_gn_sha256"])' "${manifest}")"
    expected_ninja_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["build_ninja_sha256"])' "${manifest}")"
    test "${args_hash}" = "${expected_args_hash}"
    test "${ninja_hash}" = "${expected_ninja_hash}"
    echo "Checkpoint manifest matches extracted Ninja metadata."
  fi
}

# Override the common implementation after this file is sourced. GNU tar's
# usual default archive format stores mtimes at whole-second resolution. Ninja
# records higher-resolution timestamps in .ninja_log/.ninja_deps, so rounding
# restored outputs can make valid work look stale. POSIX/PAX archives retain
# subsecond mtimes.
create_out_checkpoint() {
  local version="${1:?Chromium version is required for checkpoint metadata}"
  local stage="${2:?Checkpoint stage is required}"

  if [ ! -d "${OUT_DIR}" ]; then
    echo "::error::Expected build output directory not found: ${OUT_DIR}"
    return 1
  fi
  test -s "${OUT_DIR}/build.ninja"
  test -s "${OUT_DIR}/args.gn"

  if ! ensure_build_disk_space 10; then
    return 1
  fi

  mkdir -p "${CHECKPOINT_DIR}"
  rm -f "${CHECKPOINT_ARCHIVE}" "${CHECKPOINT_SHA256}" "${CHECKPOINT_MANIFEST}"

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

  zstd -q -t "${CHECKPOINT_ARCHIVE}"
  (
    cd "${CHECKPOINT_DIR}"
    sha256sum "$(basename "${CHECKPOINT_ARCHIVE}")" > "$(basename "${CHECKPOINT_SHA256}")"
    sha256sum -c "$(basename "${CHECKPOINT_SHA256}")"
  )

  local source_checksum_file="${WORKSPACE}/.chromium-source-cache/chromium-${version}.tar.xz.sha256"
  test -s "${source_checksum_file}"
  local source_sha clang_revision port_hash args_hash ninja_hash
  source_sha="$(awk 'NR == 1 {print $1}' "${source_checksum_file}")"
  clang_revision="$(cat "${CHROMIUM_SRC}/third_party/llvm-build/Release+Asserts/cr_build_revision")"
  port_hash="$(compute_port_config_sha256 "${version}")"
  args_hash="$(sha256sum "${OUT_DIR}/args.gn" | awk '{print $1}')"
  ninja_hash="$(sha256sum "${OUT_DIR}/build.ninja" | awk '{print $1}')"

  CHECKPOINT_VERSION="${version}" \
  CHECKPOINT_STAGE="${stage}" \
  CHECKPOINT_SOURCE_SHA="${source_sha}" \
  CHECKPOINT_CLANG="${clang_revision}" \
  CHECKPOINT_PORT_HASH="${port_hash}" \
  CHECKPOINT_ARGS_HASH="${args_hash}" \
  CHECKPOINT_NINJA_HASH="${ninja_hash}" \
  CHECKPOINT_MANIFEST_PATH="${CHECKPOINT_MANIFEST}" \
  python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "schema_version": 1,
    "chromium_version": os.environ["CHECKPOINT_VERSION"],
    "checkpoint_stage": int(os.environ["CHECKPOINT_STAGE"]),
    "target_os": "linux",
    "target_cpu": "x86",
    "source_tar_sha256": os.environ["CHECKPOINT_SOURCE_SHA"],
    "clang_revision": os.environ["CHECKPOINT_CLANG"],
    "port_config_sha256": os.environ["CHECKPOINT_PORT_HASH"],
    "args_gn_sha256": os.environ["CHECKPOINT_ARGS_HASH"],
    "build_ninja_sha256": os.environ["CHECKPOINT_NINJA_HASH"],
    "workflow_sha": os.environ.get("GITHUB_SHA", "unknown"),
    "runner_os": os.environ.get("RUNNER_OS", "unknown"),
    "runner_image": os.environ.get("ImageOS", "unknown"),
    "runner_image_version": os.environ.get("ImageVersion", "unknown"),
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
}
Path(os.environ["CHECKPOINT_MANIFEST_PATH"]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n"
)
PY

  python3 -m json.tool "${CHECKPOINT_MANIFEST}" >/dev/null
  ls -lh "${CHECKPOINT_ARCHIVE}" "${CHECKPOINT_SHA256}" "${CHECKPOINT_MANIFEST}"
}
