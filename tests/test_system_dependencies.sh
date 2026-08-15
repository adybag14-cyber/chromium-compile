#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

MODE=success
INSTALL_CALLS=0
bounded_sudo_apt_get() {
  if [ "${1:-}" = update ]; then
    [ "${MODE}" != update_fail ]
    return
  fi
  INSTALL_CALLS=$((INSTALL_CALLS + 1))
  [ "${MODE}" != install_fail ]
}
bounded_apt_get_simulate() {
  [ "${MODE}" != simulate_fail ]
}

MODE=update_fail
set +e
install_system_dependencies >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "${SYSTEM_DEPENDENCY_FAILURE_CLASS}" = infrastructure ]
[ "${INSTALL_CALLS}" -eq 0 ]

MODE=simulate_fail
set +e
install_system_dependencies >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "${SYSTEM_DEPENDENCY_FAILURE_CLASS}" = deterministic_build ]
[ "${INSTALL_CALLS}" -eq 0 ]

MODE=install_fail
set +e
install_system_dependencies >/dev/null 2>&1
status=$?
set -e
[ "${status}" -ne 0 ]
[ "${SYSTEM_DEPENDENCY_FAILURE_CLASS}" = infrastructure ]
[ "${INSTALL_CALLS}" -eq 1 ]

MODE=success
install_system_dependencies >/dev/null
[ -z "${SYSTEM_DEPENDENCY_FAILURE_CLASS}" ]
[ "${INSTALL_CALLS}" -eq 2 ]

echo "native system dependency classification tests passed"
