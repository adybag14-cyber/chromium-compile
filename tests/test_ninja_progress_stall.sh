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
for value in 30 90 180; do
  validate_ninja_stall_minutes "${value}" >/dev/null
done
for bad in 0 1 29 181 999 abc -1; do
  if validate_ninja_stall_minutes "${bad}" >/dev/null 2>&1; then
    echo "expected invalid Ninja stall policy ${bad} to fail" >&2
    exit 1
  fi
done

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

# Setup-only slices must not count as compiler stalls. Simulate arriving at the
# five-minute reserve before autoninja is entered and preserve the prior streak.
CHROMIUM_SRC="${RUNNER_TEMP}/chromium-source"
OUT_DIR="${CHROMIUM_SRC}/out/Release_x86"
BUILD_LOG="${RUNNER_TEMP}/build-stage.log"
mkdir -p "${OUT_DIR}"
: > "${BUILD_LOG}"
ensure_build_disk_space() { return 0; }
autoninja() {
  echo "autoninja must not run in setup-only slice" >&2
  return 99
}
JOB_CHECKPOINT_MINUTES=340
now="$(date +%s)"
JOB_STARTED_AT=$((now - JOB_CHECKPOINT_MINUTES * 60 + 299))
CHROMIUM_I686_PRIOR_NO_PROGRESS_STREAK=1
prep_output="${RUNNER_TEMP}/prep-only-outputs"
: > "${prep_output}"
run_build_until_checkpoint "${prep_output}" >/dev/null
grep -qx 'complete=false' "${prep_output}"
grep -qx 'no_progress_streak=1' "${prep_output}"
grep -qx 'failure_class=' "${prep_output}"

# An intra-slice stall should checkpoint/rotate early, while a second consecutive
# no-progress stall becomes deterministic. Stub only the watchdog process; the
# run_build_until_checkpoint control flow remains real.
JOB_STARTED_AT="$(date +%s)"
JOB_CHECKPOINT_MINUTES=30
CHROMIUM_I686_NINJA_STALL_MINUTES=30
WORKSPACE="${RUNNER_TEMP}/stall-workspace"
CHROMIUM_SRC="${RUNNER_TEMP}/stall-source"
OUT_DIR="${CHROMIUM_SRC}/out/Release_x86"
BUILD_LOG="${RUNNER_TEMP}/stall-build.log"
mkdir -p "${WORKSPACE}" "${OUT_DIR}"
ensure_build_disk_space() { return 0; }
df() { :; }
ccache() { :; }
python3() {
  case " $* " in
    *" --stall-seconds "*" --timeout-seconds "*) ;;
    *) return 2 ;;
  esac
  printf 'stalled\n' > "${CHROMIUM_SRC}/.ninja-stall-watchdog.marker"
  return 86
}

first_stall="${RUNNER_TEMP}/first-stall-output"
: > "${first_stall}"
CHROMIUM_I686_PRIOR_NO_PROGRESS_STREAK=0
run_build_until_checkpoint "${first_stall}" >/dev/null
grep -qx 'complete=false' "${first_stall}"
grep -qx 'no_progress_streak=1' "${first_stall}"
grep -qx 'failure_class=' "${first_stall}"

second_stall="${RUNNER_TEMP}/second-stall-output"
: > "${second_stall}"
CHROMIUM_I686_PRIOR_NO_PROGRESS_STREAK=1
if run_build_until_checkpoint "${second_stall}" >/dev/null 2>&1; then
  second_status=0
else
  second_status=$?
fi
[ "${second_status}" -ne 0 ]
grep -qx 'complete=false' "${second_stall}"
grep -qx 'no_progress_streak=2' "${second_stall}"
grep -qx 'failure_class=deterministic_build' "${second_stall}"

echo "Ninja progress stall contract tests passed"
