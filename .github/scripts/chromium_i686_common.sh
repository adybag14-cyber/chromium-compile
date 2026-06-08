#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
CHROMIUM_SRC="${WORKSPACE}/chromium_source"
DEPOT_TOOLS="${WORKSPACE}/depot_tools"
OUT_DIR="${CHROMIUM_SRC}/out/Release_x86"
CHECKPOINT_DIR="${WORKSPACE}/checkpoints"
CHECKPOINT_ARCHIVE="${CHECKPOINT_DIR}/out-Release_x86.tar.zst"
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
  echo "=== Disk space AFTER cleanup ==="
  df -h
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
  rm -rf "${CHROMIUM_SRC}"
  mkdir -p "${CHROMIUM_SRC}"
  echo "Streaming Chromium ${version} source download and extraction..."
  curl --fail --retry 5 --retry-delay 10 -L \
    "https://commondatastorage.googleapis.com/chromium-browser-official/chromium-${version}.tar.xz" \
    | tar -xJ -C "${CHROMIUM_SRC}" --strip-components=1
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

configure_gn() {
  cd "${CHROMIUM_SRC}"
  mkdir -p out/Release_x86
  gn gen out/Release_x86 --args='
    target_os="linux"
    target_cpu="x86"
    is_debug=false
    symbol_level=0
    blink_symbol_level=0
    enable_nacl=false
    is_official_build=false
    use_thin_lto=false
    use_reclient=false
    treat_warnings_as_errors=false
    cc_wrapper="ccache"
  '
}

run_build_until_checkpoint() {
  local output_file="${1:-${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}}"
  local started_at="${JOB_STARTED_AT:-$(date +%s)}"
  local checkpoint_minutes="${JOB_CHECKPOINT_MINUTES:-330}"
  local cutoff=$((started_at + checkpoint_minutes * 60))
  local now
  now=$(date +%s)
  local remaining=$((cutoff - now))

  if [ "${remaining}" -le 300 ]; then
    echo "::warning::Less than five minutes remain before checkpoint cutoff; saving state for the next job."
    echo "complete=false" >> "${output_file}"
    return 0
  fi

  cd "${CHROMIUM_SRC}"
  export PATH="${DEPOT_TOOLS}:${DEPOT_TOOLS}/.cipd_bin:${PATH}"
  export CCACHE_DIR

  echo "Starting compiler slice at $(date)."
  echo "Job checkpoint cutoff is ${checkpoint_minutes} minutes after job start; build timeout for this slice is ${remaining} seconds."
  echo "Disk space before build:"
  df -h
  set +e
  timeout -k 120s "${remaining}s" autoninja -C out/Release_x86 -j2 chrome
  local status=$?
  set -e

  if [ "${status}" -eq 0 ]; then
    echo "Build finished at $(date)"
    echo "complete=true" >> "${output_file}"
    df -h
    ccache -s || true
    return 0
  fi

  if [ "${status}" -eq 124 ] || [ "${status}" -eq 137 ] || [ "${status}" -eq 143 ]; then
    echo "Compiler slice reached the checkpoint cutoff at $(date); preserving work for the next job."
    echo "complete=false" >> "${output_file}"
    df -h
    ccache -s || true
    return 0
  fi

  echo "::error::autoninja failed with status ${status}"
  exit "${status}"
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
