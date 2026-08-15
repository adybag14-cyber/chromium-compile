#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"
source "${ROOT}/.github/scripts/chromium_i686_resume.sh"

version=151.0.7922.108
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
mkdir -p "${tmp}/.github/scripts"
cp -R "${ROOT}/patches" "${tmp}/patches"
cp "${ROOT}/.github/scripts/chromium_i686_port.sh" "${tmp}/.github/scripts/chromium_i686_port.sh"
export GITHUB_WORKSPACE="${tmp}"

semantic_before="$(compute_port_config_sha256 "${version}")"
legacy_before="$(compute_legacy_port_config_sha256 "${version}")"
[[ "${semantic_before}" =~ ^[0-9a-f]{64}$ ]]
[[ "${legacy_before}" =~ ^[0-9a-f]{64}$ ]]
[ "${PORT_CONFIG_HASH_SCHEMA}" = 2 ]

# Prove the compatibility function exactly reproduces the previous algorithm,
# including the lexicographic ordering of the port wrapper and patch files.
major="${version%%.*}"
manual_legacy="$({
  chromium_i686_gn_args
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
} | sha256sum | awk '{print $1}')"
[ "${legacy_before}" = "${manual_legacy}" ]

legacy_manifest="${tmp}/legacy.json"
semantic_manifest="${tmp}/semantic.json"
printf '{"port_config_sha256":"%s"}\n' "${legacy_before}" > "${legacy_manifest}"
printf '{"port_config_hash_schema":2,"port_config_sha256":"%s"}\n' "${semantic_before}" > "${semantic_manifest}"
checkpoint_port_config_is_compatible "${legacy_manifest}" "${version}" >/dev/null
checkpoint_port_config_is_compatible "${semantic_manifest}" "${version}" >/dev/null

# Operational/error-reporting edits to the wrapper do not alter build-semantic
# state, but they intentionally alter the legacy hash.
printf '\n# operational-only test edit\n' >> "${GITHUB_WORKSPACE}/.github/scripts/chromium_i686_port.sh"
semantic_after_wrapper="$(compute_port_config_sha256 "${version}")"
legacy_after_wrapper="$(compute_legacy_port_config_sha256 "${version}")"
[ "${semantic_after_wrapper}" = "${semantic_before}" ]
[ "${legacy_after_wrapper}" != "${legacy_before}" ]
checkpoint_port_config_is_compatible "${semantic_manifest}" "${version}" >/dev/null
set +e
checkpoint_port_config_is_compatible "${legacy_manifest}" "${version}" >/dev/null 2>&1
legacy_status=$?
set -e
[ "${legacy_status}" -ne 0 ]

# A semantic patch change must invalidate schema 2.
printf '\n# semantic patch test edit\n' >> "${GITHUB_WORKSPACE}/patches/common/enable_linux_i686.py"
semantic_after_patch="$(compute_port_config_sha256 "${version}")"
[ "${semantic_after_patch}" != "${semantic_before}" ]
set +e
checkpoint_port_config_is_compatible "${semantic_manifest}" "${version}" >/dev/null 2>&1
patch_status=$?
set -e
[ "${patch_status}" -ne 0 ]

# Canonical GN arguments are semantic input too.
cp -R "${ROOT}/patches/." "${GITHUB_WORKSPACE}/patches/"
semantic_reset="$(compute_port_config_sha256 "${version}")"
[ "${semantic_reset}" = "${semantic_before}" ]
chromium_i686_gn_args() {
  printf '%s\n' 'target_os="linux"' 'target_cpu="x86"' 'is_debug=true'
}
semantic_after_gn="$(compute_port_config_sha256 "${version}")"
[ "${semantic_after_gn}" != "${semantic_before}" ]

echo "port configuration hash schema contract tests passed"
