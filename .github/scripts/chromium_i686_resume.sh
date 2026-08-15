#!/usr/bin/env bash
set -euo pipefail

# Helpers for carrying a live Ninja build graph between fresh GitHub-hosted
# runners. Source chromium_i686_common.sh before sourcing this file.

CHECKPOINT_REQUIRES_GN_REFRESH=false

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


CHECKPOINT_PROVENANCE_FAILURE_CLASS=""
CHECKPOINT_PROVENANCE_STATUS=""
CHECKPOINT_PRODUCER_SHA=""
CHECKPOINT_PRODUCER_STAGE=""

validate_checkpoint_source_run() {
  local run_id="${1:?checkpoint run id is required}"
  local expected_version="${2:?expected Chromium version is required}"
  local current_stage="${3:?current stage is required}"
  local artifact_name="${4:?checkpoint artifact name is required}"
  local expected_repo="${GITHUB_REPOSITORY:-}"
  local expected_ref="${GITHUB_REF_NAME:-}"
  CHECKPOINT_PROVENANCE_FAILURE_CLASS=deterministic_build
  CHECKPOINT_PROVENANCE_STATUS=invalid
  CHECKPOINT_PRODUCER_SHA=""
  CHECKPOINT_PRODUCER_STAGE=""
  if [ -z "${expected_repo}" ] || [ -z "${expected_ref}" ]; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::GitHub repository/ref metadata is unavailable for checkpoint provenance validation."
    return 1
  fi

  [[ "${run_id}" =~ ^[0-9]+$ ]] || {
    echo "::error::Checkpoint run id is not numeric: ${run_id}"
    return 1
  }
  [[ "${expected_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "::error::Checkpoint Chromium version is invalid: ${expected_version}"
    return 1
  }
  [[ "${current_stage}" =~ ^[1-9][0-9]*$ ]] || {
    echo "::error::Checkpoint consumer stage is invalid: ${current_stage}"
    return 1
  }
  if [[ ! "${artifact_name}" =~ ^chromium-i686-out-stage-([1-9][0-9]*)$ ]]; then
    echo "::error::Checkpoint artifact name is outside the stage contract: ${artifact_name}"
    return 1
  fi
  local producer_stage="${BASH_REMATCH[1]}"
  if [ "${producer_stage}" -ne "${current_stage}" ] \
      && [ "${producer_stage}" -ne "$((current_stage - 1))" ]; then
    echo "::error::Checkpoint artifact stage ${producer_stage} is incompatible with consumer stage ${current_stage}."
    return 1
  fi

  local run_json
  if ! run_json="$(bounded_gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}")"; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::Could not establish checkpoint run provenance for Actions run ${run_id}."
    return 1
  fi
  local workflow_path head_repo head_branch head_sha title
  if ! workflow_path="$(jq -er '.path | strings' <<<"${run_json}")" \
      || ! head_repo="$(jq -er '.head_repository.full_name | strings' <<<"${run_json}")" \
      || ! head_branch="$(jq -er '.head_branch | strings' <<<"${run_json}")" \
      || ! head_sha="$(jq -er '.head_sha | strings' <<<"${run_json}")" \
      || ! title="$(jq -er '.display_title | strings' <<<"${run_json}")"; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::Checkpoint run ${run_id} returned incomplete or malformed provenance metadata."
    return 1
  fi
  [[ "${head_sha}" =~ ^[0-9a-fA-F]{40}$ ]] || {
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::Checkpoint run ${run_id} returned an invalid head SHA: ${head_sha}"
    return 1
  }

  test "${workflow_path}" = ".github/workflows/chromium-i686.yml" || {
    echo "::error::Checkpoint run ${run_id} belongs to ${workflow_path}, not the Chromium i686 build workflow."
    return 1
  }
  test "${head_repo}" = "${expected_repo}" || {
    echo "::error::Checkpoint run ${run_id} originated from ${head_repo}, not ${expected_repo}."
    return 1
  }
  test "${head_branch}" = "${expected_ref}" || {
    echo "::error::Checkpoint run ${run_id} is from branch ${head_branch}, not ${expected_ref}."
    return 1
  }
  if [[ ! "${title}" =~ ^Chromium\ i686\ ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\ -\ stage\ ([1-9][0-9]*)\ -\ attempt\ ([0-9]+)$ ]]; then
    echo "::error::Checkpoint run title is outside the exact staged-build contract: ${title}"
    return 1
  fi
  test "${BASH_REMATCH[1]}" = "${expected_version}" || {
    echo "::error::Checkpoint run title does not match Chromium ${expected_version}: ${title}"
    return 1
  }
  test "${BASH_REMATCH[2]}" = "${producer_stage}" || {
    echo "::error::Checkpoint run title stage ${BASH_REMATCH[2]} does not match artifact stage ${producer_stage}."
    return 1
  }

  local artifacts_json total count expired
  if ! artifacts_json="$(bounded_gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/artifacts?per_page=100")"; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::Could not verify checkpoint artifact ownership for Actions run ${run_id}."
    return 1
  fi
  if ! total="$(jq -er '.total_count | numbers' <<<"${artifacts_json}")"; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::Checkpoint artifact listing for run ${run_id} returned malformed total_count metadata."
    return 1
  fi
  [[ "${total}" =~ ^[0-9]+$ ]] || {
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=infrastructure
    echo "::error::Checkpoint artifact total_count is not an integer: ${total}"
    return 1
  }
  if [ "${total}" -gt 100 ]; then
    echo "::error::Checkpoint run ${run_id} has ${total} artifacts; refusing truncated artifact provenance lookup."
    return 1
  fi
  count="$(jq -r --arg name "${artifact_name}" '[.artifacts[]? | select(.name == $name)] | length' <<<"${artifacts_json}")"
  expired="$(jq -r --arg name "${artifact_name}" '[.artifacts[]? | select(.name == $name and .expired == true)] | length' <<<"${artifacts_json}")"
  if [ "${count}" -eq 0 ]; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=""
    CHECKPOINT_PROVENANCE_STATUS=unavailable
    echo "::warning::Checkpoint artifact ${artifact_name} is no longer present on run ${run_id}; falling back without treating retention as a build defect."
    return 2
  fi
  if [ "${count}" -ne 1 ]; then
    echo "::error::Expected exactly one checkpoint artifact ${artifact_name} on run ${run_id}; found ${count}."
    return 1
  fi
  if [ "${expired}" -ne 0 ]; then
    CHECKPOINT_PROVENANCE_FAILURE_CLASS=""
    CHECKPOINT_PROVENANCE_STATUS=unavailable
    echo "::warning::Checkpoint artifact ${artifact_name} on run ${run_id} has expired; falling back without treating retention as a build defect."
    return 2
  fi
  CHECKPOINT_PROVENANCE_FAILURE_CLASS=""
  CHECKPOINT_PROVENANCE_STATUS=usable
  CHECKPOINT_PRODUCER_SHA="${head_sha,,}"
  CHECKPOINT_PRODUCER_STAGE="${producer_stage}"
  echo "Verified checkpoint provenance: run ${run_id}, branch ${head_branch}, head ${CHECKPOINT_PRODUCER_SHA}, stage ${producer_stage}, artifact ${artifact_name}."
}

checkpoint_bundle_is_usable() {
  local archive="${1:?checkpoint archive is required}"
  local expected_version="${2:?expected version is required}"
  local current_stage="${3:?current stage is required}"
  local expected_producer_sha="${4:-}"
  local expected_producer_stage="${5:-}"
  local expected_producer_run_id="${6:-}"
  local bundle_dir
  bundle_dir="$(dirname "${archive}")"
  local checksum="${bundle_dir}/$(basename "${CHECKPOINT_SHA256}")"
  local manifest="${bundle_dir}/$(basename "${CHECKPOINT_MANIFEST}")"

  if [ ! -s "${archive}" ]; then
    return 1
  fi

  echo "Validating checkpoint compression stream: ${archive}"
  if ! bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" zstd -q -t "${archive}"; then
    echo "::error::Checkpoint compression stream is corrupt: ${archive}"
    return 1
  fi

  # Structural safety is mandatory even for explicitly allowed legacy bundles.
  # A legacy migration opt-in relaxes metadata requirements only; it never permits
  # unsafe archive paths, links, special files, duplicates, or missing Ninja graph files.
  local archive_stats="${bundle_dir}/checkpoint-archive-stats.json"
  rm -f "${archive_stats}"
  if ! bounded_external "${CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS}" \
      python3 "${WORKSPACE}/scripts/validate_checkpoint_archive.py" \
        "${archive}" --stats-file "${archive_stats}"; then
    echo "::error::Checkpoint archive member/link/resource safety validation failed: ${archive}"
    return 1
  fi

  if [ ! -s "${manifest}" ] || [ ! -s "${checksum}" ]; then
    if [ "${ALLOW_LEGACY_CHECKPOINT_VERSION:-}" = "${expected_version}" ]; then
      echo "::warning::Legacy checkpoint accepted only because ALLOW_LEGACY_CHECKPOINT_VERSION explicitly permits Chromium ${expected_version}; structural safety passed and the next checkpoint will regenerate integrity metadata."
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
  EXPECTED_CHECKPOINT_CONTRACT="${CHECKPOINT_CONTRACT_VERSION}" \
  EXPECTED_PRODUCER_SHA="${expected_producer_sha}" \
  EXPECTED_PRODUCER_STAGE="${expected_producer_stage}" \
  EXPECTED_PRODUCER_RUN_ID="${expected_producer_run_id}" \
  CHECKPOINT_MANIFEST_PATH="${manifest}" python3 - <<'PY'
import json
import os
from pathlib import Path

manifest = json.loads(Path(os.environ["CHECKPOINT_MANIFEST_PATH"]).read_text())
if manifest.get("schema_version") != 1:
    raise SystemExit("Unsupported checkpoint manifest schema")
contract = int(manifest.get("checkpoint_contract_version", 1))
expected_contract = int(os.environ["EXPECTED_CHECKPOINT_CONTRACT"])
if contract != expected_contract:
    raise SystemExit(
        f"Checkpoint contract {contract} is incompatible with expected {expected_contract}"
    )
if manifest.get("chromium_version") != os.environ["EXPECTED_VERSION"]:
    raise SystemExit("Checkpoint Chromium version does not match requested version")
stage = int(manifest.get("checkpoint_stage", -1))
current = int(os.environ["CURRENT_STAGE"])
if stage not in {current, max(1, current - 1)}:
    raise SystemExit(f"Checkpoint stage {stage} is incompatible with current stage {current}")
expected_stage = os.environ.get("EXPECTED_PRODUCER_STAGE", "")
if expected_stage and stage != int(expected_stage):
    raise SystemExit(
        f"Checkpoint manifest stage {stage} does not match trusted producer stage {expected_stage}"
    )
if manifest.get("target_os") != "linux" or manifest.get("target_cpu") != "x86":
    raise SystemExit("Checkpoint target tuple is not linux/x86")
expected_sha = os.environ.get("EXPECTED_PRODUCER_SHA", "").lower()
if expected_sha:
    manifest_sha = str(manifest.get("workflow_sha", "")).lower()
    if manifest_sha != expected_sha:
        raise SystemExit(
            f"Checkpoint workflow SHA {manifest_sha or 'missing'} does not match trusted producer {expected_sha}"
        )
expected_run_id = os.environ.get("EXPECTED_PRODUCER_RUN_ID", "")
manifest_run_id = str(manifest.get("producer_run_id", ""))
if expected_run_id and manifest_run_id and manifest_run_id != expected_run_id:
    raise SystemExit(
        f"Checkpoint producer run {manifest_run_id} does not match trusted run {expected_run_id}"
    )
PY
  then
    echo "::error::Checkpoint manifest compatibility/provenance validation failed."
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

  local current_gn current_depot manifest_gn manifest_depot
  current_gn="$(chromium_gn_version)"
  current_depot="$(chromium_depot_tools_revision)"
  manifest_gn="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("gn_version", ""))' "${manifest}")"
  manifest_depot="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("depot_tools_revision", ""))' "${manifest}")"
  if [ -n "${manifest_gn}" ] && [ "${manifest_gn}" != "${current_gn}" ]; then
    echo "::error::Checkpoint GN pin differs from the Chromium source DEPS pin."
    return 1
  fi
  if [ -n "${manifest_depot}" ] && [ "${manifest_depot}" != "${current_depot}" ]; then
    echo "::error::Checkpoint depot_tools pin differs from the Chromium source DEPS pin."
    return 1
  fi
}

restore_out_checkpoint() {
  CHECKPOINT_REQUIRES_GN_REFRESH=false
  local archive="${1:-}"
  local expected_version="${2:-}"
  local current_stage="${3:-1}"
  local already_validated="${4:-false}"
  local out_parent="${CHROMIUM_SRC}/out"
  mkdir -p "${out_parent}"

  if [ -z "${archive}" ] || [ ! -s "${archive}" ]; then
    echo "No previous output checkpoint found; continuing with ccache and a fresh out directory."
    mkdir -p "${OUT_DIR}"
    return 0
  fi

  if [ "${already_validated}" != "true" ]; then
    checkpoint_bundle_is_usable "${archive}" "${expected_version}" "${current_stage}"
  fi

  # Refuse extraction if the validated archive would consume nearly all remaining
  # disk. The stats file was produced by the same streaming validator before this call.
  local bundle_dir archive_stats unpacked_bytes available_bytes reserve_bytes required_bytes required_gib
  bundle_dir="$(dirname "${archive}")"
  archive_stats="${bundle_dir}/checkpoint-archive-stats.json"
  if [ ! -s "${archive_stats}" ]; then
    echo "::error::Checkpoint archive stats are missing; refusing extraction without a validated size bound."
    return 1
  fi
  if ! unpacked_bytes="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("unpacked_bytes"); assert isinstance(v,int) and v >= 0; print(v)' "${archive_stats}" 2>/dev/null)"; then
    echo "::error::Checkpoint archive stats are malformed."
    return 1
  fi
  [[ "${CHROMIUM_I686_CHECKPOINT_RESTORE_RESERVE_GIB}" =~ ^[0-9]+$ ]] || {
    echo "::error::CHROMIUM_I686_CHECKPOINT_RESTORE_RESERVE_GIB must be a non-negative integer."
    return 1
  }
  reserve_bytes=$((CHROMIUM_I686_CHECKPOINT_RESTORE_RESERVE_GIB * 1024 * 1024 * 1024))
  required_bytes=$((unpacked_bytes + reserve_bytes))
  available_bytes="$(df -PB1 "${out_parent}" | awk 'NR == 2 {print $4}')"
  [[ "${available_bytes}" =~ ^[0-9]+$ ]] || {
    echo "::error::Could not determine free disk bytes for checkpoint restore."
    return 1
  }
  if [ "${available_bytes}" -lt "${required_bytes}" ]; then
    required_gib=$(( (required_bytes + 1024 * 1024 * 1024 - 1) / (1024 * 1024 * 1024) ))
    echo "::warning::Checkpoint restore needs about ${required_gib} GiB free including reserve; attempting expendable-cache cleanup."
    ensure_build_disk_space "${required_gib}" || true
    available_bytes="$(df -PB1 "${out_parent}" | awk 'NR == 2 {print $4}')"
    [[ "${available_bytes}" =~ ^[0-9]+$ ]] || {
      echo "::error::Could not determine free disk bytes after checkpoint cleanup."
      return 1
    }
    if [ "${available_bytes}" -lt "${required_bytes}" ]; then
      echo "::error::Insufficient disk space for bounded checkpoint restore: need ${required_bytes} bytes including reserve, have ${available_bytes}."
      return 1
    fi
  fi

  # Extract into an isolated sibling first. Nothing in the current output tree is
  # removed until the archive has fully extracted and its graph hashes are checked.
  local nonce restore_root staged_out backup_out
  nonce="${GITHUB_RUN_ID:-$$}-${RANDOM}"
  restore_root="${out_parent}/.checkpoint-restore-${nonce}"
  staged_out="${restore_root}/Release_x86"
  backup_out="${out_parent}/.Release_x86-before-restore-${nonce}"
  bounded_rm_rf "${restore_root}" || true
  bounded_rm_rf "${backup_out}" || true
  mkdir -p "${restore_root}"

  echo "Staging previous Ninja output checkpoint from ${archive}"
  if ! bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
      tar -I 'zstd -T0 -d' -xf "${archive}" -C "${restore_root}"; then
    echo "::error::Checkpoint extraction failed before the active output tree was touched."
    bounded_rm_rf "${restore_root}" || true
    return 1
  fi
  if [ ! -s "${staged_out}/build.ninja" ] || [ ! -s "${staged_out}/args.gn" ]; then
    echo "::error::Staged checkpoint lacks required Ninja graph files."
    bounded_rm_rf "${restore_root}" || true
    return 1
  fi

  local bundle_dir manifest
  bundle_dir="$(dirname "${archive}")"
  manifest="${bundle_dir}/$(basename "${CHECKPOINT_MANIFEST}")"
  if [ -s "${manifest}" ]; then
    local args_hash ninja_hash expected_args_hash expected_ninja_hash
    args_hash="$(sha256sum "${staged_out}/args.gn" | awk '{print $1}')"
    ninja_hash="$(sha256sum "${staged_out}/build.ninja" | awk '{print $1}')"
    expected_args_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["args_gn_sha256"])' "${manifest}")"
    expected_ninja_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["build_ninja_sha256"])' "${manifest}")"
    if [ "${args_hash}" != "${expected_args_hash}" ] || [ "${ninja_hash}" != "${expected_ninja_hash}" ]; then
      echo "::error::Extracted checkpoint Ninja metadata differs from its manifest; active output remains untouched."
      bounded_rm_rf "${restore_root}" || true
      return 1
    fi
    echo "Checkpoint manifest matches staged Ninja metadata."

    local manifest_gn manifest_depot
    manifest_gn="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("gn_version", ""))' "${manifest}")"
    manifest_depot="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("depot_tools_revision", ""))' "${manifest}")"
    if [ -z "${manifest_gn}" ] || [ -z "${manifest_depot}" ]; then
      CHECKPOINT_REQUIRES_GN_REFRESH=true
      echo "::warning::Restored checkpoint predates exact GN/depot_tools provenance; regenerating the Ninja graph once with Chromium's source-declared pinned GN before reuse."
    fi
  else
    CHECKPOINT_REQUIRES_GN_REFRESH=true
    echo "::warning::Restored legacy checkpoint has no tool-pin manifest; regenerating the Ninja graph once before reuse."
  fi

  # Promote only after extraction and metadata validation. If replacing an existing
  # tree, keep a same-filesystem rollback copy until the staged directory is in place.
  local had_previous=false
  if [ -e "${OUT_DIR}" ] || [ -L "${OUT_DIR}" ]; then
    if ! mv "${OUT_DIR}" "${backup_out}"; then
      echo "::error::Could not move the existing output tree aside for checkpoint promotion."
      bounded_rm_rf "${restore_root}" || true
      return 1
    fi
    had_previous=true
  fi
  if ! mv "${staged_out}" "${OUT_DIR}"; then
    echo "::error::Could not promote the staged checkpoint output tree."
    if [ "${had_previous}" = "true" ]; then
      if ! mv "${backup_out}" "${OUT_DIR}"; then
        echo "::error::Rollback also failed; preserved previous output remains at ${backup_out}."
      fi
    fi
    bounded_rm_rf "${restore_root}" || true
    return 1
  fi
  bounded_rm_rf "${restore_root}" || true
  if [ "${had_previous}" = "true" ]; then
    bounded_rm_rf "${backup_out}" \
      || echo "::warning::Checkpoint promotion succeeded but old output cleanup did not; disk guard will handle residue."
  fi

  echo "Checkpoint restored atomically after staged validation."
  du -sh "${OUT_DIR}" || true
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
  rm -f "${CHECKPOINT_ARCHIVE}" "${CHECKPOINT_SHA256}" "${CHECKPOINT_MANIFEST}" \
    "${CHECKPOINT_DIR}/checkpoint-archive-stats.json"

  echo "Creating nanosecond-preserving Ninja output checkpoint..."
  du -sh "${OUT_DIR}" || true
  stat -c '%y %s %n' \
    "${OUT_DIR}/build.ninja" \
    "${OUT_DIR}/.ninja_log" \
    "${OUT_DIR}/.ninja_deps" 2>/dev/null || true

  bounded_external "${CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS}" tar \
    --format=posix \
    --pax-option='delete=atime,delete=ctime' \
    -C "${CHROMIUM_SRC}/out" \
    -I 'zstd -T0 -1' \
    -cf "${CHECKPOINT_ARCHIVE}" \
    Release_x86

  # Validate producer output with the same streaming archive contract used by consumers.
  bounded_external "${CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS}" \
    python3 "${WORKSPACE}/scripts/validate_checkpoint_archive.py" \
      "${CHECKPOINT_ARCHIVE}" --stats-file "${CHECKPOINT_DIR}/checkpoint-archive-stats.json"
  bounded_external "${CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS}" zstd -q -t "${CHECKPOINT_ARCHIVE}"
  (
    cd "${CHECKPOINT_DIR}"
    sha256sum "$(basename "${CHECKPOINT_ARCHIVE}")" > "$(basename "${CHECKPOINT_SHA256}")"
    sha256sum -c "$(basename "${CHECKPOINT_SHA256}")"
  )

  local source_checksum_file="${WORKSPACE}/.chromium-source-cache/chromium-${version}.tar.xz.sha256"
  test -s "${source_checksum_file}"
  local source_sha clang_revision port_hash args_hash ninja_hash gn_version depot_revision
  source_sha="$(awk 'NR == 1 {print $1}' "${source_checksum_file}")"
  clang_revision="$(cat "${CHROMIUM_SRC}/third_party/llvm-build/Release+Asserts/cr_build_revision")"
  port_hash="$(compute_port_config_sha256 "${version}")"
  args_hash="$(sha256sum "${OUT_DIR}/args.gn" | awk '{print $1}')"
  ninja_hash="$(sha256sum "${OUT_DIR}/build.ninja" | awk '{print $1}')"
  gn_version="$(chromium_gn_version)"
  depot_revision="$(chromium_depot_tools_revision)"

  CHECKPOINT_VERSION="${version}" \
  CHECKPOINT_STAGE="${stage}" \
  CHECKPOINT_SOURCE_SHA="${source_sha}" \
  CHECKPOINT_CLANG="${clang_revision}" \
  CHECKPOINT_PORT_HASH="${port_hash}" \
  CHECKPOINT_ARGS_HASH="${args_hash}" \
  CHECKPOINT_NINJA_HASH="${ninja_hash}" \
  CHECKPOINT_GN_VERSION="${gn_version}" \
  CHECKPOINT_DEPOT_REVISION="${depot_revision}" \
  CHECKPOINT_CONTRACT_VERSION_VALUE="${CHECKPOINT_CONTRACT_VERSION}" \
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
    "gn_version": os.environ["CHECKPOINT_GN_VERSION"],
    "depot_tools_revision": os.environ["CHECKPOINT_DEPOT_REVISION"],
    "checkpoint_contract_version": int(os.environ["CHECKPOINT_CONTRACT_VERSION_VALUE"]),
    "workflow_sha": os.environ.get("GITHUB_SHA", "unknown"),
    "producer_run_id": os.environ.get("GITHUB_RUN_ID", "unknown"),
    "producer_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "unknown"),
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
