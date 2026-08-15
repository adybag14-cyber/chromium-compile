#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
export RUNNER_TEMP="$(mktemp -d)"
trap 'rm -rf "${RUNNER_TEMP}"' EXIT
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

OUT_DIR="${RUNNER_TEMP}/out"
mkdir -p "${OUT_DIR}"

[ "$(ninja_log_entry_count)" = 0 ]
[ "$(compute_no_progress_streak 0 0 0)" = 1 ]
[ "$(compute_no_progress_streak 1 10 10)" = 2 ]
[ "$(compute_no_progress_streak 2 10 9)" = 2 ]
[ "$(compute_no_progress_streak 1 10 11)" = 0 ]
for bad in -1 3 abc 999999999999999999; do
  if validate_no_progress_streak "${bad}" >/dev/null 2>&1; then
    echo "expected invalid no-progress streak ${bad} to fail" >&2
    exit 1
  fi
done

printf '# ninja log v5\n' > "${OUT_DIR}/.ninja_log"
before="$(ninja_log_entry_count)"
printf '1\t2\t0\tobj/example.o\tdeadbeef\n' >> "${OUT_DIR}/.ninja_log"
after="$(ninja_log_entry_count)"
[ "${before}" = 0 ]
[ "${after}" = 1 ]
[ "$(compute_no_progress_streak 1 "${before}" "${after}")" = 0 ]

output="${RUNNER_TEMP}/outputs"
: > "${output}"
current="$(ninja_log_entry_count)"
streak="$(record_ninja_progress_streak "${output}" 0 "${current}")"
[ "${streak}" = 1 ]
grep -qx 'no_progress_streak=1' "${output}"

printf '3\t4\t0\tobj/next.o\tcafebabe\n' >> "${OUT_DIR}/.ninja_log"
: > "${output}"
streak="$(record_ninja_progress_streak "${output}" 1 "${current}")"
[ "${streak}" = 0 ]
grep -qx 'no_progress_streak=0' "${output}"

echo "Ninja progress stall contract tests passed"
