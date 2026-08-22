#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
export RUNNER_TEMP="$(mktemp -d)"
trap 'rm -rf "${RUNNER_TEMP}"' EXIT
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

rm_calls=0
apt_mutation_calls=0
timeout_log="${RUNNER_TEMP}/timeout.log"
bounded_sudo_rm_rf() { rm_calls=$((rm_calls + 1)); return 0; }
bounded_sudo_apt_get() { apt_mutation_calls=$((apt_mutation_calls + 1)); return 99; }
bounded_sudo_apt_install_prefetched() { apt_mutation_calls=$((apt_mutation_calls + 1)); return 99; }
ensure_swap() { return 0; }
timeout() { printf '%s\n' "$*" >> "${timeout_log}"; return 0; }

maximize_runner_disk_space >/dev/null
[ "${rm_calls}" -eq 4 ]
[ "${apt_mutation_calls}" -eq 0 ]
grep -q 'sudo apt-get clean' "${timeout_log}"
if grep -Eq 'purge|autoremove' "${timeout_log}"; then
  echo "runner cleanup attempted package-manager mutation" >&2
  exit 1
fi

echo "non-mutating runner cleanup contract tests passed"
