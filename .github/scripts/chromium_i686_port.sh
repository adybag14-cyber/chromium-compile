#!/usr/bin/env bash
set -euo pipefail

apply_i686_port_patches() {
  local version="${1:?Chromium version is required}"
  local source_root="${CHROMIUM_SRC:?CHROMIUM_SRC is required}"
  local major="${version%%.*}"

  python3 "${GITHUB_WORKSPACE}/patches/common/enable_linux_i686.py" \
    --source-root "${source_root}" \
    --version "${version}"

  local version_dir="${GITHUB_WORKSPACE}/patches/versions/${major}"
  if [ ! -d "${version_dir}" ]; then
    echo "No major-version patch directory exists for Chromium ${major}; common semantic patches only."
    return 0
  fi

  shopt -s nullglob
  local patches=("${version_dir}"/*.patch)
  local patch_file
  for patch_file in "${patches[@]}"; do
    echo "Checking downstream patch ${patch_file}"
    (
      cd "${source_root}"
      patch --batch --forward --dry-run -p1 < "${patch_file}"
      patch --batch --forward -p1 < "${patch_file}"
    )
  done
}

run_i686_compatibility_preflight() {
  local out_dir="${OUT_DIR:?OUT_DIR is required}"
  local source_root="${CHROMIUM_SRC:?CHROMIUM_SRC is required}"

  test -s "${out_dir}/args.gn"
  test -s "${out_dir}/build.ninja"
  grep -Eq 'target_cpu[[:space:]]*=[[:space:]]*"x86"' "${out_dir}/args.gn"
  grep -Eq 'target_os[[:space:]]*=[[:space:]]*"linux"' "${out_dir}/args.gn"

  local sysroot
  sysroot="$(find "${source_root}/build/linux" -maxdepth 1 -type d -name '*_i386-sysroot' -print -quit)"
  if [ -z "${sysroot}" ]; then
    echo "::error::Chromium i386 sysroot was not installed."
    return 1
  fi
  echo "Using i386 sysroot: ${sysroot}"

  echo "Confirming that the generated Ninja graph contains the chrome target."
  ninja -C "${out_dir}" -t query chrome > "${GITHUB_WORKSPACE}/i686-preflight-ninja-query.txt"
  grep -q '^chrome:' "${GITHUB_WORKSPACE}/i686-preflight-ninja-query.txt"
}

validate_i686_chrome_binary() {
  local binary="${1:?Path to chrome binary is required}"
  test -x "${binary}"

  local file_output
  file_output="$(file "${binary}")"
  printf '%s\n' "${file_output}"
  grep -Eq 'ELF 32-bit.*Intel (80386|i386)' <<<"${file_output}"

  local elf_class
  local elf_machine
  elf_class="$(readelf -h "${binary}" | awk -F: '/Class:/ {gsub(/^[[:space:]]+/, "", $2); print $2}')"
  elf_machine="$(readelf -h "${binary}" | awk -F: '/Machine:/ {gsub(/^[[:space:]]+/, "", $2); print $2}')"
  echo "ELF class: ${elf_class}"
  echo "ELF machine: ${elf_machine}"

  test "${elf_class}" = "ELF32"
  grep -q 'Intel 80386' <<<"${elf_machine}"
}
