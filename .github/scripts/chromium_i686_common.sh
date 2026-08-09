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
  sudo apt-get purge -y '^mysql-' '^mongodb-' '^postgresql-' '^dotnet-' '^android-sdk-' || true
  sudo apt-get autoremove -y || true
  sudo apt-get clean || true
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
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git python3 python3-pip curl jq xz-utils zstd zip unzip \
    build-essential pkg-config ninja-build ccache \
    libgtk-3-dev libnss3-dev libasound2-dev libxss-dev libxtst-dev libxrandr-dev \
    libxcomposite-dev libxdamage-dev libxfixes-dev libxrender-dev libxkbcommon-dev \
    libdrm-dev libgbm-dev libpango1.0-dev libcups2-dev libatk1.0-dev \
    libatspi2.0-dev libatk-bridge2.0-dev
}

I386_RUNTIME_PACKAGES=(
  libc6:i386
  libgcc-s1:i386
  libstdc++6:i386
  libglib2.0-0:i386
  libexpat1:i386
  libnspr4:i386
  libnss3:i386
  libdbus-1-3:i386
  libx11-6:i386
  libxext6:i386
  libgbm1:i386
  libxcb1:i386
  libxkbcommon0:i386
  libudev1:i386
  libasound2:i386
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
)

install_i386_runtime_libraries() {
  sudo dpkg --add-architecture i386
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${I386_RUNTIME_PACKAGES[@]}"
  verify_i386_host_runtime
}

verify_i386_host_runtime() {
  local soname
  local missing=0
  local cache
  cache="$(ldconfig -p 2>/dev/null || true)"
  for soname in "${!I386_SONAME_PACKAGES[@]}"; do
    if ! grep -Fq "${soname} (libc6,x86-32)" <<<"${cache}"; then
      case "${soname}" in
        libc.so.6|libdl.so.2|libpthread.so.0|libm.so.6|libgcc_s.so.1|libstdc++.so.6|libglib-2.0.so.0|libgobject-2.0.so.0|libgio-2.0.so.0|libgmodule-2.0.so.0)
          if grep -Eq "[[:space:]]${soname//./\\.}[[:space:]].*(x86-32|i386)" <<<"${cache}"; then
            continue
          fi
          ;;
      esac
      echo "::error::Required i386 runtime SONAME is unavailable on the runner: ${soname}"
      missing=1
    fi
  done
  if [ "${missing}" -ne 0 ]; then
    return 1
  fi
  echo "Verified required i386 host runtime SONAMEs."
}

repair_missing_i386_runtime_for_binary() {
  local binary="${1:?binary is required}"
  local ldd_output
  ldd_output="$(ldd "${binary}" 2>&1 || true)"
  printf '%s\n' "${ldd_output}"

  mapfile -t missing_sonames < <(awk '/=> not found/ {print $1}' <<<"${ldd_output}" | sort -u)
  if [ "${#missing_sonames[@]}" -eq 0 ]; then
    return 0
  fi

  local -a packages=()
  local soname package
  for soname in "${missing_sonames[@]}"; do
    package="${I386_SONAME_PACKAGES[${soname}]:-}"
    if [ -z "${package}" ]; then
      echo "::error::No automatic i386 package mapping is known for missing SONAME ${soname}."
      return 1
    fi
    packages+=("${package}")
  done
  mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort -u)
  echo "Repairing missing i386 runtime dependencies: ${packages[*]}"
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"

  ldd_output="$(ldd "${binary}" 2>&1 || true)"
  printf '%s\n' "${ldd_output}"
  ! grep -q '=> not found' <<<"${ldd_output}"
}

verify_or_repair_i386_runtime_dependencies() {
  if [ ! -d "${OUT_DIR}" ]; then
    return 0
  fi

  local -a candidates=()
  local binary file_output
  while IFS= read -r -d '' binary; do
    file_output="$(file "${binary}" 2>/dev/null || true)"
    if grep -Eq 'ELF 32-bit.*Intel (80386|i386)' <<<"${file_output}"; then
      candidates+=("${binary}")
    fi
  done < <(find "${OUT_DIR}" -maxdepth 1 -type f -perm -111 -print0)

  if [ "${#candidates[@]}" -eq 0 ]; then
    echo "No generated ELF32 build-time executables are present yet."
    return 0
  fi

  echo "Checking ${#candidates[@]} generated ELF32 build-time executable(s)."
  for binary in "${candidates[@]}"; do
    echo "Runtime check: ${binary}"
    repair_missing_i386_runtime_for_binary "${binary}" || return 1
  done
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
    local file
    while IFS= read -r file; do
      sha256sum "${file}"
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

restore_out_checkpoint() {
  local archive="${1:-}"
  mkdir -p "${CHROMIUM_SRC}/out"
  if [ -n "${archive}" ] && [ -s "${archive}" ]; then
    echo "Restoring previous ninja output checkpoint from ${archive}"
    rm -rf "${OUT_DIR}"
    tar -I 'zstd -T0 -d' -xf "${archive}" -C "${CHROMIUM_SRC}/out"
    du -sh "${OUT_DIR}" || true
  else
    echo "No previous output checkpoint found; continuing with ccache and a fresh out directory."
    mkdir -p "${OUT_DIR}"
  fi
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
  local now remaining status failure_class pass
  local repaired_runtime=false

  if ! ensure_build_disk_space 20; then
    echo "complete=false" >> "${output_file}"
    echo "failure_class=infrastructure" >> "${output_file}"
    return 1
  fi

  cd "${CHROMIUM_SRC}"
  export PATH="${DEPOT_TOOLS}:${DEPOT_TOOLS}/.cipd_bin:${PATH}"
  export CCACHE_DIR
  : > "${BUILD_LOG}"

  for pass in 1 2; do
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

    set +e
    set +o pipefail
    timeout -k 120s "${remaining}s" autoninja -C out/Release_x86 -j3 chrome 2>&1 | tee -a "${BUILD_LOG}"
    status=${PIPESTATUS[0]}
    set -o pipefail
    set -e

    if [ "${status}" -eq 0 ]; then
      echo "Build finished at $(date)"
      echo "complete=true" >> "${output_file}"
      echo "failure_class=" >> "${output_file}"
      df -h
      ccache -s || true
      return 0
    fi

    if [ "${status}" -eq 124 ] || [ "${status}" -eq 137 ] || [ "${status}" -eq 143 ]; then
      echo "Compiler slice reached the checkpoint cutoff at $(date); preserving work for the next job."
      echo "complete=false" >> "${output_file}"
      echo "failure_class=" >> "${output_file}"
      df -h
      ccache -s || true
      return 0
    fi

    failure_class="$(classify_build_failure "${BUILD_LOG}")"
    echo "Failure class: ${failure_class}"

    if [ "${failure_class}" = "runtime_environment" ] && [ "${repaired_runtime}" = "false" ]; then
      echo "::warning::A generated ELF32 tool is missing host runtime libraries; attempting in-job repair before consuming a runner retry."
      if verify_or_repair_i386_runtime_dependencies; then
        repaired_runtime=true
        continue
      fi
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
