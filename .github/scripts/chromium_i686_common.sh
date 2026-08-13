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

maximize_runner_disk_space() {
  echo "=== Disk space BEFORE cleanup ==="
  df -h
  sudo rm -rf /usr/share/dotnet
  sudo rm -rf /usr/local/lib/android
  sudo rm -rf /opt/ghc
  sudo rm -rf /opt/hostedtoolcache/CodeQL
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
  echo "Adding 8G swap file to reduce OOM risk during Chromium linking..."
  sudo fallocate -l 8G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=8192 status=progress
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  swapon --show
}

install_system_dependencies() {
  bounded_sudo_apt_get update
  bounded_sudo_apt_get install -y \
    git python3 python3-pip curl jq xz-utils zstd zip unzip \
    build-essential pkg-config ninja-build ccache \
    libgtk-3-dev libnss3-dev libasound2-dev libxss-dev libxtst-dev libxrandr-dev \
    libxcomposite-dev libxdamage-dev libxfixes-dev libxrender-dev libxkbcommon-dev \
    libdrm-dev libgbm-dev libpango1.0-dev libcups2-dev libatk1.0-dev \
    libatspi2.0-dev libatk-bridge2.0-dev
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
  if [ ! -r /etc/os-release ]; then
    echo "::error::Cannot identify Linux runner: /etc/os-release is unavailable."
    return 1
  fi
  local ID="" VERSION_ID=""
  # shellcheck disable=SC1091
  source /etc/os-release
  RUNNER_DISTRO_ID="${ID:-unknown}"
  RUNNER_DISTRO_VERSION_ID="${VERSION_ID:-unknown}"
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

repair_missing_i386_runtime_for_binary() {
  local binary="${1:?binary is required}"
  local ldd_output soname package round current_missing previous_missing=""
  local -a missing_sonames=() packages=() to_install=()
  I386_RUNTIME_REPAIR_FAILURE_CLASS=runtime_environment
  I386_RUNTIME_REPAIR_CHANGED=false

  for round in 1 2 3; do
    ldd_output="$(ldd "${binary}" 2>&1 || true)"
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

  ldd_output="$(ldd "${binary}" 2>&1 || true)"
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

ensure_build_disk_space() {
  local minimum_gb="${1:-20}"
  local available
  available="$(available_disk_gb "${WORKSPACE}")"
  echo "Available disk space: ${available} GiB; target minimum: ${minimum_gb} GiB."
  if [ "${available}" -ge "${minimum_gb}" ]; then
    return 0
  fi

  echo "::warning::Disk space is below the preferred threshold; trimming expendable caches."
  ccache --max-size=2G || true
  ccache --cleanup || true
  rm -f "${WORKSPACE}/.chromium-source-cache"/chromium-*.tar.xz || true
  sudo apt-get clean || true

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

install_depot_tools() {
  rm -rf "${DEPOT_TOOLS}"
  git clone --depth=1 https://chromium.googlesource.com/chromium/tools/depot_tools.git "${DEPOT_TOOLS}"
  echo "${DEPOT_TOOLS}" >> "${GITHUB_PATH}"
  echo "${DEPOT_TOOLS}/.cipd_bin" >> "${GITHUB_PATH}"
  export PATH="${DEPOT_TOOLS}:${DEPOT_TOOLS}/.cipd_bin:${PATH}"
  "${DEPOT_TOOLS}/update_depot_tools"
}

resolve_latest_version() {
  python3 - <<'PY'
import json
import sys
import urllib.request

url = "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions"
try:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = json.load(response)
except Exception as exc:
    raise SystemExit(f"Failed to resolve latest Chromium version: {exc}")

version = ((data.get("versions") or [{}])[0]).get("version")
if not version:
    raise SystemExit("Failed to resolve latest Chromium version: response did not include versions[0].version")
print(version)
PY
}

prepare_chromium_source() {
  local version="${1:?version is required}"
  local cache_dir="${WORKSPACE}/.chromium-source-cache"
  local tarball="${cache_dir}/chromium-${version}.tar.xz"
  rm -rf "${CHROMIUM_SRC}"
  mkdir -p "${CHROMIUM_SRC}" "${cache_dir}"
  if [ -s "${tarball}" ]; then
    echo "Using cached Chromium ${version} source tarball at ${tarball}"
  else
    echo "Downloading Chromium ${version} source tarball..."
    curl --fail --retry 5 --retry-delay 10 -L \
      "https://commondatastorage.googleapis.com/chromium-browser-official/chromium-${version}.tar.xz" \
      -o "${tarball}.partial"
    mv "${tarball}.partial" "${tarball}"
  fi
  if [ ! -s "${tarball}.sha256" ]; then
    (cd "${cache_dir}" && sha256sum "$(basename "${tarball}")" > "$(basename "${tarball}").sha256")
  fi
  (cd "${cache_dir}" && sha256sum -c "$(basename "${tarball}.sha256")")
  echo "Extracting Chromium ${version} source..."
  tar -xJf "${tarball}" -C "${CHROMIUM_SRC}" --strip-components=1
  echo "Extraction complete. Source size:"
  du -sh "${CHROMIUM_SRC}"
}

install_chromium_clang() {
  cd "${CHROMIUM_SRC}"
  python3 tools/clang/scripts/update.py
  test -x third_party/llvm-build/Release+Asserts/bin/clang
  test -s third_party/llvm-build/Release+Asserts/cr_build_revision
  echo "Chromium clang revision:"
  cat third_party/llvm-build/Release+Asserts/cr_build_revision
}

install_i386_sysroot() {
  cd "${CHROMIUM_SRC}"
  python3 build/linux/sysroot_scripts/install-sysroot.py --arch=i386
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
  cd "${CHROMIUM_SRC}"
  mkdir -p build/util
  echo "LASTCHANGE=0000000000000000000000000000000000000000-refs/heads/main@{#0}" > build/util/LASTCHANGE
  cat build/util/LASTCHANGE
}

configure_ccache() {
  mkdir -p "${CCACHE_DIR}"
  ccache --set-config=cache_dir="${CCACHE_DIR}" || true
  ccache --set-config=compression=true || true
  ccache --set-config=compiler_check=content || true
  ccache --max-size="${CCACHE_MAX_SIZE:-8G}" || true
  ccache -s || true
}

install_gn_from_cipd() {
  cd "${CHROMIUM_SRC}"
  if [ -x "${GN_BINARY}" ]; then
    "${GN_BINARY}" --version || true
    return 0
  fi

  echo "Installing prebuilt GN from CIPD..."
  mkdir -p "$(dirname "${GN_BINARY}")"
  cipd install gn/gn/linux-amd64 latest -root "$(dirname "${GN_BINARY}")"
  test -x "${GN_BINARY}"
  "${GN_BINARY}" --version || true
}

configure_gn() {
  cd "${CHROMIUM_SRC}"
  install_gn_from_cipd
  mkdir -p out/Release_x86
  "${GN_BINARY}" gen out/Release_x86 --args="$(chromium_i686_gn_args)"
}

run_build_until_checkpoint() {
  local output_file="${1:-${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}}"
  local started_at="${JOB_STARTED_AT:-$(date +%s)}"
  local checkpoint_minutes="${JOB_CHECKPOINT_MINUTES:-330}"
  local cutoff=$((started_at + checkpoint_minutes * 60))
  local now remaining status failure_class pass pass_log_start pass_log
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
    timeout -k 120s "${remaining}s" autoninja -C out/Release_x86 -j3 chrome 2>&1 | tee -a "${BUILD_LOG}"
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

create_out_checkpoint() {
  if [ ! -d "${OUT_DIR}" ]; then
    echo "::error::Expected build output directory not found: ${OUT_DIR}"
    exit 1
  fi
  mkdir -p "${CHECKPOINT_DIR}"
  rm -f "${CHECKPOINT_ARCHIVE}"
  echo "Creating ninja output checkpoint..."
  du -sh "${OUT_DIR}" || true
  tar -C "${CHROMIUM_SRC}/out" -I 'zstd -T0 -1' -cf "${CHECKPOINT_ARCHIVE}" Release_x86
  ls -lh "${CHECKPOINT_ARCHIVE}"
}

package_chromium_i686() {
  local version="${1:?version is required}"
  cd "${OUT_DIR}"
  local package="${WORKSPACE}/chromium-${version}-linux-i686.tar.xz"
  local manifest="${WORKSPACE}/chromium-${version}-linux-i686-manifest.txt"
  {
    echo "version=${version}"
    echo "target_cpu=x86"
    echo "target_os=linux"
    echo "source_tarball=https://commondatastorage.googleapis.com/chromium-browser-official/chromium-${version}.tar.xz"
    echo "github_sha=${GITHUB_SHA}"
    echo
    find . -maxdepth 1 -type f -printf '%P\n' | sort
  } > "${manifest}"

  shopt -s nullglob
  local files=(chrome)
  local optional
  for optional in chrome_sandbox locales; do
    if [ -e "${optional}" ]; then
      files+=("${optional}")
    fi
  done
  local extra_runtime=(*.pak *.bin *.dat)
  files+=("${extra_runtime[@]}")
  {
    echo
    echo "packaged_files:"
    printf '%s\n' "${files[@]}"
  } >> "${manifest}"

  tar -cJf "${package}" "${files[@]}" || {
    echo "::error::Failed to package Chromium runtime files"
    find . -maxdepth 1 -type f -printf '%P\n' | sort
    exit 1
  }
  sha256sum "${package}" > "${package}.sha256"
  ls -lh "${package}" "${package}.sha256" "${manifest}"
}

publish_chromium_release() {
  local version="${1:?version is required}"
  local package="${WORKSPACE}/chromium-${version}-linux-i686.tar.xz"
  local checksum="${package}.sha256"
  local manifest="${WORKSPACE}/chromium-${version}-linux-i686-manifest.txt"
  local release_tag="chromium-${version}-linux-i686"
  export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"

  if gh release view "${release_tag}" >/dev/null 2>&1; then
    gh release upload "${release_tag}" "${package}" "${checksum}" "${manifest}" --clobber
  else
    gh release create "${release_tag}" "${package}" "${checksum}" "${manifest}" \
      --target "${GITHUB_SHA}" \
      --title "Chromium ${version} Linux i686" \
      --notes "Chromium ${version} Linux i686 build from GitHub Actions run ${GITHUB_RUN_ID}."
  fi
}
