#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
export GITHUB_REPOSITORY="owner/repo"
export GITHUB_REF_NAME="main"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"
source "${ROOT}/.github/scripts/chromium_i686_resume.sh"

RUN_JSON='{"path":".github/workflows/chromium-i686.yml","head_repository":{"full_name":"owner/repo"},"head_branch":"main","display_title":"Chromium i686 151.0.7922.108 - stage 2 - attempt 0"}'
ARTIFACTS_JSON='{"total_count":1,"artifacts":[{"name":"chromium-i686-out-stage-2","expired":false}]}'
GH_MODE=ok
bounded_gh() {
  if [ "${GH_MODE}" = fail ]; then return 1; fi
  case "$*" in
    *'/artifacts?per_page=100'*) printf '%s
' "${ARTIFACTS_JSON}" ;;
    *) printf '%s
' "${RUN_JSON}" ;;
  esac
}

validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null
[ -z "${CHECKPOINT_PROVENANCE_FAILURE_CLASS}" ]

RUN_JSON='{"path":".github/workflows/other.yml","head_repository":{"full_name":"owner/repo"},"head_branch":"main","display_title":"Chromium i686 151.0.7922.108 - stage 2 - attempt 0"}'
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]
[ "${CHECKPOINT_PROVENANCE_FAILURE_CLASS}" = deterministic_build ]

RUN_JSON='{"path":".github/workflows/chromium-i686.yml","head_repository":{"full_name":"owner/repo"},"head_branch":"feature","display_title":"Chromium i686 151.0.7922.108 - stage 2 - attempt 0"}'
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]

RUN_JSON='{"path":".github/workflows/chromium-i686.yml","head_repository":{"full_name":"owner/repo"},"head_branch":"main","display_title":"Chromium i686 151.0.7922.108 - stage 2 - attempt 0"}'
ARTIFACTS_JSON='{"total_count":2,"artifacts":[{"name":"chromium-i686-out-stage-2","expired":false},{"name":"chromium-i686-out-stage-2","expired":false}]}'
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]

ARTIFACTS_JSON='{"total_count":1,"artifacts":[{"name":"chromium-i686-out-stage-2","expired":false}]}'
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 4 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]

GH_MODE=fail
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]
[ "${CHECKPOINT_PROVENANCE_FAILURE_CLASS}" = infrastructure ]

GH_MODE=ok
RUN_JSON='{}'
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]
[ "${CHECKPOINT_PROVENANCE_FAILURE_CLASS}" = infrastructure ]

RUN_JSON='{"path":".github/workflows/chromium-i686.yml","head_repository":{"full_name":"owner/repo"},"head_branch":"main","display_title":"Chromium i686 151.0.7922.108 - stage 2 - attempt 0"}'
ARTIFACTS_JSON='{"total_count":"not-a-number","artifacts":[]}'
set +e
validate_checkpoint_source_run 12345 151.0.7922.108 3 chromium-i686-out-stage-2 >/dev/null 2>&1
status=$?
set -e
[ "${status}" -eq 1 ]
[ "${CHECKPOINT_PROVENANCE_FAILURE_CLASS}" = infrastructure ]

echo "checkpoint provenance contract tests passed"
