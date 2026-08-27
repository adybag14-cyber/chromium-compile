#!/usr/bin/env python3
"""Safely prune a provenance-checked superseded Chromium checkpoint artifact."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Sequence

RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
TITLE_RES = {
    "linux": re.compile(r"^Chromium i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) - stage ([1-9][0-9]?) - attempt ([0-9]+)$"),
    "windows": re.compile(r"^Chromium Windows i686 ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) - stage ([1-9][0-9]?) - attempt ([0-9]+)$"),
}
WORKFLOW_PATHS = {
    "linux": ".github/workflows/chromium-i686.yml",
    "windows": ".github/workflows/chromium-windows-i686.yml",
}
ARTIFACT_PREFIXES = {
    "linux": "chromium-i686-out-stage-",
    "windows": "chromium-windows-i686-out-stage-",
}


class PruneError(RuntimeError):
    pass


def run_gh(args: Sequence[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise PruneError(f"gh {' '.join(args)} timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "GitHub CLI failure").strip()
        raise PruneError(f"gh {' '.join(args)} failed: {detail}") from exc


def _json(result: subprocess.CompletedProcess[str], context: str) -> dict[str, object]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PruneError(f"{context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise PruneError(f"{context} returned non-object JSON")
    return value

def _bounded_stage(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]?", value):
        raise PruneError(f"expected_stage must be an integer from 1 through 50: {value!r}")
    stage = int(value)
    if stage > 50:
        raise PruneError(f"expected_stage exceeds hard maximum 50: {stage}")
    return stage

def resolve_checkpoint_artifact(
    repository: str, run_id: str, version: str, expected_stage: str, default_branch: str,
    *, lane: str = "linux"
) -> int | None:
    if lane not in TITLE_RES:
        raise PruneError(f"unsupported checkpoint lane: {lane!r}")
    if not REPOSITORY_RE.fullmatch(repository):
        raise PruneError(f"invalid repository: {repository!r}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise PruneError(f"invalid Actions run id: {run_id!r}")
    if not VERSION_RE.fullmatch(version):
        raise PruneError(f"invalid Chromium version: {version!r}")
    stage = _bounded_stage(expected_stage)
    if not BRANCH_RE.fullmatch(default_branch) or ".." in default_branch:
        raise PruneError(f"invalid default branch: {default_branch!r}")

    run = _json(run_gh(["api", f"repos/{repository}/actions/runs/{run_id}"]), "run provenance")
    if run.get("status") != "completed":
        raise PruneError(f"run {run_id} is not completed and cannot be pruned safely")
    actor = run.get("actor")
    actor_login = actor.get("login") if isinstance(actor, dict) else None
    if actor_login != "github-actions[bot]":
        raise PruneError(f"run {run_id} was created by {actor_login!r}, not github-actions[bot]")
    workflow_path = str(run.get("path", "")).split("@", 1)[0]
    if workflow_path != WORKFLOW_PATHS[lane]:
        raise PruneError(f"run {run_id} is not the trusted {lane} i686 build workflow")
    head_repo = (run.get("head_repository") or {}).get("full_name") if isinstance(run.get("head_repository"), dict) else None
    if head_repo != repository:
        raise PruneError(f"run {run_id} originated from {head_repo!r}, not {repository}")
    if run.get("head_branch") != default_branch:
        raise PruneError(f"run {run_id} is from branch {run.get('head_branch')!r}, not {default_branch!r}")
    if run.get("event") != "workflow_dispatch":
        raise PruneError(f"run {run_id} was not created by workflow_dispatch")
    title = str(run.get("display_title", ""))
    match = TITLE_RES[lane].fullmatch(title)
    if not match or match.group(1) != version or int(match.group(2)) != stage:
        raise PruneError(f"run {run_id} title does not match Chromium {version} stage {stage}: {title!r}")

    artifacts = _json(
        run_gh(["api", f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"]),
        "artifact listing",
    )
    total = artifacts.get("total_count")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or total > 100:
        raise PruneError(f"run {run_id} artifact listing exceeds bounded 0..100 contract: {total!r}")
    expected_name = f"{ARTIFACT_PREFIXES[lane]}{stage}"
    values = artifacts.get("artifacts")
    if not isinstance(values, list):
        raise PruneError("artifact listing lacks an artifacts array")
    matches = [item for item in values if isinstance(item, dict) and item.get("name") == expected_name]
    if not matches:
        return None
    if len(matches) != 1:
        raise PruneError(f"run {run_id} has multiple checkpoint artifacts named {expected_name}")
    artifact = matches[0]
    expired = artifact.get("expired")
    if not isinstance(expired, bool):
        raise PruneError(f"checkpoint artifact has malformed expired metadata: {expired!r}")
    if expired:
        return None
    artifact_id = str(artifact.get("id", ""))
    if not RUN_ID_RE.fullmatch(artifact_id):
        raise PruneError(f"checkpoint artifact has invalid id: {artifact_id!r}")
    return int(artifact_id)

def prune_checkpoint(
    repository: str, run_id: str, version: str, expected_stage: str, default_branch: str, *,
    protect_run_id: str = "", dry_run: bool = False, lane: str = "linux"
) -> str:
    if protect_run_id:
        if not RUN_ID_RE.fullmatch(protect_run_id):
            raise PruneError(f"invalid protected Actions run id: {protect_run_id!r}")
        if run_id == protect_run_id:
            raise PruneError(f"refusing to prune checkpoint from protected current run {run_id}")
    artifact_id = resolve_checkpoint_artifact(
        repository, run_id, version, expected_stage, default_branch, lane=lane
    )
    if artifact_id is None:
        return "already-missing"
    if dry_run:
        return f"dry-run:{artifact_id}"
    try:
        run_gh(["api", "--method", "DELETE", f"repos/{repository}/actions/artifacts/{artifact_id}"], timeout=120)
    except PruneError as exc:
        # DELETE is non-idempotent. Confirm whether GitHub accepted it before surfacing failure.
        if resolve_checkpoint_artifact(
            repository, run_id, version, expected_stage, default_branch, lane=lane
        ) is None:
            return f"deleted-after-client-error:{artifact_id}"
        raise exc
    if resolve_checkpoint_artifact(
        repository, run_id, version, expected_stage, default_branch, lane=lane
    ) is not None:
        raise PruneError(f"checkpoint artifact {artifact_id} is still visible after DELETE")
    return f"deleted:{artifact_id}"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-stage", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--protect-run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lane", choices=tuple(TITLE_RES), default="linux")
    args = parser.parse_args()
    try:
        result = prune_checkpoint(
            args.repository, args.run_id, args.version, args.expected_stage, args.default_branch,
            protect_run_id=args.protect_run_id, dry_run=args.dry_run, lane=args.lane
        )
    except PruneError as exc:
        parser.error(str(exc))
    print(result)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
