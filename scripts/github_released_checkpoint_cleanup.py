#!/usr/bin/env python3
"""Delete checkpoint artifacts only after a version has a healthy immutable release."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Sequence

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
CHECKPOINT_NAME_RE = re.compile(r"^chromium-i686-out-stage-([1-9][0-9]?)$")
RUN_TITLE_RE = re.compile(r"^Chromium i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) - stage ([1-9][0-9]?) - attempt ([0-9]+)$")
ACTIVE_RUN_VERSION_RE = re.compile(r"^Chromium i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)")
MAX_STAGE = 50
MAX_ARTIFACTS_PER_STAGE = 1000
PER_PAGE = 100


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointArtifact:
    artifact_id: int
    run_id: int
    stage: int
    size_bytes: int


def run_gh(args: Sequence[str], *, timeout: int = 90, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["gh", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanupError(f"gh {' '.join(args)} timed out after {timeout}s") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "GitHub CLI failure").strip()
        raise CleanupError(f"gh {' '.join(args)} failed: {detail}")
    return result


def parse_object(result: subprocess.CompletedProcess[str], context: str) -> dict[str, object]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CleanupError(f"{context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise CleanupError(f"{context} returned non-object JSON")
    return value


def validate_inputs(repository: str, version: str, default_branch: str, expected_build_sha: str) -> None:
    if not REPOSITORY_RE.fullmatch(repository):
        raise CleanupError(f"invalid repository: {repository!r}")
    if not VERSION_RE.fullmatch(version):
        raise CleanupError(f"invalid Chromium version: {version!r}")
    if not BRANCH_RE.fullmatch(default_branch) or ".." in default_branch:
        raise CleanupError(f"invalid default branch: {default_branch!r}")
    if not SHA1_RE.fullmatch(expected_build_sha):
        raise CleanupError(f"invalid expected build SHA: {expected_build_sha!r}")


def verify_healthy_release(repository: str, version: str, expected_build_sha: str) -> None:
    tag = f"chromium-{version}-linux-i686"
    payload = parse_object(
        run_gh(["release", "view", tag, "--repo", repository, "--json", "isDraft,isPrerelease,assets"]),
        "release state",
    )
    if payload.get("isDraft") is not False or payload.get("isPrerelease") is not False:
        raise CleanupError(f"release {tag} is draft/prerelease; checkpoint cleanup is forbidden")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise CleanupError(f"release {tag} lacks an assets array")
    expected = {
        f"chromium-{version}-linux-i686.tar.xz",
        f"chromium-{version}-linux-i686.tar.xz.sha256",
        f"chromium-{version}-linux-i686-manifest.txt",
    }
    seen: dict[str, int] = {}
    for item in assets:
        if not isinstance(item, dict):
            raise CleanupError(f"release {tag} contains malformed asset metadata")
        name = str(item.get("name", ""))
        if name not in expected:
            continue
        seen[name] = seen.get(name, 0) + 1
        digest = str(item.get("digest", ""))
        if not SHA256_DIGEST_RE.fullmatch(digest):
            raise CleanupError(f"release asset {name} lacks a verifiable SHA-256 digest")
    missing = sorted(name for name in expected if seen.get(name) != 1)
    if missing:
        raise CleanupError(f"release {tag} does not have exactly one of each required asset: {', '.join(missing)}")
    commit = run_gh(["api", f"repos/{repository}/commits/{tag}", "--jq", ".sha"]).stdout.strip()
    if not SHA1_RE.fullmatch(commit):
        raise CleanupError(f"release tag {tag} did not resolve to a 40-hex commit")
    if commit.lower() != expected_build_sha.lower():
        raise CleanupError(
            f"release tag {tag} resolves to {commit}, not validated build {expected_build_sha}; cleanup is forbidden"
        )
    return None


ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}


def ensure_no_active_build_for_version(repository: str, version: str, default_branch: str) -> None:
    payload = parse_object(
        run_gh(
            [
                "api",
                f"repos/{repository}/actions/workflows/chromium-i686.yml/runs?branch={default_branch}&per_page=100",
            ]
        ),
        "active build listing",
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise CleanupError("active build listing lacks a workflow_runs array")
    for run in runs:
        if not isinstance(run, dict):
            raise CleanupError("active build listing contains malformed run metadata")
        status = str(run.get("status", ""))
        if status not in ACTIVE_RUN_STATES:
            continue
        title = str(run.get("display_title", ""))
        match = ACTIVE_RUN_VERSION_RE.match(title)
        if match and match.group(1) == version:
            raise CleanupError(
                f"Chromium {version} still has an active staged build ({status}); deferring checkpoint cleanup"
            )


def list_checkpoint_artifacts(repository: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for stage in range(1, MAX_STAGE + 1):
        name = f"chromium-i686-out-stage-{stage}"
        first = parse_object(
            run_gh(["api", f"repos/{repository}/actions/artifacts?name={name}&per_page={PER_PAGE}&page=1"]),
            f"artifact listing for {name}",
        )
        total = first.get("total_count")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or total > MAX_ARTIFACTS_PER_STAGE
        ):
            raise CleanupError(
                f"checkpoint artifact count for {name} exceeds bounded 0..{MAX_ARTIFACTS_PER_STAGE} contract: {total!r}"
            )
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        stage_values: list[dict[str, object]] = []
        for page in range(1, pages + 1):
            payload = first if page == 1 else parse_object(
                run_gh(["api", f"repos/{repository}/actions/artifacts?name={name}&per_page={PER_PAGE}&page={page}"]),
                f"artifact listing for {name} page {page}",
            )
            raw = payload.get("artifacts")
            if not isinstance(raw, list):
                raise CleanupError(f"artifact listing for {name} page {page} lacks an artifacts array")
            for item in raw:
                if not isinstance(item, dict):
                    raise CleanupError(f"artifact listing for {name} page {page} contains malformed metadata")
                if item.get("name") != name:
                    raise CleanupError(
                        f"artifact name filter for {name} returned unexpected artifact {item.get('name')!r}"
                    )
                stage_values.append(item)
        if len(stage_values) != total:
            raise CleanupError(
                f"artifact pagination for {name} returned {len(stage_values)} of {total} advertised artifacts"
            )
        values.extend(stage_values)
    return values


def _run_matches_checkpoint(
    repository: str,
    run_id: int,
    version: str,
    stage: int,
    default_branch: str,
    cache: dict[int, dict[str, object]],
) -> bool:
    run = cache.get(run_id)
    if run is None:
        run = parse_object(run_gh(["api", f"repos/{repository}/actions/runs/{run_id}"]), f"run {run_id}")
        cache[run_id] = run
    workflow_path = str(run.get("path", "")).split("@", 1)[0]
    head_repo = (run.get("head_repository") or {}).get("full_name") if isinstance(run.get("head_repository"), dict) else None
    if workflow_path != ".github/workflows/chromium-i686.yml" or head_repo != repository:
        return False
    if run.get("head_branch") != default_branch or run.get("event") != "workflow_dispatch":
        return False
    if run.get("status") != "completed":
        return False
    match = RUN_TITLE_RE.fullmatch(str(run.get("display_title", "")))
    if not match:
        return False
    return match.group(1) == version and int(match.group(2)) == stage


def find_version_checkpoints(repository: str, version: str, default_branch: str) -> list[CheckpointArtifact]:
    run_cache: dict[int, dict[str, object]] = {}
    found: list[CheckpointArtifact] = []
    for item in list_checkpoint_artifacts(repository):
        name = str(item.get("name", ""))
        match = CHECKPOINT_NAME_RE.fullmatch(name)
        if not match:
            continue
        expired = item.get("expired")
        if expired is True:
            continue
        if expired is not False:
            raise CleanupError(f"checkpoint artifact {name} has malformed expired metadata: {expired!r}")
        artifact_id = str(item.get("id", ""))
        if not ID_RE.fullmatch(artifact_id):
            raise CleanupError(f"checkpoint artifact {name} has invalid id: {artifact_id!r}")
        workflow_run = item.get("workflow_run")
        run_id = str(workflow_run.get("id", "")) if isinstance(workflow_run, dict) else ""
        if not ID_RE.fullmatch(run_id):
            # Never delete an artifact whose producer cannot be proven.
            continue
        size = item.get("size_in_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise CleanupError(f"checkpoint artifact {artifact_id} has invalid size metadata: {size!r}")
        stage = int(match.group(1))
        if stage > 50:
            continue
        if _run_matches_checkpoint(repository, int(run_id), version, stage, default_branch, run_cache):
            found.append(CheckpointArtifact(int(artifact_id), int(run_id), stage, size))
    found.sort(key=lambda x: (x.stage, x.run_id, x.artifact_id))
    return found


def artifact_is_missing(repository: str, artifact_id: int) -> bool:
    result = run_gh(["api", f"repos/{repository}/actions/artifacts/{artifact_id}"], check=False)
    if result.returncode == 0:
        return False
    detail = f"{result.stdout}\n{result.stderr}"
    if "HTTP 404" in detail or "Not Found" in detail:
        return True
    raise CleanupError(f"could not confirm checkpoint artifact {artifact_id} state: {detail.strip()}")


def delete_checkpoint(repository: str, artifact: CheckpointArtifact) -> str:
    result = run_gh(
        ["api", "--method", "DELETE", f"repos/{repository}/actions/artifacts/{artifact.artifact_id}"],
        timeout=120,
        check=False,
    )
    if result.returncode != 0 and not artifact_is_missing(repository, artifact.artifact_id):
        detail = (result.stderr or result.stdout or "GitHub CLI failure").strip()
        raise CleanupError(f"checkpoint delete failed for {artifact.artifact_id}: {detail}")
    if not artifact_is_missing(repository, artifact.artifact_id):
        raise CleanupError(f"checkpoint artifact {artifact.artifact_id} is still visible after DELETE")
    return f"deleted:{artifact.artifact_id}"


def cleanup_released_version(
    repository: str, version: str, default_branch: str, expected_build_sha: str, *, dry_run: bool
) -> tuple[list[str], int]:
    validate_inputs(repository, version, default_branch, expected_build_sha)
    verify_healthy_release(repository, version, expected_build_sha)
    ensure_no_active_build_for_version(repository, version, default_branch)
    candidates = find_version_checkpoints(repository, version, default_branch)
    total_bytes = sum(item.size_bytes for item in candidates)
    results: list[str] = []
    for item in candidates:
        if dry_run:
            line = f"dry-run:{item.artifact_id}:run={item.run_id}:stage={item.stage}:bytes={item.size_bytes}"
        else:
            line = delete_checkpoint(repository, item)
        print(line, flush=True)
        results.append(line)
    return results, total_bytes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--expected-build-sha", required=True)
    parser.add_argument("--apply", action="store_true", help="Actually delete; default is dry-run")
    args = parser.parse_args()
    try:
        results, total_bytes = cleanup_released_version(
            args.repository,
            args.version,
            args.default_branch,
            args.expected_build_sha,
            dry_run=not args.apply,
        )
    except CleanupError as exc:
        parser.error(str(exc))
    print(f"checkpoint_count={len(results)}")
    print(f"checkpoint_bytes={total_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
