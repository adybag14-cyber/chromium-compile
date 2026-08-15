#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GITHUB_WORKSPACE="${ROOT}"
source "${ROOT}/.github/scripts/chromium_i686_common.sh"

validate_effective_https_host "https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.108/DEPS?format=TEXT" chromium.googlesource.com
validate_effective_https_host "https://commondatastorage.googleapis.com/chromium-browser-official/chromium-151.0.7922.108.tar.xz" commondatastorage.googleapis.com

for pair in \
  "http://chromium.googlesource.com/x|chromium.googlesource.com" \
  "https://evil.invalid/x|chromium.googlesource.com" \
  "https://user@chromium.googlesource.com/x|chromium.googlesource.com" \
  "https://chromium.googlesource.com:444/x|chromium.googlesource.com"; do
  url="${pair%%|*}"
  host="${pair#*|}"
  if validate_effective_https_host "${url}" "${host}" >/dev/null 2>&1; then
    echo "FAIL: accepted untrusted effective URL ${url}" >&2
    exit 1
  fi
done

echo "source trust effective-host contract tests passed"
