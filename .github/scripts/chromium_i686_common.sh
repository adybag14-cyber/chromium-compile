#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
CHROMIUM_SRC="${WORKSPACE}/chromium_source"
DEPOT_TOOLS="${WORKSPACE}/depot_tools"
GN_BINARY="${CHROMIUM_SRC}/third_party/gn/gn"
OUT_DIR="${CHROMIUM_SRC}/out/Release_x86"
CHECKPOINT_DIR="${WORKSPACE}/checkpoints"
CHECKPOINT_ARCHIVE="${CHECKPOINT_DIR}/out-Release_x86.tar.zst"
CHECKPOINT_SHA256="${CHECKPOINT_ARCHIVE}.sha256"
CHECKPOINT_MANIFEST="${CHECKPOINT_DIR}/checkpoint-manifest.json"
BUILD_LOG="${WORKSPACE}/build-stage.log"
export CCACHE_DIR="${CCACHE_DIR:-${WORKSPACE}/.ccache}"
export PATH="${DEPOT_TOOLS}:${DEPOT_TOOLS}/.cipd_bin:${PATH}"

CHROMIUM_I686_REMOVE_TIMEOUT_SECONDS="${CHROMIUM_I686_REMOVE_TIMEOUT_SECONDS:-300}"
CHROMIUM_I686_SYSTEM_CLEANUP_TIMEOUT_SECONDS="${CHROMIUM_I686_SYSTEM_CLEANUP_TIMEOUT_SECONDS:-180}"
CHROMIUM_I686_SWAP_TIMEOUT_SECONDS="${CHROMIUM_I686_SWAP_TIMEOUT_SECONDS:-180}"
CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS="${CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS:-1800}"
CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS="${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS:-1800}"
CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS="${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS:-1800}"
CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS="${CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS:-600}"
CHROMIUM_I686_GH_TIMEOUT_SECONDS="${CHROMIUM_I686_GH_TIMEOUT_SECONDS:-600}"
CHROMIUM_I686_LDD_TIMEOUT_SECONDS="${CHROMIUM_I686_LDD_TIMEOUT_SECONDS:-15}"
CHROMIUM_I686_RUNTIME_SMOKE_TIMEOUT_SECONDS="${CHROMIUM_I686_RUNTIME_SMOKE_TIMEOUT_SECONDS:-60}"
CHROMIUM_I686_MAX_RELEASE_ARTIFACT_GIB="${CHROMIUM_I686_MAX_RELEASE_ARTIFACT_GIB:-4}"
CHROMIUM_I686_MAX_CHECKPOINT_ARTIFACT_GIB="${CHROMIUM_I686_MAX_CHECKPOINT_ARTIFACT_GIB:-8}"
CHROMIUM_I686_MAX_SOURCE_ARCHIVE_GIB="${CHROMIUM_I686_MAX_SOURCE_ARCHIVE_GIB:-16}"
CHROMIUM_I686_MAX_RELEASE_UNPACKED_GIB="${CHROMIUM_I686_MAX_RELEASE_UNPACKED_GIB:-8}"
CHROMIUM_I686_MAX_RELEASE_MEMBERS="${CHROMIUM_I686_MAX_RELEASE_MEMBERS:-250000}"
CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB="${CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB:-2}"
CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB="${CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB:-80}"
CHROMIUM_I686_MAX_SOURCE_MEMBERS="${CHROMIUM_I686_MAX_SOURCE_MEMBERS:-2000000}"
CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB="${CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB:-10}"
CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_GIB="${CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_GIB:-40}"
CHROMIUM_I686_MAX_CHECKPOINT_MEMBERS="${CHROMIUM_I686_MAX_CHECKPOINT_MEMBERS:-2000000}"
CHROMIUM_I686_CHECKPOINT_RESTORE_RESERVE_GIB="${CHROMIUM_I686_CHECKPOINT_RESTORE_RESERVE_GIB:-5}"
CHECKPOINT_CONTRACT_VERSION="${CHECKPOINT_CONTRACT_VERSION:-1}"
export DEPOT_TOOLS_UPDATE=0
export CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_GIB CHROMIUM_I686_MAX_CHECKPOINT_MEMBERS
export CHROMIUM_I686_MAX_RELEASE_UNPACKED_GIB CHROMIUM_I686_MAX_RELEASE_MEMBERS
export CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB CHROMIUM_I686_MAX_SOURCE_MEMBERS CHROMIUM_I686_MAX_SOURCE_ARCHIVE_GIB


bounded_external() {
  local seconds="${1:?timeout seconds are required}"
  shift
  timeout -k 30s "${seconds}s" "$@"
}

# Hard ceiling for extraction/restore reserve knobs. Values are validated as
# short decimal text before arithmetic so malicious/accidental huge integers
# cannot overflow Bash's signed integer calculations.
CHROMIUM_I686_HARD_MAX_RESERVE_GIB=64
CHROMIUM_I686_HARD_MAX_CHECKPOINT_MINUTES=340
CHROMIUM_I686_HARD_MAX_SOURCE_UNPACKED_GIB=160
CHROMIUM_I686_HARD_MAX_SOURCE_MEMBERS=4000000
CHROMIUM_I686_HARD_MAX_SOURCE_ARCHIVE_GIB=32
CHROMIUM_I686_HARD_MAX_CHECKPOINT_UNPACKED_GIB=80
CHROMIUM_I686_HARD_MAX_CHECKPOINT_MEMBERS=4000000
CHROMIUM_I686_HARD_MAX_CHECKPOINT_ARTIFACT_GIB=16
CHROMIUM_I686_HARD_MAX_RELEASE_UNPACKED_GIB=16
CHROMIUM_I686_HARD_MAX_RELEASE_MEMBERS=1000000
CHROMIUM_I686_HARD_MAX_RELEASE_ARTIFACT_GIB=8
validate_bounded_positive_policy() {
  local value="${1:?policy value is required}"
  local name="${2:?policy variable name is required}"
  local hard_max="${3:?policy hard maximum is required}"
  if [[ ! "${value}" =~ ^[1-9][0-9]{0,9}$ ]] || [ "${value}" -gt "${hard_max}" ]; then
    echo "::error::${name} must be a positive integer no greater than ${hard_max}."
    return 1
  fi
}

validate_artifact_size_bytes() {
  local bytes="${1:?artifact byte size is required}"
  local gib_limit="${2:?artifact GiB limit is required}"
  local name="${3:?artifact policy name is required}"
  local hard_max_gib="${4:?artifact hard maximum is required}"
  [[ "${bytes}" =~ ^(0|[1-9][0-9]{0,14})$ ]] || {
    echo "::error::${name} byte size is missing/malformed: ${bytes}"
    return 1
  }
  validate_bounded_positive_policy "${gib_limit}" "${name}" "${hard_max_gib}" || return 2
  local max_bytes=$((gib_limit * 1024 * 1024 * 1024))
  if [ "${bytes}" -gt "${max_bytes}" ]; then
    echo "::error::Artifact size ${bytes} bytes exceeds ${name}=${gib_limit} GiB."
    return 2
  fi
}

validate_bounded_reserve_gib() {
  local value="${1:?reserve value is required}"
  local name="${2:?reserve variable name is required}"
  if [[ ! "${value}" =~ ^(0|[1-9][0-9]{0,2})$ ]] \
      || [ "${value}" -gt "${CHROMIUM_I686_HARD_MAX_RESERVE_GIB}" ]; then
    echo "::error::${name} must be a non-negative integer no greater than ${CHROMIUM_I686_HARD_MAX_RESERVE_GIB} GiB."
    return 1
  fi
}

validate_checkpoint_minutes() {
  local value="${1:-}"
  if [[ ! "${value}" =~ ^[1-9][0-9]{0,2}$ ]] \
      || [ "${value}" -gt "${CHROMIUM_I686_HARD_MAX_CHECKPOINT_MINUTES}" ]; then
    echo "::error::JOB_CHECKPOINT_MINUTES must be an integer from 1 through ${CHROMIUM_I686_HARD_MAX_CHECKPOINT_MINUTES}."
    return 1
  fi
}

validate_job_started_at() {
  local value="${1:-}"
  local now="${2:-$(date +%s)}"
  if [[ ! "${value}" =~ ^[1-9][0-9]{0,11}$ ]] || [[ ! "${now}" =~ ^[1-9][0-9]{0,11}$ ]]; then
    echo "::error::JOB_STARTED_AT/current epoch metadata is missing or malformed."
    return 1
  fi
  if [ "${value}" -gt "$((now + 300))" ]; then
    echo "::error::JOB_STARTED_AT is implausibly far in the future; refusing unsafe checkpoint arithmetic."
    return 1
  fi
}

bounded_gh() {
  bounded_external "${CHROMIUM_I686_GH_TIMEOUT_SECONDS}" gh "$@"
}

bounded_rm_rf() {
  timeout -k 15s "${CHROMIUM_I686_REMOVE_TIMEOUT_SECONDS}s" rm -rf -- "$@"
}

bounded_sudo_rm_rf() {
  timeout -k 15s "${CHROMIUM_I686_SYSTEM_CLEANUP_TIMEOUT_SECONDS}s" sudo rm -rf -- "$@"
}

maximize_runner_disk_space() {
  echo "=== Disk space BEFORE cleanup ==="
  df -h
  bounded_sudo_rm_rf /usr/share/dotnet || echo "::warning::Timed out removing /usr/share/dotnet; later disk guards will decide whether the runner is usable."
  bounded_sudo_rm_rf /usr/local/lib/android || echo "::warning::Timed out removing /usr/local/lib/android; later disk guards will decide whether the runner is usable."
  bounded_sudo_rm_rf /opt/ghc || echo "::warning::Timed out removing /opt/ghc; later disk guards will decide whether the runner is usable."
  bounded_sudo_rm_rf /opt/hostedtoolcache/CodeQL || echo "::warning::Timed out removing CodeQL cache; later disk guards will decide whether the runner is usable."
  bounded_sudo_apt_get purge -y '^mysql-' '^mongodb-' '^postgresql-' '^dotnet-' '^android-sdk-' || true
  bounded_sudo_apt_get autoremove -y || true
  timeout -k 10s 60s sudo apt-get clean || true
  ensure_swap
  echo "=== Disk space AFTER cleanup ==="
  df -h
}

ensure_swap() {
  if swapon --show | grep -q '/swapfile'; then
    echo "Swap already enabled"
    swapon --show
    return 0
  fi

  echo "Attempting an 8G best-effort swap file to reduce OOM risk during Chromium linking..."
  sudo rm -f /swapfile || true
  if ! timeout -k 15s "${CHROMIUM_I686_SWAP_TIMEOUT_SECONDS}s" sudo fallocate -l 8G /swapfile       && ! timeout -k 15s "${CHROMIUM_I686_SWAP_TIMEOUT_SECONDS}s"         sudo dd if=/dev/zero of=/swapfile bs=1M count=8192 status=none; then
    echo "::warning::Could not allocate swap on this runner; continuing with physical memory and normal OOM classification."
    sudo rm -f /swapfile || true
    return 0
  fi
  if ! sudo chmod 600 /swapfile       || ! timeout -k 10s 60s sudo mkswap /swapfile >/dev/null       || ! timeout -k 10s 60s sudo swapon /swapfile; then
    echo "::warning::Runner does not permit swap activation; continuing without swap."
    sudo swapoff /swapfile 2>/dev/null || true
    sudo rm -f /swapfile || true
    return 0
  fi
  swapon --show
}

NATIVE_BUILD_PACKAGES=(
  git python3 python3-pip curl jq xz-utils zstd zip unzip file binutils
  build-essential pkg-config ninja-build ccache
  libgtk-3-dev libnss3-dev libasound2-dev libxss-dev libxtst-dev libxrandr-dev
  libxcomposite-dev libxdamage-dev libxfixes-dev libxrender-dev libxkbcommon-dev
  libdrm-dev libgbm-dev libpango1.0-dev libcups2-dev libatk1.0-dev
  libatspi2.0-dev libatk-bridge2.0-dev
)
SYSTEM_DEPENDENCY_FAILURE_CLASS=""

install_system_dependencies() {
  SYSTEM_DEPENDENCY_FAILURE_CLASS=""
  if ! bounded_sudo_apt_get update; then
    SYSTEM_DEPENDENCY_FAILURE_CLASS=infrastructure
    echo "::error::Native build package index refresh failed or timed out; a later runner/mirror may recover."
    return 1
  fi

  # Simulation separates release/package-set drift from a transient mutation failure.
  # If APT cannot solve this exact set after a successful index refresh, recycling the
  # same runner image is not useful; the LTS contract/package list needs maintenance.
  if ! bounded_apt_get_simulate install -y "${NATIVE_BUILD_PACKAGES[@]}"; then
    SYSTEM_DEPENDENCY_FAILURE_CLASS=deterministic_build
    echo "::error::Native Chromium build prerequisites are not solvable on this runner release; update the LTS package contract."
    return 1
  fi

  if ! bounded_sudo_apt_get install -y "${NATIVE_BUILD_PACKAGES[@]}"; then
    SYSTEM_DEPENDENCY_FAILURE_CLASS=infrastructure
    echo "::error::Native build prerequisite installation failed after a successful simulation; treating this as runner/mirror infrastructure."
    return 1
  fi
}

I386_BASELINE_SONAMES=(
  libc.so.6
  libgcc_s.so.1
  libstdc++.so.6
  libglib-2.0.so.0
  libgobject-2.0.so.0
  libgio-2.0.so.0
  libgmodule-2.0.so.0
  libexpat.so.1
  libnspr4.so
  libnss3.so
  libnssutil3.so
  libsmime3.so
  libdbus-1.so.3
  libX11.so.6
  libXext.so.6
  libgbm.so.1
  libxcb.so.1
  libxkbcommon.so.0
  libudev.so.1
  libasound.so.2
)

declare -A I386_SONAME_PACKAGES=(
  [libc.so.6]=libc6:i386
  [libdl.so.2]=libc6:i386
  [libpthread.so.0]=libc6:i386
  [libm.so.6]=libc6:i386
  [libgcc_s.so.1]=libgcc-s1:i386
  [libstdc++.so.6]=libstdc++6:i386
  [libglib-2.0.so.0]=libglib2.0-0:i386
  [libgobject-2.0.so.0]=libglib2.0-0:i386
  [libgio-2.0.so.0]=libglib2.0-0:i386
  [libgmodule-2.0.so.0]=libglib2.0-0:i386
  [libexpat.so.1]=libexpat1:i386
  [libnspr4.so]=libnspr4:i386
  [libnss3.so]=libnss3:i386
  [libnssutil3.so]=libnss3:i386
  [libsmime3.so]=libnss3:i386
  [libdbus-1.so.3]=libdbus-1-3:i386
  [libX11.so.6]=libx11-6:i386
  [libXext.so.6]=libxext6:i386
  [libgbm.so.1]=libgbm1:i386
  [libxcb.so.1]=libxcb1:i386
  [libxkbcommon.so.0]=libxkbcommon0:i386
  [libudev.so.1]=libudev1:i386
  [libasound.so.2]=libasound2:i386
  # Qt is intentionally lazy: shared target shims are not host executables.
  # These remain preferred mappings if a future generated host tool genuinely needs Qt5.
  [libQt5Core.so.5]=libqt5core5a:i386
  [libQt5Gui.so.5]=libqt5gui5:i386
  [libQt5Widgets.so.5]=libqt5widgets5:i386
)

I386_RUNTIME_REPAIR_FAILURE_CLASS=""
I386_RUNTIME_REPAIR_CHANGED=false
I386_RESOLVED_PACKAGE=""
CHROMIUM_PACKAGE_FAILURE_CLASS=""
RELEASE_ARCHIVE_FAILURE_CLASS=""
RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=""
RUNNER_DISTRO_ID=""
RUNNER_DISTRO_VERSION_ID=""
I386_MULTIARCH="i386-linux-gnu"
CHROMIUM_I686_APT_TIMEOUT_SECONDS="${CHROMIUM_I686_APT_TIMEOUT_SECONDS:-900}"
CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS="${CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS:-180}"
CHROMIUM_I686_APT_FILE_SEARCH_TIMEOUT_SECONDS="${CHROMIUM_I686_APT_FILE_SEARCH_TIMEOUT_SECONDS:-20}"

bounded_sudo_apt_get() {
  timeout -k 30s "${CHROMIUM_I686_APT_TIMEOUT_SECONDS}s" \
    sudo env DEBIAN_FRONTEND=noninteractive apt-get \
      -o Acquire::Retries=3 \
      -o Acquire::http::Timeout=30 \
      -o Acquire::https::Timeout=30 \
      -o DPkg::Lock::Timeout=60 \
      "$@"
}

bounded_apt_get_simulate() {
  timeout -k 15s "${CHROMIUM_I686_APT_TIMEOUT_SECONDS}s" \
    apt-get -s \
      -o Acquire::Retries=3 \
      -o Acquire::http::Timeout=30 \
      -o Acquire::https::Timeout=30 \
      "$@"
}

detect_runner_platform() {
  local os_release_file="${CHROMIUM_I686_OS_RELEASE_FILE:-/etc/os-release}"
  if [ ! -r "${os_release_file}" ]; then
    echo "::error::Cannot identify Linux runner: ${os_release_file} is unavailable."
    return 1
  fi

  local platform_line
  # os-release deliberately uses generic names such as VERSION and ID. Source it
  # only in a subshell so platform probing can never overwrite caller/workflow state.
  if ! platform_line="$(
    (
      set +u
      # shellcheck disable=SC1090
      source "${os_release_file}"
      printf '%s\t%s\n' "${ID:-unknown}" "${VERSION_ID:-unknown}"
    )
  )"; then
    echo "::error::Failed to parse runner platform metadata from ${os_release_file}."
    return 1
  fi
  IFS=$'\t' read -r RUNNER_DISTRO_ID RUNNER_DISTRO_VERSION_ID <<<"${platform_line}"
  RUNNER_DISTRO_ID="${RUNNER_DISTRO_ID:-unknown}"
  RUNNER_DISTRO_VERSION_ID="${RUNNER_DISTRO_VERSION_ID:-unknown}"

  I386_MULTIARCH="$(dpkg-architecture -ai386 -qDEB_HOST_MULTIARCH 2>/dev/null || true)"
  I386_MULTIARCH="${I386_MULTIARCH:-i386-linux-gnu}"
  echo "Runner platform: ${RUNNER_DISTRO_ID} ${RUNNER_DISTRO_VERSION_ID}; i386 multiarch tuple: ${I386_MULTIARCH}"
  if [ "${RUNNER_DISTRO_ID}" != "ubuntu" ]; then
    echo "::warning::The i686 pipeline is continuously validated on Ubuntu LTS runners; ${RUNNER_DISTRO_ID} is best-effort only."
  fi
}

i386_package_has_candidate() {
  local package="${1:?package is required}"
  apt-cache policy "${package}" 2>/dev/null \
    | awk '/Candidate:/ && $2 != "(none)" {found=1} END {exit !found}'
}

i386_package_variants() {
  local package="${1:?package is required}"
  local base="${package%:i386}"
  printf '%s\n' "${package}"
  if [[ "${base}" != *t64 ]]; then
    printf '%s\n' "${base}t64:i386"
  fi
}

verify_i386_runner_capability() {
  detect_runner_platform
  sudo dpkg --add-architecture i386
  if ! bounded_sudo_apt_get update; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=infrastructure
    echo "::error::Failed to refresh package indexes within the bounded APT timeout."
    return 1
  fi
  if ! i386_package_has_candidate libc6:i386; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
    echo "::error::Runner ${RUNNER_DISTRO_ID} ${RUNNER_DISTRO_VERSION_ID} does not expose an installable libc6:i386 candidate."
    return 1
  fi
}

i386_soname_is_baseline() {
  local needle="${1:?SONAME is required}" soname
  for soname in "${I386_BASELINE_SONAMES[@]}"; do
    if [ "${soname}" = "${needle}" ]; then
      return 0
    fi
  done
  return 1
}

i386_soname_is_available() {
  local soname="${1:?SONAME is required}" dir
  for dir in \
    "/lib/${I386_MULTIARCH}" \
    "/usr/lib/${I386_MULTIARCH}" \
    /lib32 \
    /usr/lib32; do
    if [ -e "${dir}/${soname}" ]; then
      return 0
    fi
  done
  return 1
}

guess_i386_packages_for_soname() {
  local soname="${1:?SONAME is required}"
  local stem lower major=""
  stem="${soname%%.so*}"
  lower="${stem,,}"
  if [[ "${soname}" =~ \.so\.([0-9]+) ]]; then
    major="${BASH_REMATCH[1]}"
  fi

  if [ -n "${major}" ]; then
    printf '%s\n' "${lower}${major}:i386" "${lower}-${major}:i386"
  else
    printf '%s\n' "${lower}:i386"
  fi
}

ensure_apt_file_i386_metadata() {
  local marker="${RUNNER_TEMP:-/tmp}/chromium-i686-apt-file-i386-ready"
  if [ -s "${marker}" ]; then
    return 0
  fi

  echo "Preparing bounded apt-file metadata fallback for automatic i386 SONAME resolution."
  if ! command -v apt-file >/dev/null 2>&1; then
    if ! bounded_sudo_apt_get install -y --no-install-recommends apt-file; then
      I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
      echo "::error::apt-file fallback tooling is unavailable on this runner; add a SONAME mapping or update the resolver."
      return 1
    fi
  fi
  if ! timeout -k 20s "${CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS}s" \
      sudo apt-file -o APT::Architecture=i386 -o APT::Architectures::=i386 update; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
    echo "::error::apt-file metadata fallback exceeded ${CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS}s or failed; refusing to burn a fresh runner retry."
    return 1
  fi
  printf 'ready\n' > "${marker}"
}

apt_file_search_i386() {
  local path="${1:?path is required}"
  timeout -k 5s "${CHROMIUM_I686_APT_FILE_SEARCH_TIMEOUT_SECONDS}s" \
    apt-file --filter-origins Ubuntu -a i386 -l -F search "${path}" 2>/dev/null
}

classify_apt_file_search_status() {
  local status="${1:?status is required}"
  case "${status}" in
    0) return 0 ;;
    1) return 1 ;; # Valid search with no matching package.
    2)
      I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
      echo "::error::apt-file rejected the bounded search invocation; resolver syntax/tooling requires maintenance." >&2
      return 2
      ;;
    *)
      I386_RUNTIME_REPAIR_FAILURE_CLASS=infrastructure
      echo "::error::apt-file search failed or timed out with status ${status}; a later runner may recover." >&2
      return 3
      ;;
  esac
}

resolve_i386_package_for_soname() {
  local soname="${1:?SONAME is required}"
  local preferred="${I386_SONAME_PACKAGES[${soname}]:-}"
  I386_RESOLVED_PACKAGE=""

  local candidate variant
  local -a candidates=() guessed=() variants=()
  if [ -n "${preferred}" ]; then
    mapfile -t variants < <(i386_package_variants "${preferred}" | sort -u)
    for variant in "${variants[@]}"; do
      if dpkg-query -W -f='${db:Status-Abbrev}' "${variant}" 2>/dev/null | grep -qx 'ii ' \
          || i386_package_has_candidate "${variant}"; then
        I386_RESOLVED_PACKAGE="${variant}"
        if [ "${variant}" = "${preferred}" ]; then
          echo "Known i386 runtime mapping: ${soname} -> ${I386_RESOLVED_PACKAGE}"
        else
          echo "Release-local i386 runtime mapping: ${soname} -> ${I386_RESOLVED_PACKAGE} (preferred ${preferred})"
        fi
        return 0
      fi
    done
    echo "::warning::Preferred mapping ${soname} -> ${preferred} and its release-local variants are unavailable on ${RUNNER_DISTRO_ID:-this runner}; trying SONAME-derived discovery."
  fi

  mapfile -t guessed < <(guess_i386_packages_for_soname "${soname}" | sort -u)
  for candidate in "${guessed[@]}"; do
    mapfile -t variants < <(i386_package_variants "${candidate}" | sort -u)
    for variant in "${variants[@]}"; do
      if i386_package_has_candidate "${variant}"; then
        candidates+=("${variant}")
      fi
    done
  done
  mapfile -t candidates < <(printf '%s\n' "${candidates[@]}" | sed '/^$/d' | sort -u)
  if [ "${#candidates[@]}" -eq 1 ]; then
    I386_RESOLVED_PACKAGE="${candidates[0]}"
    echo "Derived i386 runtime mapping: ${soname} -> ${I386_RESOLVED_PACKAGE}"
    return 0
  fi

  if ! ensure_apt_file_i386_metadata; then
    return 1
  fi

  local path search_output
  candidates=()
  for path in \
    "usr/lib/${I386_MULTIARCH}/${soname}" \
    "lib/${I386_MULTIARCH}/${soname}" \
    "usr/lib32/${soname}" \
    "lib32/${soname}"; do
    local search_status=0 classified_status=0
    if search_output="$(apt_file_search_i386 "${path}")"; then
      search_status=0
    else
      search_status=$?
    fi
    if [ "${search_status}" -ne 0 ]; then
      if classify_apt_file_search_status "${search_status}"; then
        classified_status=0
      else
        classified_status=$?
      fi
      if [ "${classified_status}" -eq 1 ]; then
        continue
      fi
      return 1
    fi
    while IFS= read -r candidate; do
      [ -n "${candidate}" ] || continue
      candidate="${candidate%:i386}"
      if i386_package_has_candidate "${candidate}:i386"; then
        candidates+=("${candidate}:i386")
      fi
    done <<<"${search_output}"
  done

  mapfile -t candidates < <(printf '%s\n' "${candidates[@]}" | sed '/^$/d' | sort -u)
  if [ "${#candidates[@]}" -eq 1 ]; then
    I386_RESOLVED_PACKAGE="${candidates[0]}"
    echo "Discovered i386 runtime mapping: ${soname} -> ${I386_RESOLVED_PACKAGE}"
    return 0
  fi

  I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
  if [ "${#candidates[@]}" -eq 0 ]; then
    echo "::error::No installable Ubuntu i386 package provides ${soname}; a fresh runner retry will not help."
  else
    echo "::error::Multiple Ubuntu i386 packages provide ${soname}; refusing to choose one automatically: ${candidates[*]}"
  fi
  return 1
}

install_i386_runtime_libraries() {
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  verify_i386_runner_capability || return 1

  local soname package
  local -a packages=()
  for soname in "${I386_BASELINE_SONAMES[@]}"; do
    if ! resolve_i386_package_for_soname "${soname}"; then
      echo "::error::Could not resolve baseline i386 SONAME ${soname} on ${RUNNER_DISTRO_ID} ${RUNNER_DISTRO_VERSION_ID}."
      return 1
    fi
    packages+=("${I386_RESOLVED_PACKAGE}")
  done
  mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort -u)

  echo "Resolved baseline i386 runtime packages for ${RUNNER_DISTRO_ID} ${RUNNER_DISTRO_VERSION_ID}: ${packages[*]}"
  if ! bounded_apt_get_simulate install -y --no-install-recommends "${packages[@]}" >/dev/null 2>&1; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
    echo "::error::Resolved baseline i386 packages cannot be installed together on this runner image."
    return 1
  fi
  if ! bounded_sudo_apt_get install -y --no-install-recommends "${packages[@]}"; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=infrastructure
    return 1
  fi
  if ! verify_i386_host_runtime; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
    echo "::error::Installed i386 package set did not satisfy the baseline SONAME contract on this runner image."
    return 1
  fi
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
}

verify_i386_host_runtime() {
  local soname
  local missing=0

  if [ ! -x /lib/ld-linux.so.2 ]; then
    echo "::error::The i386 dynamic loader /lib/ld-linux.so.2 is unavailable."
    missing=1
  fi

  for soname in "${I386_BASELINE_SONAMES[@]}"; do
    if ! i386_soname_is_available "${soname}"; then
      echo "::error::Required baseline i386 runtime SONAME is not installed: ${soname}"
      missing=1
    fi
  done

  if [ "${missing}" -ne 0 ]; then
    return 1
  fi
  echo "Verified baseline i386 loader and SONAMEs on ${RUNNER_DISTRO_ID} ${RUNNER_DISTRO_VERSION_ID}."
}

is_i386_host_executable() {
  local binary="${1:?binary is required}"
  local file_output="${2:-}"
  if [ -z "${file_output}" ]; then
    file_output="$(file -b "${binary}" 2>/dev/null || true)"
  fi
  # Shared libraries are target artifacts, not host build tools. Only actual ELF32
  # executables/PIE executables need to run against the GitHub runner's i386 runtime.
  grep -Eq 'ELF 32-bit.*(pie )?executable, Intel (80386|i386)' <<<"${file_output}"
}

bounded_ldd() {
  timeout -k 3s "${CHROMIUM_I686_LDD_TIMEOUT_SECONDS}s" ldd "$@"
}

capture_ldd_output() {
  local binary="${1:?binary is required}"
  local output_name="${2:?output variable name is required}"
  local output status=0
  if output="$(bounded_ldd "${binary}" 2>&1)"; then
    printf -v "${output_name}" '%s' "${output}"
    return 0
  else
    status=$?
  fi
  printf -v "${output_name}" '%s' "${output}"
  I386_RUNTIME_REPAIR_FAILURE_CLASS="$(classify_prepare_command_status "${status}" deterministic_build)"
  echo "::error::ldd failed or timed out for ELF32 runtime object ${binary} (status ${status}); refusing an unbounded runtime probe."
  return 1
}

repair_missing_i386_runtime_for_binary() {
  local binary="${1:?binary is required}"
  local ldd_output soname package round current_missing previous_missing=""
  local -a missing_sonames=() packages=() to_install=()
  I386_RUNTIME_REPAIR_FAILURE_CLASS=runtime_environment
  I386_RUNTIME_REPAIR_CHANGED=false

  for round in 1 2 3; do
    if ! capture_ldd_output "${binary}" ldd_output; then
      printf '%s\n' "${ldd_output}"
      return 1
    fi
    printf '%s\n' "${ldd_output}"
    mapfile -t missing_sonames < <(awk '/=> not found/ {print $1}' <<<"${ldd_output}" | sort -u)
    if [ "${#missing_sonames[@]}" -eq 0 ]; then
      I386_RUNTIME_REPAIR_FAILURE_CLASS=""
      return 0
    fi

    current_missing="$(printf '%s\n' "${missing_sonames[@]}")"
    if [ -n "${previous_missing}" ] && [ "${current_missing}" = "${previous_missing}" ]; then
      I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
      echo "::error::Repair round ${round}: unresolved SONAME set did not change; stopping instead of repeating identical package installs."
      return 1
    fi
    previous_missing="${current_missing}"

    packages=()
    for soname in "${missing_sonames[@]}"; do
      if ! resolve_i386_package_for_soname "${soname}"; then
        return 1
      fi
      packages+=("${I386_RESOLVED_PACKAGE}")
    done
    mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort -u)

    to_install=()
    for package in "${packages[@]}"; do
      if ! dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2>/dev/null | grep -qx 'ii '; then
        to_install+=("${package}")
      fi
    done
    if [ "${#to_install[@]}" -eq 0 ]; then
      I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
      echo "::error::Resolved provider packages are already installed but the required SONAMEs are still missing; a fresh runner retry will not help."
      return 1
    fi

    echo "Repair round ${round}: validating i386 runtime dependencies: ${to_install[*]}"
    if ! bounded_apt_get_simulate install -y --no-install-recommends "${to_install[@]}" >/dev/null 2>&1; then
      I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
      echo "::error::Resolved i386 provider packages cannot be installed together on this runner image; a fresh runner retry will not help."
      return 1
    fi
    echo "Repair round ${round}: installing i386 runtime dependencies: ${to_install[*]}"
    if ! bounded_sudo_apt_get install -y --no-install-recommends "${to_install[@]}"; then
      I386_RUNTIME_REPAIR_FAILURE_CLASS=infrastructure
      return 1
    fi
    I386_RUNTIME_REPAIR_CHANGED=true
  done

  if ! capture_ldd_output "${binary}" ldd_output; then
    printf '%s\n' "${ldd_output}"
    return 1
  fi
  printf '%s\n' "${ldd_output}"
  if grep -q '=> not found' <<<"${ldd_output}"; then
    I386_RUNTIME_REPAIR_FAILURE_CLASS=deterministic_build
    echo "::error::The ELF32 runtime is still unresolved after three package-repair rounds; a fresh runner retry will not help."
    return 1
  fi
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
}

# Returns 0 when at least one host package was installed, 1 when repair failed,
# and 2 when the log named no repairable ELF32 tool or no host change was needed.
repair_i386_runtime_from_build_log() {
  local log_file="${1:?build log is required}"
  local reported path file_output
  local repaired=0
  I386_RUNTIME_REPAIR_FAILURE_CLASS=runtime_environment
  I386_RUNTIME_REPAIR_CHANGED=false

  while IFS= read -r reported; do
    [ -n "${reported}" ] || continue
    case "${reported}" in
      /*) path="${reported}" ;;
      out/*) path="${CHROMIUM_SRC}/${reported}" ;;
      ./*) path="${OUT_DIR}/${reported#./}" ;;
      *) path="${OUT_DIR}/${reported}" ;;
    esac
    if [ ! -x "${path}" ]; then
      continue
    fi
    file_output="$(file "${path}" 2>/dev/null || true)"
    if ! is_i386_host_executable "${path}" "${file_output}"; then
      continue
    fi
    echo "Repairing runtime for failed ELF32 build tool reported by Ninja: ${path}"
    repair_missing_i386_runtime_for_binary "${path}" </dev/null || return 1
    if [ "${I386_RUNTIME_REPAIR_CHANGED}" = "true" ]; then
      repaired=1
    fi
  done < <(awk -F': error while loading shared libraries:' 'NF > 1 {print $1}' "${log_file}" | sort -u)

  if [ "${repaired}" -eq 1 ]; then
    return 0
  fi
  return 2
}

verify_or_repair_i386_runtime_dependencies() {
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
  I386_RUNTIME_REPAIR_CHANGED=false
  local changed_any=false
  if [ ! -d "${OUT_DIR}" ]; then
    return 0
  fi

  local -a candidates=()
  local binary file_output
  while IFS= read -r -d '' binary; do
    file_output="$(file "${binary}" 2>/dev/null || true)"
    if is_i386_host_executable "${binary}" "${file_output}"; then
      candidates+=("${binary}")
    fi
  done < <(find "${OUT_DIR}" -maxdepth 2 -type f -perm -111 -print0)

  if [ "${#candidates[@]}" -eq 0 ]; then
    echo "No generated ELF32 host build executables are present yet."
    return 0
  fi

  echo "Checking ${#candidates[@]} generated ELF32 host build executable(s); shared target objects are intentionally excluded."
  for binary in "${candidates[@]}"; do
    echo "Runtime check: ${binary}"
    repair_missing_i386_runtime_for_binary "${binary}" </dev/null || return 1
    if [ "${I386_RUNTIME_REPAIR_CHANGED}" = "true" ]; then
      changed_any=true
    fi
  done
  I386_RUNTIME_REPAIR_CHANGED="${changed_any}"
  I386_RUNTIME_REPAIR_FAILURE_CLASS=""
}

chromium_i686_gn_args() {
  cat <<'EOF'
target_os="linux"
target_cpu="x86"
is_debug=false
symbol_level=0
blink_symbol_level=0
enable_nacl=false
is_official_build=false
use_thin_lto=false
use_reclient=false
generate_location_tags=false
treat_warnings_as_errors=false
cc_wrapper="ccache"
EOF
}

compute_port_config_sha256() {
  local version="${1:?version is required}"
  local major="${version%%.*}"
  {
    chromium_i686_gn_args
    local file relative hash
    while IFS= read -r file; do
      relative="${file#${GITHUB_WORKSPACE}/}"
      hash="$(sha256sum "${file}" | awk '{print $1}')"
      printf '%s  %s\n' "${hash}" "${relative}"
    done < <(
      {
        find "${GITHUB_WORKSPACE}/patches/common" -maxdepth 1 -type f -print 2>/dev/null || true
        find "${GITHUB_WORKSPACE}/patches/versions/${major}" -maxdepth 1 -type f -print 2>/dev/null || true
        printf '%s\n' "${GITHUB_WORKSPACE}/.github/scripts/chromium_i686_port.sh"
      } | sort -u
    )
  } | sha256sum | awk '{print $1}'
}

available_disk_gb() {
  local path="${1:-${WORKSPACE}}"
  df -PB1G "${path}" | awk 'NR == 2 {print $4}'
}

cleanup_stale_checkpoint_residue() {
  local out_parent="${CHROMIUM_SRC}/out"
  [ -d "${out_parent}" ] || return 0

  local active_output=false
  if [ -e "${OUT_DIR}" ] || [ -L "${OUT_DIR}" ]; then
    active_output=true
  fi

  local -a residues=()
  shopt -s nullglob
  residues+=("${out_parent}"/.checkpoint-restore-*)
  residues+=("${out_parent}"/.Release_x86-before-restore-*)
  shopt -u nullglob
  [ "${#residues[@]}" -gt 0 ] || return 0

  if [ "${active_output}" != true ]; then
    echo "::warning::Checkpoint restore residue exists while the active output tree is missing; preserving rollback state instead of deleting it."
    return 0
  fi

  local residue
  for residue in "${residues[@]}"; do
    echo "Removing stale checkpoint restore residue: ${residue}"
    bounded_rm_rf "${residue}" \
      || echo "::warning::Could not remove stale checkpoint residue ${residue}; the disk guard will account for the remaining bytes."
  done
}

ensure_build_disk_space() {
  local minimum_gb="${1:-20}"
  local available
  cleanup_stale_checkpoint_residue || true
  available="$(available_disk_gb "${WORKSPACE}")"
  echo "Available disk space: ${available} GiB; target minimum: ${minimum_gb} GiB."
  if [ "${available}" -ge "${minimum_gb}" ]; then
    return 0
  fi

  echo "::warning::Disk space is below the preferred threshold; trimming expendable caches."
  ccache --max-size=2G || true
  timeout -k 10s 120s ccache --cleanup || true
  rm -f "${WORKSPACE}/.chromium-source-cache"/chromium-*.tar.xz || true
  timeout -k 10s 60s sudo apt-get clean || true

  available="$(available_disk_gb "${WORKSPACE}")"
  echo "Available disk space after cleanup: ${available} GiB."
  if [ "${available}" -lt 10 ]; then
    echo "::error::Insufficient disk space to safely continue Chromium compilation."
    return 1
  fi
}

classify_build_failure() {
  local log_file="${1:?log file is required}"
  if grep -Eqi 'error while loading shared libraries|=> not found|cannot open shared object file' "${log_file}"; then
    echo runtime_environment
  elif grep -Eqi 'No space left on device|Input/output error|Temporary failure in name resolution|Connection reset|TLS handshake|Could not resolve host|network is unreachable|Cannot allocate memory|out of memory|Killed process' "${log_file}"; then
    echo infrastructure
  else
    echo deterministic_build
  fi
}

write_stage_summary() {
  local version="${1:-unknown}"
  local stage="${2:-unknown}"
  local attempt="${3:-unknown}"
  local complete="${4:-unknown}"
  local failure_class="${5:-none}"
  local summary="${GITHUB_STEP_SUMMARY:-}"
  if [ -z "${summary}" ]; then
    return 0
  fi

  local progress="not available"
  if [ -s "${BUILD_LOG}" ]; then
    progress="$(grep -Eo '\[[0-9]+/[0-9]+\]' "${BUILD_LOG}" | tail -n1 || true)"
    progress="${progress:-not available}"
  fi
  local checkpoint_size="none"
  if [ -s "${CHECKPOINT_ARCHIVE}" ]; then
    checkpoint_size="$(du -h "${CHECKPOINT_ARCHIVE}" | awk '{print $1}')"
  fi
  {
    echo "## Chromium i686 stage summary"
    echo
    echo "| Field | Value |"
    echo "| --- | --- |"
    echo "| Chromium | \`${version}\` |"
    echo "| Stage | \`${stage}\` |"
    echo "| Attempt | \`${attempt}\` |"
    echo "| Complete | \`${complete}\` |"
    echo "| Failure class | \`${failure_class:-none}\` |"
    echo "| Last Ninja progress | \`${progress}\` |"
    echo "| Checkpoint | \`${checkpoint_size}\` |"
    echo "| Free disk | \`$(available_disk_gb "${WORKSPACE}") GiB\` |"
    echo
    echo "### ccache"
    echo '```text'
    ccache -s 2>/dev/null | head -n 20 || true
    echo '```'
  } >> "${summary}"
}

CHROMIUM_PREPARE_FAILURE_CLASS=""

classify_prepare_command_status() {
  local status="${1:-1}"
  local fallback="${2:-infrastructure}"
  case "${status}" in
    124|126|127|137|143) printf '%s\n' infrastructure ;;
    *) printf '%s\n' "${fallback}" ;;
  esac
}

verify_depot_tools_bootstrap() {
  CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
  local marker="${DEPOT_TOOLS}/python3_bin_reldir.txt"
  if [ ! -s "${marker}" ]; then
    echo "::error::Pinned depot_tools bootstrap did not create ${marker}."
    return 1
  fi

  local python_rel python_dir python_bin
  if ! python_rel="$(tr -d '\r\n' < "${marker}")"; then
    echo "::error::Could not read pinned depot_tools Python bootstrap marker."
    return 1
  fi
  if [ -z "${python_rel}" ] || [[ "${python_rel}" = /* ]] || [[ "${python_rel}" == *".."* ]] || [[ "${python_rel}" == *\\* ]]; then
    echo "::error::Pinned depot_tools wrote an unsafe Python bootstrap path: ${python_rel:-<empty>}"
    return 1
  fi
  python_dir="${DEPOT_TOOLS}/${python_rel}"
  python_bin="${python_dir}/python3"
  if [ ! -x "${python_bin}" ]; then
    echo "::error::Pinned depot_tools Python bootstrap is incomplete: ${python_bin} is not executable."
    return 1
  fi

  local wrapper_status=0
  bounded_external "${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS}" \
    "${DEPOT_TOOLS}/python-bin/python3" -c \
    'import pathlib,sys; print(f"depot_tools bootstrap Python: {pathlib.Path(sys.executable).resolve()}")' \
    || wrapper_status=$?
  if [ "${wrapper_status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${wrapper_status}" deterministic_build)"
    echo "::error::Pinned depot_tools Python wrapper is not executable after bootstrap (status ${wrapper_status})."
    return 1
  fi
  echo "Pinned depot_tools Python bootstrap marker: ${python_rel}"
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

install_depot_tools() {
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  local deps_file="${CHROMIUM_SRC}/DEPS"
  if [ ! -s "${deps_file}" ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Chromium DEPS is unavailable for pinned depot_tools resolution."
    return 1
  fi
  local revision
  if ! revision="$(python3 "${WORKSPACE}/scripts/chromium_tool_pins.py" --deps "${deps_file}" --field depot_tools_revision)"; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Could not resolve immutable depot_tools revision from Chromium DEPS."
    return 1
  fi
  [[ "${revision}" =~ ^[0-9a-f]{40}$ ]] || {
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Invalid Chromium-pinned depot_tools revision: ${revision}"
    return 1
  }

  if ! bounded_rm_rf "${DEPOT_TOOLS}" || ! mkdir -p "${DEPOT_TOOLS}"; then
    echo "::error::Could not prepare pinned depot_tools checkout directory."
    return 1
  fi
  if ! git -C "${DEPOT_TOOLS}" init -q \
      || ! git -C "${DEPOT_TOOLS}" remote add origin https://chromium.googlesource.com/chromium/tools/depot_tools.git; then
    echo "::error::Could not initialize pinned depot_tools checkout."
    return 1
  fi
  echo "Fetching Chromium-pinned depot_tools revision ${revision}."
  local fetch_status=0
  bounded_external "${CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS}" \
    git -C "${DEPOT_TOOLS}" fetch --depth=1 origin "${revision}" || fetch_status=$?
  if [ "${fetch_status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${fetch_status}" infrastructure)"
    echo "::error::Could not fetch Chromium-pinned depot_tools revision ${revision} (status ${fetch_status})."
    return 1
  fi
  if ! git -C "${DEPOT_TOOLS}" checkout -q --detach FETCH_HEAD; then
    echo "::error::Could not check out fetched depot_tools revision."
    return 1
  fi
  local checked_out
  if ! checked_out="$(git -C "${DEPOT_TOOLS}" rev-parse HEAD)"; then
    echo "::error::Could not read pinned depot_tools checkout revision."
    return 1
  fi
  if [ "${checked_out}" != "${revision}" ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Pinned depot_tools checkout mismatch: expected ${revision}, got ${checked_out}."
    return 1
  fi

  export DEPOT_TOOLS_UPDATE=0
  if ! printf '%s\n' 'DEPOT_TOOLS_UPDATE=0' >> "${GITHUB_ENV}" \
      || ! printf '%s\n' "${DEPOT_TOOLS}" >> "${GITHUB_PATH}" \
      || ! printf '%s\n' "${DEPOT_TOOLS}/.cipd_bin" >> "${GITHUB_PATH}"; then
    echo "::error::Could not export pinned depot_tools environment paths."
    return 1
  fi
  export PATH="${DEPOT_TOOLS}:${DEPOT_TOOLS}/.cipd_bin:${PATH}"

  if [ ! -x "${DEPOT_TOOLS}/ensure_bootstrap" ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Pinned depot_tools revision ${revision} lacks executable ensure_bootstrap."
    return 1
  fi
  echo "Bootstrapping Chromium-pinned depot_tools revision ${revision} without self-update."
  local bootstrap_status=0
  bounded_external "${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS}" \
    env DEPOT_TOOLS_UPDATE=0 "${DEPOT_TOOLS}/ensure_bootstrap" || bootstrap_status=$?
  if [ "${bootstrap_status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${bootstrap_status}" infrastructure)"
    echo "::error::Pinned depot_tools bootstrap failed with status ${bootstrap_status}."
    return 1
  fi

  local cipd_status=0
  bounded_external "${CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS}" "${DEPOT_TOOLS}/cipd" version || cipd_status=$?
  if [ "${cipd_status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${cipd_status}" infrastructure)"
    echo "::error::Pinned depot_tools CIPD client probe failed with status ${cipd_status}."
    return 1
  fi
  if ! verify_depot_tools_bootstrap; then
    return 1
  fi
  if ! checked_out="$(git -C "${DEPOT_TOOLS}" rev-parse HEAD)" || [ "${checked_out}" != "${revision}" ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::depot_tools revision changed during pinned bootstrap."
    return 1
  fi
  echo "Pinned depot_tools revision: ${checked_out}"
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

chromium_gn_version() {
  python3 "${WORKSPACE}/scripts/chromium_tool_pins.py"     --deps "${CHROMIUM_SRC}/DEPS" --field gn_version
}

chromium_depot_tools_revision() {
  python3 "${WORKSPACE}/scripts/chromium_tool_pins.py"     --deps "${CHROMIUM_SRC}/DEPS" --field depot_tools_revision
}

resolve_latest_version() {
  python3 - <<'PY'
import json
import urllib.request
from urllib.parse import urlsplit

url = "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions"
try:
    with urllib.request.urlopen(url, timeout=60) as response:
        effective = response.geturl()
        parsed = urlsplit(effective)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "versionhistory.googleapis.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise RuntimeError(
                f"version-history request escaped trusted host: {effective!r}"
            )
        data = json.load(response)
except Exception as exc:
    raise SystemExit(f"Failed to resolve latest Chromium version: {exc}")

version = ((data.get("versions") or [{}])[0]).get("version")
if not version:
    raise SystemExit("Failed to resolve latest Chromium version: response did not include versions[0].version")
print(version)
PY
}

validate_extracted_chromium_version() {
  local expected="${1:?expected version is required}"
  local version_file="${CHROMIUM_SRC}/chrome/VERSION"
  test -s "${version_file}"
  local major minor build patch actual
  major="$(awk -F= '$1 == "MAJOR" {print $2}' "${version_file}")"
  minor="$(awk -F= '$1 == "MINOR" {print $2}' "${version_file}")"
  build="$(awk -F= '$1 == "BUILD" {print $2}' "${version_file}")"
  patch="$(awk -F= '$1 == "PATCH" {print $2}' "${version_file}")"
  actual="${major}.${minor}.${build}.${patch}"
  if [ "${actual}" != "${expected}" ]; then
    echo "::error::Extracted Chromium version ${actual} does not match requested ${expected}."
    return 1
  fi
  echo "Verified extracted Chromium version: ${actual}"
}

CHROMIUM_SOURCE_FAILURE_CLASS=""

source_archive_stats_are_usable() {
  local stats_file="${1:?source archive stats path is required}"
  local version="${2:?source version is required}"
  local source_sha="${3:?source SHA-256 is required}"
  validate_bounded_positive_policy \
    "${CHROMIUM_I686_MAX_SOURCE_MEMBERS}" CHROMIUM_I686_MAX_SOURCE_MEMBERS \
    "${CHROMIUM_I686_HARD_MAX_SOURCE_MEMBERS}" || return 1
  validate_bounded_positive_policy \
    "${CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB}" CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB \
    "${CHROMIUM_I686_HARD_MAX_SOURCE_UNPACKED_GIB}" || return 1
  SOURCE_MAX_MEMBERS="${CHROMIUM_I686_MAX_SOURCE_MEMBERS}" \
  SOURCE_MAX_UNPACKED_GIB="${CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB}" \
  python3 - "${stats_file}" "${version}" "${source_sha}" <<'PY' >/dev/null 2>&1
import json
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
version = sys.argv[2]
source_sha = sys.argv[3].lower()
if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
    raise SystemExit(1)
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
member_count = payload.get("member_count")
unpacked_bytes = payload.get("unpacked_bytes")
max_members = int(os.environ["SOURCE_MAX_MEMBERS"])
max_unpacked_bytes = int(os.environ["SOURCE_MAX_UNPACKED_GIB"]) * 1024**3
if (
    payload.get("version") != version
    or str(payload.get("source_sha256", "")).lower() != source_sha
    or not isinstance(member_count, int)
    or member_count <= 0
    or not isinstance(unpacked_bytes, int)
    or unpacked_bytes < 0
    or member_count > max_members
    or unpacked_bytes > max_unpacked_bytes
):
    raise SystemExit(1)
PY
}

ensure_source_archive_extract_space() {
  local stats_file="${1:?source archive stats path is required}"
  local target_parent="${2:?source extraction parent is required}"
  if ! validate_bounded_reserve_gib \
      "${CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB}" CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB; then
    return 1
  fi
  local unpacked_bytes available_bytes reserve_bytes required_bytes required_gib
  if ! unpacked_bytes="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("unpacked_bytes"); assert isinstance(v,int) and v >= 0; print(v)' "${stats_file}" 2>/dev/null)"; then
    echo "::error::Source archive stats are missing or malformed: ${stats_file}"
    return 1
  fi
  reserve_bytes=$((CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB * 1024 * 1024 * 1024))
  required_bytes=$((unpacked_bytes + reserve_bytes))
  if ! mkdir -p "${target_parent}"; then
    echo "::error::Could not create Chromium source extraction target: ${target_parent}"
    return 1
  fi
  available_bytes="$(df -PB1 "${target_parent}" | awk 'NR == 2 {print $4}')"
  [[ "${available_bytes}" =~ ^[0-9]+$ ]] || {
    echo "::error::Could not determine free disk bytes for Chromium source extraction."
    return 1
  }
  required_gib=$(( (required_bytes + 1024 * 1024 * 1024 - 1) / (1024 * 1024 * 1024) ))
  echo "Source extraction requires ${required_bytes} bytes including reserve (~${required_gib} GiB); ${available_bytes} bytes are available."
  if [ "${available_bytes}" -lt "${required_bytes}" ]; then
    echo "::error::Insufficient disk space for bounded Chromium source extraction."
    return 1
  fi
}

validate_chromium_source_tarball() {
  local tarball="${1:?source tarball is required}"
  local version="${2:?version is required}"
  local source_sha="${3:?source SHA-256 is required}"
  local stats_file="${4:?source archive stats path is required}"
  if ! bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
      python3 "${WORKSPACE}/scripts/validate_chromium_source_archive.py" \
        "${tarball}" --version "${version}" --source-sha256 "${source_sha}" \
        --stats-file "${stats_file}"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
    echo "::error::Chromium ${version} source archive failed safe-path/structure/resource validation: ${tarball}"
    return 1
  fi
  if ! source_archive_stats_are_usable "${stats_file}" "${version}" "${source_sha}"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
    echo "::error::Chromium source archive validator did not produce SHA-bound usable stats."
    return 1
  fi
}


validate_effective_https_host() {
  local url="${1:?effective URL is required}"
  local expected_host="${2:?expected host is required}"
  EFFECTIVE_URL="${url}" EXPECTED_HOST="${expected_host}" python3 - <<'PY'
import os
from urllib.parse import urlsplit

url = os.environ["EFFECTIVE_URL"]
expected = os.environ["EXPECTED_HOST"]
parsed = urlsplit(url)
if (
    parsed.scheme != "https"
    or parsed.hostname != expected
    or parsed.username is not None
    or parsed.password is not None
    or parsed.port not in (None, 443)
):
    raise SystemExit(
        f"Refusing redirected trust endpoint {url!r}; expected https://{expected}/"
    )
PY
}
validate_chromium_critical_source_identity() {
  local version="${1:?version is required}"
  local rel encoded decoded local_sha remote_sha effective_url
  local -a critical_files=(
    DEPS
    chrome/VERSION
    BUILD.gn
    chrome/installer/linux/BUILD.gn
  )
  for rel in "${critical_files[@]}"; do
    encoded="${RUNNER_TEMP:-${WORKSPACE}}/chromium-${version}-${rel//\//_}.b64"
    decoded="${encoded%.b64}.upstream"
    if ! effective_url="$(curl --fail --location --retry 4 --retry-all-errors --retry-delay 5 \
        --proto '=https' --proto-redir '=https' \
        --connect-timeout 20 --max-time 120 \
        --write-out '%{url_effective}' \
        "https://chromium.googlesource.com/chromium/src/+/refs/tags/${version}/${rel}?format=TEXT" \
        -o "${encoded}")"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
      echo "::error::Could not fetch authoritative Chromium ${version} ${rel} for critical-source identity validation."
      return 1
    fi
    if ! validate_effective_https_host "${effective_url}" chromium.googlesource.com; then
      CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
      echo "::error::Gitiles critical-source request escaped the trusted Chromium host."
      return 1
    fi
    if ! base64 --decode "${encoded}" > "${decoded}"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
      echo "::error::Gitiles returned invalid base64 for Chromium ${version} ${rel}."
      return 1
    fi
    if ! local_sha="$(sha256sum "${CHROMIUM_SRC}/${rel}" | awk '{print $1}')"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
      echo "::error::Extracted Chromium source lacks readable critical file ${rel}."
      return 1
    fi
    if ! remote_sha="$(sha256sum "${decoded}" | awk '{print $1}')"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
      echo "::error::Could not hash downloaded Gitiles proof for ${rel}."
      return 1
    fi
    echo "Critical source identity ${rel}: local=${local_sha} upstream=${remote_sha}"
    rm -f "${encoded}" "${decoded}"
    if [ "${local_sha}" != "${remote_sha}" ]; then
      CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
      echo "::error::Chromium source archive critical file ${rel} does not match authoritative tag ${version}."
      return 1
    fi
  done
  echo "Verified Chromium ${version} critical source files against the authoritative Gitiles tag."
}

prepare_chromium_source() {
  local version="${1:?version is required}"
  local cache_dir="${WORKSPACE}/.chromium-source-cache"
  local tarball="${cache_dir}/chromium-${version}.tar.xz"
  local source_url="https://commondatastorage.googleapis.com/chromium-browser-official/chromium-${version}.tar.xz"
  local metadata="${cache_dir}/chromium-${version}.source-object.json"
  local marker="${cache_dir}/chromium-${version}.validated.json"
  local source_stats="${cache_dir}/chromium-${version}.source-archive-stats.json"
  local trusted_marker=false
  local source_sha=""
  local effective_source_url=""
  CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
  if ! bounded_rm_rf "${CHROMIUM_SRC}"; then
    echo "::error::Could not clear prior Chromium source tree."
    return 1
  fi
  if ! mkdir -p "${CHROMIUM_SRC}" "${cache_dir}"; then
    echo "::error::Could not create Chromium source/cache directories."
    return 1
  fi

  if [ -s "${tarball}" ]; then
    echo "Verifying cached Chromium ${version} bytes against authoritative GCS object metadata."
    if bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
        python3 "${WORKSPACE}/scripts/chromium_source_object.py" \
          --version "${version}" --file "${tarball}" --metadata-out "${metadata}"; then
      if ! source_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "${metadata}")"; then
        CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
        echo "::error::Could not read verified Chromium source SHA-256 metadata."
        return 1
      fi
      if python3 "${WORKSPACE}/scripts/chromium_source_object.py" \
          --metadata-in "${metadata}" --marker "${marker}" --version "${version}" \
          --check-marker >/dev/null 2>&1; then
        trusted_marker=true
        if source_archive_stats_are_usable "${source_stats}" "${version}" "${source_sha}"; then
          echo "Cached Chromium ${version} matches prior safe/Gitiles proof plus SHA-bound archive stats; skipping redundant decompression scan."
        else
          echo "Cached Chromium trust marker is valid but archive stats are absent/stale; regenerating bounded stats once."
          if ! validate_chromium_source_tarball "${tarball}" "${version}" "${source_sha}" "${source_stats}"; then
            echo "::error::Authoritative GCS source bytes no longer satisfy the bounded archive contract."
            rm -f "${tarball}" "${tarball}.sha256" "${metadata}" "${marker}" "${source_stats}"
            return 1
          fi
        fi
      else
        echo "Cached bytes are authoritative but have no matching safety marker; performing the full archive scan."
        if ! validate_chromium_source_tarball "${tarball}" "${version}" "${source_sha}" "${source_stats}"; then
          echo "::error::Authoritative GCS source bytes are structurally unsafe; refusing a redundant redownload of the same object."
          rm -f "${tarball}" "${tarball}.sha256" "${metadata}" "${marker}" "${source_stats}"
          return 1
        fi
      fi
    else
      echo "::warning::Discarding cached Chromium source bytes that do not match the authoritative GCS object."
      rm -f "${tarball}" "${tarball}.sha256" "${metadata}" "${marker}" "${source_stats}"
    fi
  fi

  if [ ! -s "${tarball}" ]; then
    echo "Downloading Chromium ${version} source tarball..."
    rm -f "${tarball}.partial" "${metadata}" "${source_stats}"
    if ! effective_source_url="$(curl --fail --location --retry 5 --retry-all-errors --retry-delay 10 \
        --proto '=https' --proto-redir '=https' \
        --connect-timeout 30 --max-time "${CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS}" \
        --write-out '%{url_effective}' \
        "${source_url}" -o "${tarball}.partial")"; then
      rm -f "${tarball}.partial"
      return 1
    fi
    if ! validate_effective_https_host "${effective_source_url}" commondatastorage.googleapis.com; then
      CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
      echo "::error::Chromium source download escaped the trusted GCS download host."
      rm -f "${tarball}.partial"
      return 1
    fi
    if ! bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
        python3 "${WORKSPACE}/scripts/chromium_source_object.py" \
          --version "${version}" --file "${tarball}.partial" --metadata-out "${metadata}"; then
      rm -f "${tarball}.partial" "${metadata}"
      return 1
    fi
    if ! source_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "${metadata}")"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
      echo "::error::Could not read verified downloaded Chromium source SHA-256 metadata."
      rm -f "${tarball}.partial" "${metadata}" "${source_stats}"
      return 1
    fi
    if python3 "${WORKSPACE}/scripts/chromium_source_object.py" \
        --metadata-in "${metadata}" --marker "${marker}" --version "${version}" \
        --check-marker >/dev/null 2>&1; then
      trusted_marker=true
      if source_archive_stats_are_usable "${source_stats}" "${version}" "${source_sha}"; then
        echo "Redownloaded bytes exactly match an existing trusted marker and SHA-bound archive stats."
      else
        echo "Redownloaded trusted bytes lack matching bounded archive stats; regenerating them once."
        if ! validate_chromium_source_tarball "${tarball}.partial" "${version}" "${source_sha}" "${source_stats}"; then
          rm -f "${tarball}.partial" "${source_stats}"
          return 1
        fi
      fi
    else
      if ! validate_chromium_source_tarball "${tarball}.partial" "${version}" "${source_sha}" "${source_stats}"; then
        rm -f "${tarball}.partial" "${source_stats}"
        return 1
      fi
    fi
    if ! mv "${tarball}.partial" "${tarball}"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
      echo "::error::Could not promote verified Chromium source download into the cache."
      return 1
    fi
  fi

  if [ -z "${source_sha}" ]; then
    if ! source_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "${metadata}")"; then
      CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
      echo "::error::Could not recover Chromium source SHA-256 from verified metadata."
      return 1
    fi
  fi
  if ! [[ "${source_sha}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
    echo "::error::Prepared Chromium source SHA-256 is missing or malformed."
    return 1
  fi
  if ! source_archive_stats_are_usable "${source_stats}" "${version}" "${source_sha}"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
    echo "::error::Prepared Chromium source lacks SHA-bound bounded archive stats."
    return 1
  fi
  if ! printf '%s  %s\n' "${source_sha}" "$(basename "${tarball}")" > "${tarball}.sha256"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
    echo "::error::Could not write Chromium source SHA-256 sidecar."
    return 1
  fi

  if ! ensure_source_archive_extract_space "${source_stats}" "${CHROMIUM_SRC}"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
    return 1
  fi
  echo "Extracting Chromium ${version} source..."
  local extract_status=0
  bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
    tar -xJf "${tarball}" -C "${CHROMIUM_SRC}" --strip-components=1 || extract_status=$?
  if [ "${extract_status}" -ne 0 ]; then
    CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
    echo "::error::Validated Chromium source extraction failed with status ${extract_status}."
    return 1
  fi
  if ! validate_extracted_chromium_version "${version}"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
    return 1
  fi
  if [ "${trusted_marker}" != "true" ]; then
    if ! validate_chromium_critical_source_identity "${version}"; then
      return 1
    fi
    if ! python3 "${WORKSPACE}/scripts/chromium_source_object.py" \
      --metadata-in "${metadata}" --marker "${marker}" --version "${version}" \
      --write-marker --safe-archive-verified --gitiles-identity-verified >/dev/null; then
      CHROMIUM_SOURCE_FAILURE_CLASS=infrastructure
      echo "::error::Could not persist Chromium source trust marker after successful validation."
      return 1
    fi
    echo "Recorded SHA-bound source safety/Gitiles identity marker for Chromium ${version}."
  else
    echo "Reused prior Gitiles identity proof for the exact same GCS generation, MD5, length and SHA-256."
  fi
  if ! python3 "${WORKSPACE}/scripts/chromium_tool_pins.py" --deps "${CHROMIUM_SRC}/DEPS"; then
    CHROMIUM_SOURCE_FAILURE_CLASS=deterministic_build
    echo "::error::Prepared Chromium DEPS no longer satisfies the pinned-tool contract."
    return 1
  fi
  CHROMIUM_SOURCE_FAILURE_CLASS=""
  echo "Extraction complete. Source size:"
  du -sh "${CHROMIUM_SRC}" || true
}

install_chromium_clang() {
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  if ! cd "${CHROMIUM_SRC}"; then
    echo "::error::Chromium source directory is unavailable for Clang installation."
    return 1
  fi
  local status=0
  bounded_external "${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS}" \
    python3 tools/clang/scripts/update.py || status=$?
  if [ "${status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${status}" infrastructure)"
    echo "::error::Chromium-pinned Clang installation failed with status ${status}."
    return 1
  fi
  if [ ! -x third_party/llvm-build/Release+Asserts/bin/clang \
      ] || [ ! -s third_party/llvm-build/Release+Asserts/cr_build_revision ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Chromium Clang update succeeded without the required compiler/revision outputs."
    return 1
  fi
  echo "Chromium clang revision:"
  if ! cat third_party/llvm-build/Release+Asserts/cr_build_revision; then
    CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
    return 1
  fi
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

install_i386_sysroot() {
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  if ! cd "${CHROMIUM_SRC}"; then
    echo "::error::Chromium source directory is unavailable for i386 sysroot installation."
    return 1
  fi
  local status=0
  bounded_external "${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS}" \
    python3 build/linux/sysroot_scripts/install-sysroot.py --arch=i386 || status=$?
  if [ "${status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${status}" infrastructure)"
    echo "::error::Chromium i386 sysroot installation failed with status ${status}."
    return 1
  fi
  local sysroot
  sysroot="$(find build/linux -maxdepth 1 -type d -name '*_i386-sysroot' -print -quit 2>/dev/null || true)"
  if [ -z "${sysroot}" ] || [ ! -d "${sysroot}" ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Chromium sysroot installer succeeded without an i386 sysroot directory."
    return 1
  fi
  echo "Pinned Chromium i386 sysroot: ${sysroot}"
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

patch_build_gn_for_x86_linux() {
  cd "${CHROMIUM_SRC}"
  python3 - <<'PY'
from pathlib import Path

path = Path("BUILD.gn")
text = path.read_text()
old = 'is_valid_x86_target || target_cpu != "x86" || v8_target_cpu == "arm",'
new = (
    'is_valid_x86_target || target_cpu != "x86" || '
    'v8_target_cpu == "arm" || target_os == "linux",'
)
if new not in text:
    if old not in text:
        raise SystemExit("Could not find x86 target assertion predicate in BUILD.gn")
    path.write_text(text.replace(old, new, 1))
PY
  echo "Patched BUILD.gn x86 predicate:"
  grep -n -A4 -B2 "target_cpu=x86.*target_os=linux\|is_valid_x86_target" BUILD.gn || true
}

write_lastchange() {
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  if ! cd "${CHROMIUM_SRC}" || ! mkdir -p build/util; then
    echo "::error::Could not prepare Chromium LASTCHANGE path."
    return 1
  fi
  if ! printf '%s\n' 'LASTCHANGE=0000000000000000000000000000000000000000-refs/heads/main@{#0}' > build/util/LASTCHANGE; then
    echo "::error::Could not write deterministic Chromium LASTCHANGE."
    return 1
  fi
  if ! cat build/util/LASTCHANGE; then
    echo "::error::Could not read back deterministic Chromium LASTCHANGE."
    return 1
  fi
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

configure_ccache() {
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  if ! mkdir -p "${CCACHE_DIR}"; then
    echo "::error::Could not create ccache directory ${CCACHE_DIR}."
    return 1
  fi
  ccache --set-config=cache_dir="${CCACHE_DIR}" || true
  ccache --set-config=compression=true || true
  ccache --set-config=compiler_check=content || true
  ccache --max-size="${CCACHE_MAX_SIZE:-8G}" || true
  ccache -s || true
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

install_gn_from_cipd() {
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  if ! cd "${CHROMIUM_SRC}"; then
    echo "::error::Chromium source directory is unavailable for GN installation."
    return 1
  fi
  local expected_version
  if ! expected_version="$(chromium_gn_version)"; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Could not resolve immutable GN version from Chromium DEPS."
    return 1
  fi
  if [[ ! "${expected_version}" =~ ^git_revision:[0-9a-f]{40}$ ]]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Chromium DEPS returned unsupported GN pin: ${expected_version}."
    return 1
  fi
  if [ -x "${GN_BINARY}" ]; then
    echo "Existing GN binary will be re-asserted against Chromium's exact CIPD pin: $("${GN_BINARY}" --version || true)"
  fi

  local host_arch
  case "$(uname -m)" in
    x86_64|amd64) host_arch=amd64 ;;
    aarch64|arm64) host_arch=arm64 ;;
    *)
      CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
      echo "::error::Unsupported GN host architecture: $(uname -m)"
      return 1
      ;;
  esac

  echo "Installing Chromium-pinned GN ${expected_version} from CIPD..."
  if ! mkdir -p "$(dirname "${GN_BINARY}")"; then
    echo "::error::Could not create GN install directory."
    return 1
  fi
  local status=0
  bounded_external "${CHROMIUM_I686_NETWORK_TIMEOUT_SECONDS}" \
    cipd install "gn/gn/linux-${host_arch}" "${expected_version}" \
      -root "$(dirname "${GN_BINARY}")" || status=$?
  if [ "${status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${status}" infrastructure)"
    echo "::error::Chromium-pinned GN CIPD install failed with status ${status}."
    return 1
  fi
  if [ ! -x "${GN_BINARY}" ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Pinned GN CIPD install completed without executable ${GN_BINARY}."
    return 1
  fi
  if ! "${GN_BINARY}" --version; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::Pinned GN binary is not executable after installation."
    return 1
  fi
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

configure_gn() {
  if ! install_gn_from_cipd; then
    return 1
  fi
  CHROMIUM_PREPARE_FAILURE_CLASS=infrastructure
  if ! cd "${CHROMIUM_SRC}" || ! mkdir -p out/Release_x86; then
    echo "::error::Could not prepare Chromium GN output directory."
    return 1
  fi
  local status=0
  bounded_external "${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS}" \
    "${GN_BINARY}" gen out/Release_x86 --args="$(chromium_i686_gn_args)" || status=$?
  if [ "${status}" -ne 0 ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS="$(classify_prepare_command_status "${status}" deterministic_build)"
    echo "::error::Chromium i686 GN graph generation failed with status ${status}."
    return 1
  fi
  if [ ! -s out/Release_x86/build.ninja ] || [ ! -s out/Release_x86/args.gn ]; then
    CHROMIUM_PREPARE_FAILURE_CLASS=deterministic_build
    echo "::error::GN generation succeeded without build.ninja/args.gn."
    return 1
  fi
  CHROMIUM_PREPARE_FAILURE_CLASS=""
}

run_build_until_checkpoint() {
  local output_file="${1:-${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}}"
  local started_at="${JOB_STARTED_AT:-}"
  local checkpoint_minutes="${JOB_CHECKPOINT_MINUTES:-340}"
  local now remaining status failure_class pass pass_log_start pass_log
  now="$(date +%s)"
  if ! validate_job_started_at "${started_at}" "${now}"; then
    echo "complete=false" >> "${output_file}"
    echo "failure_class=infrastructure" >> "${output_file}"
    return 1
  fi
  if ! validate_checkpoint_minutes "${checkpoint_minutes}"; then
    echo "complete=false" >> "${output_file}"
    echo "failure_class=deterministic_build" >> "${output_file}"
    return 1
  fi
  local cutoff=$((started_at + checkpoint_minutes * 60))
  local runtime_repairs=0

  if ! ensure_build_disk_space 20; then
    echo "complete=false" >> "${output_file}"
    echo "failure_class=infrastructure" >> "${output_file}"
    return 1
  fi

  cd "${CHROMIUM_SRC}"
  export PATH="${DEPOT_TOOLS}:${DEPOT_TOOLS}/.cipd_bin:${PATH}"
  export CCACHE_DIR
  : > "${BUILD_LOG}"

  for pass in 1 2 3; do
    now=$(date +%s)
    remaining=$((cutoff - now))
    if [ "${remaining}" -le 300 ]; then
      echo "::warning::Less than five minutes remain before checkpoint cutoff; saving state for the next job."
      echo "complete=false" >> "${output_file}"
      echo "failure_class=" >> "${output_file}"
      return 0
    fi

    echo "Starting compiler slice pass ${pass} at $(date)."
    echo "Job checkpoint cutoff is ${checkpoint_minutes} minutes after job start; build timeout for this pass is ${remaining} seconds."
    df -h

    pass_log_start="$(wc -c < "${BUILD_LOG}")"
    pass_log="${WORKSPACE}/build-stage-pass-${pass}.log"
    set +e
    set +o pipefail
    local -a build_targets=(chrome "chrome/installer/linux:installer_deps")
    timeout -k 120s "${remaining}s" autoninja -C out/Release_x86 -j3 "${build_targets[@]}" 2>&1 | tee -a "${BUILD_LOG}"
    status=${PIPESTATUS[0]}
    set -o pipefail
    set -e
    tail -c "+$((pass_log_start + 1))" "${BUILD_LOG}" > "${pass_log}"

    if [ "${status}" -eq 0 ]; then
      echo "Build finished at $(date)"
      echo "complete=true" >> "${output_file}"
      echo "failure_class=" >> "${output_file}"
      df -h
      ccache -s || true
      return 0
    fi

    now=$(date +%s)
    if [ "${status}" -eq 124 ]         || { { [ "${status}" -eq 137 ] || [ "${status}" -eq 143 ]; } && [ "${now}" -ge "${cutoff}" ]; }; then
      echo "Compiler slice reached the checkpoint cutoff at $(date); preserving work for the next job."
      echo "complete=false" >> "${output_file}"
      echo "failure_class=" >> "${output_file}"
      df -h
      ccache -s || true
      return 0
    fi

    if [ "${status}" -eq 137 ] || [ "${status}" -eq 143 ]; then
      echo "complete=false" >> "${output_file}"
      echo "failure_class=infrastructure" >> "${output_file}"
      echo "::error::Compiler was terminated before the checkpoint cutoff (status ${status}); treating this as an infrastructure failure."
      return "${status}"
    fi

    failure_class="$(classify_build_failure "${pass_log}")"
    echo "Failure class: ${failure_class}"

    if [ "${failure_class}" = "runtime_environment" ] && [ "${runtime_repairs}" -lt 2 ]; then
      echo "::warning::A generated ELF32 tool is missing host runtime libraries; attempting in-job repair before consuming a runner retry."
      local repair_status=0
      if repair_i386_runtime_from_build_log "${pass_log}"; then
        runtime_repairs=$((runtime_repairs + 1))
        continue
      else
        repair_status=$?
      fi
      if [ "${repair_status}" -eq 2 ]; then
        if verify_or_repair_i386_runtime_dependencies; then
          if [ "${I386_RUNTIME_REPAIR_CHANGED}" = "true" ]; then
            runtime_repairs=$((runtime_repairs + 1))
            continue
          fi
        fi
      fi
      failure_class="${I386_RUNTIME_REPAIR_FAILURE_CLASS:-${failure_class}}"
    fi

    echo "complete=false" >> "${output_file}"
    echo "failure_class=${failure_class}" >> "${output_file}"
    echo "::error::autoninja failed with status ${status} (${failure_class})"
    return "${status}"
  done

  echo "complete=false" >> "${output_file}"
  echo "failure_class=runtime_environment" >> "${output_file}"
  return 1
}

validate_i686_runtime_bundle() {
  local root="${1:?Runtime bundle root is required}"
  local -a required=(
    chrome
    chrome-wrapper
    chrome_crashpad_handler
    chrome_management_service
    chrome_sandbox
    libEGL.so
    libGLESv2.so
    icudtl.dat
    resources.pak
    locales
  )
  local item
  for item in "${required[@]}"; do
    test -e "${root}/${item}" || {
      echo "::error::Required runtime path is missing from package: ${item}"
      return 1
    }
  done
  local -a required_executables=(
    chrome
    chrome-wrapper
    chrome_crashpad_handler
    chrome_management_service
    chrome_sandbox
  )
  for item in "${required_executables[@]}"; do
    test -f "${root}/${item}" && test -x "${root}/${item}" || {
      echo "::error::Required runtime executable is missing execute permission: ${item}"
      return 1
    }
  done

  local path resolved file_output elf_class elf_machine root_real
  root_real="$(realpath "${root}" 2>/dev/null || true)"
  if [ -z "${root_real}" ] || [ ! -d "${root_real}" ]; then
    echo "::error::Could not resolve runtime bundle root: ${root}"
    return 1
  fi
  while IFS= read -r -d '' path; do
    resolved="$(readlink -f "${path}" 2>/dev/null || true)"
    if [ -z "${resolved}" ] || [ ! -e "${resolved}" ]; then
      echo "::error::Runtime bundle contains a broken symlink: ${path}"
      return 1
    fi
    case "${resolved}" in
      "${root_real}"/*) ;;
      *)
        echo "::error::Runtime bundle symlink escapes package root: ${path} -> ${resolved}"
        return 1
        ;;
    esac
  done < <(find "${root}" -type l -print0)

  while IFS= read -r -d '' path; do
    file_output="$(file "${path}" 2>/dev/null || true)"
    if grep -q 'ELF ' <<<"${file_output}"; then
      printf '%s\n' "${file_output}"
      elf_class="$(readelf -h "${path}" | awk -F: '/Class:/ {gsub(/^[[:space:]]+/, "", $2); print $2}')"
      elf_machine="$(readelf -h "${path}" | awk -F: '/Machine:/ {gsub(/^[[:space:]]+/, "", $2); print $2}')"
      if [ "${elf_class}" != "ELF32" ] || ! grep -q 'Intel 80386' <<<"${elf_machine}"; then
        echo "::error::Runtime bundle contains a non-i686 ELF file: ${path}"
        return 1
      fi
    fi
  done < <(find "${root}" -type f -print0)
}

run_extended_i686_preflight() {
  python3 "${WORKSPACE}/scripts/chromium_linux_runtime.py" \
    --source-root "${CHROMIUM_SRC}" --validate-definition

  echo "Confirming that the generated Ninja graph contains the upstream Linux installer dependency group."
  ninja -C "${OUT_DIR}" -t query 'chrome/installer/linux:installer_deps' \
    >> "${WORKSPACE}/i686-preflight-ninja-query.txt"
  grep -q '^chrome/installer/linux:installer_deps:' "${WORKSPACE}/i686-preflight-ninja-query.txt"

  local sysroot
  sysroot="$(find "${CHROMIUM_SRC}/build/linux" -maxdepth 1 -type d -name '*_i386-sysroot' -print -quit)"
  test -n "${sysroot}"
  local clang="${CHROMIUM_SRC}/third_party/llvm-build/Release+Asserts/bin/clang"
  local canary_c="${RUNNER_TEMP:-${WORKSPACE}}/chromium-i686-target-canary.c"
  local canary_bin="${RUNNER_TEMP:-${WORKSPACE}}/chromium-i686-target-canary"
  cat > "${canary_c}" <<'EOF'
#include <stdio.h>
int main(void) { puts("chromium i686 target canary ok"); return 0; }
EOF
  bounded_external 120 "${clang}" --target=i386-linux-gnu --sysroot="${sysroot}" \
    -fuse-ld=lld "${canary_c}" -o "${canary_bin}"
  local file_output
  file_output="$(file "${canary_bin}")"
  printf '%s\n' "${file_output}"
  grep -Eq 'ELF 32-bit.*Intel (80386|i386)' <<<"${file_output}"
  "${canary_bin}"
}

validate_release_archive_with_stats() {
  RELEASE_ARCHIVE_FAILURE_CLASS=deterministic_build
  local package="${1:?release package is required}"
  local stats_file="${2:?release archive stats path is required}"
  if ! rm -f "${stats_file}"; then
    RELEASE_ARCHIVE_FAILURE_CLASS=infrastructure
    echo "::error::Could not clear stale release archive stats."
    return 1
  fi
  local status=0
  bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
    python3 "${WORKSPACE}/scripts/validate_release_archive.py" \
      "${package}" --stats-file "${stats_file}" || status=$?
  if [ "${status}" -ne 0 ]; then
    RELEASE_ARCHIVE_FAILURE_CLASS="$(classify_prepare_command_status "${status}" deterministic_build)"
    echo "::error::Release archive validation failed with status ${status}."
    return 1
  fi
  if [ ! -s "${stats_file}" ]; then
    echo "::error::Release archive validator succeeded without emitting stats."
    return 1
  fi
  RELEASE_ARCHIVE_FAILURE_CLASS=""
}

ensure_release_archive_extract_space() {
  RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=deterministic_build
  local stats_file="${1:?release archive stats path is required}"
  local target_parent="${2:?release extraction parent is required}"
  if ! validate_bounded_reserve_gib \
      "${CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB}" CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB; then
    return 1
  fi
  local unpacked_bytes available_bytes reserve_bytes required_bytes required_gib
  if ! unpacked_bytes="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("unpacked_bytes"); assert isinstance(v,int) and v >= 0; print(v)' "${stats_file}" 2>/dev/null)"; then
    echo "::error::Release archive stats are missing or malformed: ${stats_file}"
    return 1
  fi
  reserve_bytes=$((CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB * 1024 * 1024 * 1024))
  required_bytes=$((unpacked_bytes + reserve_bytes))
  if ! mkdir -p "${target_parent}"; then
    RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=infrastructure
    echo "::error::Could not create release extraction target: ${target_parent}"
    return 1
  fi
  if ! available_bytes="$(df -PB1 "${target_parent}" | awk 'NR == 2 {print $4}')"; then
    RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=infrastructure
    echo "::error::Could not query free disk bytes for release extraction."
    return 1
  fi
  [[ "${available_bytes}" =~ ^[0-9]+$ ]] || {
    RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=infrastructure
    echo "::error::Could not determine free disk bytes for release extraction."
    return 1
  }
  required_gib=$(( (required_bytes + 1024 * 1024 * 1024 - 1) / (1024 * 1024 * 1024) ))
  echo "Release extraction requires ${required_bytes} bytes including reserve (~${required_gib} GiB); ${available_bytes} bytes are available."
  if [ "${available_bytes}" -lt "${required_bytes}" ]; then
    RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=infrastructure
    echo "::error::Insufficient disk space for bounded release extraction."
    return 1
  fi
  RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS=""
}

CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS=""

classify_runtime_smoke_status() {
  case "${1:-1}" in
    124|126|127|137|143) printf '%s\n' infrastructure ;;
    *) printf '%s\n' deterministic_build ;;
  esac
}

smoke_test_i686_runtime_bundle() {
  local root="${1:?Runtime bundle root is required}"
  local version="${2:?Chromium version is required}"
  CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS=deterministic_build

  local launcher="${root}/chrome-wrapper"
  local browser="${root}/chrome"
  if [ ! -x "${launcher}" ] || [ ! -x "${browser}" ]; then
    echo "::error::Packaged Chromium launcher/browser is not executable."
    return 1
  fi

  local runtime_library_path="${root}"
  if [ -d "${root}/lib" ]; then
    runtime_library_path="${runtime_library_path}:${root}/lib"
  fi
  if [ -n "${LD_LIBRARY_PATH:-}" ]; then
    runtime_library_path="${runtime_library_path}:${LD_LIBRARY_PATH}"
  fi

  # The baseline i386 set is for host build tools and is intentionally small.
  # Validate only GPU runtime objects that the standalone package explicitly
  # promises. Avoid broad shared-object scanning: optional Qt shims may target a
  # toolkit that is intentionally absent on this distro and are selected lazily.
  local runtime_object repair_status=0
  local -a gpu_runtime_objects=(libEGL.so libGLESv2.so)
  if [ -f "${root}/libvk_swiftshader.so" ]; then
    gpu_runtime_objects+=(libvk_swiftshader.so)
  fi
  for runtime_object in "${gpu_runtime_objects[@]}"; do
    if [ ! -f "${root}/${runtime_object}" ]; then
      CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS=deterministic_build
      echo "::error::Required packaged GPU runtime object is missing: ${runtime_object}"
      return 1
    fi
    repair_status=0
    LD_LIBRARY_PATH="${runtime_library_path}" \
      repair_missing_i386_runtime_for_binary "${root}/${runtime_object}" || repair_status=$?
    if [ "${repair_status}" -ne 0 ]; then
      CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS="${I386_RUNTIME_REPAIR_FAILURE_CLASS:-deterministic_build}"
      echo "::error::Could not establish i386 dependencies for packaged GPU runtime object ${runtime_object} (status ${repair_status})."
      return 1
    fi
  done

  # Before executing the target browser, resolve/install any additional Ubuntu
  # i386 providers it genuinely needs. This keeps smoke failures about the
  # package/runtime contract instead of incidental runner package inventory.
  repair_status=0
  LD_LIBRARY_PATH="${runtime_library_path}" \
    repair_missing_i386_runtime_for_binary "${browser}" || repair_status=$?
  if [ "${repair_status}" -ne 0 ]; then
    CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS="${I386_RUNTIME_REPAIR_FAILURE_CLASS:-deterministic_build}"
    echo "::error::Could not establish the packaged Chromium target i386 runtime (status ${repair_status})."
    return 1
  fi

  local ldd_output ldd_status=0
  ldd_output="$(LD_LIBRARY_PATH="${runtime_library_path}" bounded_ldd "${browser}" 2>&1)" || ldd_status=$?
  printf '%s\n' "${ldd_output}"
  if [ "${ldd_status}" -ne 0 ]; then
    CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS="$(classify_runtime_smoke_status "${ldd_status}")"
    echo "::error::Packaged Chromium loader probe failed with status ${ldd_status}."
    return 1
  fi
  if grep -q '=> not found' <<<"${ldd_output}"; then
    echo "::error::Packaged Chromium has unresolved dynamic-library dependencies."
    return 1
  fi

  local smoke_root="${WORKSPACE}/release-runtime-smoke-${version}-${GITHUB_RUN_ID:-$$}-${RANDOM}"
  local smoke_home="${smoke_root}/home"
  local smoke_profile="${smoke_root}/profile"
  local smoke_html="${smoke_root}/runtime-smoke.html"
  bounded_rm_rf "${smoke_root}" || true
  mkdir -p "${smoke_home}" "${smoke_profile}"
  printf '%s\n' '<!doctype html><meta charset="utf-8"><main id="probe">chromium-i686-runtime-smoke</main>' > "${smoke_html}"

  local version_output version_status=0
  version_output="$(bounded_external "${CHROMIUM_I686_RUNTIME_SMOKE_TIMEOUT_SECONDS}" \
    env HOME="${smoke_home}" XDG_CONFIG_HOME="${smoke_home}/.config" XDG_CACHE_HOME="${smoke_home}/.cache" \
      "${launcher}" --version 2>&1)" || version_status=$?
  printf '%s\n' "${version_output}"
  if [ "${version_status}" -ne 0 ]; then
    CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS="$(classify_runtime_smoke_status "${version_status}")"
    bounded_rm_rf "${smoke_root}" || true
    echo "::error::Packaged chrome-wrapper --version failed with status ${version_status}."
    return 1
  fi
  if ! grep -Fxq "Chromium ${version}" <<<"${version_output}"; then
    bounded_rm_rf "${smoke_root}" || true
    echo "::error::Packaged chrome-wrapper reported an unexpected Chromium version: ${version_output}"
    return 1
  fi

  local dom_output dom_status=0
  dom_output="$(bounded_external "${CHROMIUM_I686_RUNTIME_SMOKE_TIMEOUT_SECONDS}" \
    env HOME="${smoke_home}" XDG_CONFIG_HOME="${smoke_home}/.config" XDG_CACHE_HOME="${smoke_home}/.cache" \
      "${launcher}" \
        --headless \
        --no-sandbox \
        --disable-gpu \
        --disable-dev-shm-usage \
        --disable-background-networking \
        --disable-component-update \
        --no-first-run \
        --no-default-browser-check \
        --user-data-dir="${smoke_profile}" \
        --dump-dom "file://${smoke_html}" 2>&1)" || dom_status=$?
  printf '%s\n' "${dom_output}"
  bounded_rm_rf "${smoke_root}" || true
  if [ "${dom_status}" -ne 0 ]; then
    CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS="$(classify_runtime_smoke_status "${dom_status}")"
    echo "::error::Packaged Chromium headless launch failed with status ${dom_status}."
    return 1
  fi
  if ! grep -Fq 'chromium-i686-runtime-smoke' <<<"${dom_output}"; then
    echo "::error::Packaged Chromium headless launch returned success without rendering the local smoke document."
    return 1
  fi

  CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS=""
  echo "Packaged Chromium runtime smoke passed via chrome-wrapper (version + local headless DOM)."
}

package_chromium_i686() {
  local version="${1:?version is required}"
  CHROMIUM_PACKAGE_FAILURE_CLASS=deterministic_build
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "::error::Invalid Chromium package version: ${version}"
    return 1
  }
  if ! cd "${OUT_DIR}"; then
    echo "::error::Chromium output directory is unavailable for packaging: ${OUT_DIR}"
    return 1
  fi
  local package="${WORKSPACE}/chromium-${version}-linux-i686.tar.xz"
  local checksum="${package}.sha256"
  local manifest="${WORKSPACE}/chromium-${version}-linux-i686-manifest.txt"
  local runtime_list="${WORKSPACE}/chromium-${version}-linux-i686-runtime-files.txt"

  local runtime_status=0
  bounded_external "${CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS}" \
    python3 "${WORKSPACE}/scripts/chromium_linux_runtime.py" \
      --source-root "${CHROMIUM_SRC}" --out-dir "${OUT_DIR}" --output-list "${runtime_list}" \
      --render-wrapper || runtime_status=$?
  if [ "${runtime_status}" -ne 0 ]; then
    CHROMIUM_PACKAGE_FAILURE_CLASS="$(classify_prepare_command_status "${runtime_status}" deterministic_build)"
    echo "::error::Chromium Linux runtime definition/output closure failed with status ${runtime_status}."
    return 1
  fi
  local -a files=()
  if ! mapfile -t files < "${runtime_list}"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not read Chromium runtime package file list."
    return 1
  fi
  if [ "${#files[@]}" -eq 0 ]; then
    echo "::error::Chromium runtime collector produced an empty package."
    return 1
  fi

  if ! rm -f "${package}" "${checksum}" "${manifest}"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not clear stale Chromium release outputs."
    return 1
  fi
  local archive_status=0
  bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
    tar -cJf "${package}" -- "${files[@]}" || archive_status=$?
  if [ "${archive_status}" -ne 0 ]; then
    CHROMIUM_PACKAGE_FAILURE_CLASS="$(classify_prepare_command_status "${archive_status}" infrastructure)"
    echo "::error::Failed or timed out while packaging Chromium runtime files (status ${archive_status})."
    return 1
  fi
  local package_bytes size_status=0
  if ! package_bytes="$(stat -c %s "${package}" 2>/dev/null)"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not determine compressed Chromium package size."
    return 1
  fi
  validate_artifact_size_bytes \
    "${package_bytes}" "${CHROMIUM_I686_MAX_RELEASE_ARTIFACT_GIB}" \
    CHROMIUM_I686_MAX_RELEASE_ARTIFACT_GIB "${CHROMIUM_I686_HARD_MAX_RELEASE_ARTIFACT_GIB}" \
    || size_status=$?
  if [ "${size_status}" -ne 0 ]; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=$([ "${size_status}" -eq 2 ] && printf deterministic_build || printf infrastructure)
    echo "::error::Packaged Chromium archive violates compressed artifact size policy."
    return 1
  fi

  # Once compilation and archive creation succeeded, failures collecting local
  # provenance/sidecar state are recoverable runner/filesystem failures.
  CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
  local package_sha source_sha clang_revision gn_version depot_revision port_hash
  if ! package_sha="$(sha256sum "${package}" | awk '{print $1}')"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not hash packaged Chromium archive."
    return 1
  fi
  [[ "${package_sha}" =~ ^[0-9a-fA-F]{64}$ ]] || {
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Packaged Chromium SHA-256 is malformed."
    return 1
  }
  if ! (
    cd "${WORKSPACE}" \
      && printf '%s  %s\n' "${package_sha}" "$(basename "${package}")" > "$(basename "${checksum}")"
  ); then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not write packaged Chromium checksum sidecar."
    return 1
  fi

  local source_checksum_file="${WORKSPACE}/.chromium-source-cache/chromium-${version}.tar.xz.sha256"
  if [ ! -s "${source_checksum_file}" ]; then
    echo "::error::Prepared Chromium source checksum is missing during packaging."
    return 1
  fi
  if ! source_sha="$(awk 'NR == 1 {print $1}' "${source_checksum_file}")"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not read prepared Chromium source checksum."
    return 1
  fi
  [[ "${source_sha}" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "::error::Prepared Chromium source checksum is malformed during packaging."
    return 1
  }
  local clang_revision_file="${CHROMIUM_SRC}/third_party/llvm-build/Release+Asserts/cr_build_revision"
  if [ ! -s "${clang_revision_file}" ] || ! clang_revision="$(cat "${clang_revision_file}")"; then
    echo "::error::Chromium Clang revision metadata is missing during packaging."
    return 1
  fi
  if ! gn_version="$(chromium_gn_version)" \
      || ! depot_revision="$(chromium_depot_tools_revision)" \
      || ! port_hash="$(compute_port_config_sha256 "${version}")"; then
    echo "::error::Could not resolve immutable tool/port provenance for Chromium package."
    return 1
  fi
  [[ "${port_hash}" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "::error::Chromium package port configuration hash is malformed."
    return 1
  }

  if ! {
    echo "manifest_schema=2"
    echo "version=${version}"
    echo "target_cpu=x86"
    echo "target_os=linux"
    echo "source_tarball=https://commondatastorage.googleapis.com/chromium-browser-official/chromium-${version}.tar.xz"
    echo "source_tar_sha256=${source_sha}"
    echo "package_sha256=${package_sha}"
    echo "github_sha=${GITHUB_SHA}"
    echo "github_run_id=${GITHUB_RUN_ID}"
    echo "clang_revision=${clang_revision}"
    echo "gn_version=${gn_version}"
    echo "depot_tools_revision=${depot_revision}"
    echo "port_config_sha256=${port_hash}"
    echo "checkpoint_contract_version=${CHECKPOINT_CONTRACT_VERSION}"
    echo "runner_os=${RUNNER_OS:-unknown}"
    echo "runner_image=${ImageOS:-unknown}"
    echo "runner_image_version=${ImageVersion:-unknown}"
    echo
    echo "packaged_files:"
    printf '%s\n' "${files[@]}"
  } > "${manifest}"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not write Chromium release provenance manifest."
    return 1
  fi

  local release_stats="${WORKSPACE}/release-archive-stats-${version}.json"
  CHROMIUM_PACKAGE_FAILURE_CLASS=deterministic_build
  if ! validate_release_archive_with_stats "${package}" "${release_stats}"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS="${RELEASE_ARCHIVE_FAILURE_CLASS:-deterministic_build}"
    echo "::error::Packaged runtime archive failed safety/completeness/resource validation."
    return 1
  fi

  local smoke_dir="${WORKSPACE}/release-smoke-${version}"
  if ! bounded_rm_rf "${smoke_dir}" || ! mkdir -p "${smoke_dir}"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure
    echo "::error::Could not prepare isolated package smoke directory."
    rm -f "${release_stats}" || true
    return 1
  fi
  if ! ensure_release_archive_extract_space "${release_stats}" "${smoke_dir}"; then
    CHROMIUM_PACKAGE_FAILURE_CLASS="${RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS:-infrastructure}"
    bounded_rm_rf "${smoke_dir}" || true
    rm -f "${release_stats}" || true
    return 1
  fi
  local extract_status=0
  bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" \
    tar -xJf "${package}" -C "${smoke_dir}" || extract_status=$?
  if [ "${extract_status}" -ne 0 ]; then
    CHROMIUM_PACKAGE_FAILURE_CLASS="$(classify_prepare_command_status "${extract_status}" infrastructure)"
    echo "::error::Validated release archive extraction failed with status ${extract_status}."
    bounded_rm_rf "${smoke_dir}" || true
    rm -f "${release_stats}" || true
    return 1
  fi
  local smoke_status=0
  validate_i686_runtime_bundle "${smoke_dir}" || smoke_status=$?
  if [ "${smoke_status}" -eq 0 ]; then
    smoke_test_i686_runtime_bundle "${smoke_dir}" "${version}" || smoke_status=$?
  fi
  bounded_rm_rf "${smoke_dir}" || true
  rm -f "${release_stats}" || true
  if [ "${smoke_status}" -ne 0 ]; then
    CHROMIUM_PACKAGE_FAILURE_CLASS="${CHROMIUM_RUNTIME_SMOKE_FAILURE_CLASS:-deterministic_build}"
    echo "::error::Packaged runtime bundle failed i686/runtime smoke validation."
    return 1
  fi

  CHROMIUM_PACKAGE_FAILURE_CLASS=""
  ls -lh "${package}" "${checksum}" "${manifest}" || true
  return 0
}
