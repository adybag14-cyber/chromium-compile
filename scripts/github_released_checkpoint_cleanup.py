#!/usr/bin/env python3
"""Delete resumable build state only after a version has a healthy immutable release."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
MAX_SOURCE_CACHES_PER_FILTER = 100
MAX_RELEASE_WORKFLOW_JOBS = 100
PER_PAGE = 100
PUBLISH_WORKFLOW_PATH = ".github/workflows/publish-i686-release.yml"
MAX_RELEASE_PACKAGE_BYTES = 16 * 1024**3
MAX_RELEASE_CHECKSUM_BYTES = 64 * 1024
MAX_RELEASE_MANIFEST_BYTES = 4 * 1024**2
CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})  ([A-Za-z0-9._-]+)$")


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointArtifact:
    artifact_id: int
    run_id: int
    stage: int
    size_bytes: int


@dataclass(frozen=True)
class SourceCache:
    cache_id: int
    key: str
    size_bytes: int


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    digest: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReleaseIdentity:
    tag: str
    tag_commit: str
    published_at: datetime
    assets: tuple[ReleaseAsset, ...]


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


def parse_github_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CleanupError(f"{label} is missing or not an RFC3339 UTC timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CleanupError(f"{label} is not a valid RFC3339 UTC timestamp: {value!r}") from exc
    if parsed.utcoffset() is None:
        raise CleanupError(f"{label} is not timezone-aware: {value!r}")
    return parsed


def _release_asset_limits(version: str) -> dict[str, int]:
    return {
        f"chromium-{version}-linux-i686.tar.xz": MAX_RELEASE_PACKAGE_BYTES,
        f"chromium-{version}-linux-i686.tar.xz.sha256": MAX_RELEASE_CHECKSUM_BYTES,
        f"chromium-{version}-linux-i686-manifest.txt": MAX_RELEASE_MANIFEST_BYTES,
    }


def read_release_identity(repository: str, version: str, expected_build_sha: str) -> ReleaseIdentity:
    tag = f"chromium-{version}-linux-i686"
    payload = parse_object(
        run_gh([
            "release", "view", tag, "--repo", repository,
            "--json", "isDraft,isPrerelease,assets,publishedAt,tagName",
        ]),
        "release state",
    )
    if payload.get("isDraft") is not False or payload.get("isPrerelease") is not False:
        raise CleanupError(f"release {tag} is draft/prerelease; checkpoint cleanup is forbidden")
    if payload.get("tagName") != tag:
        raise CleanupError(f"release lookup returned unexpected tag name: {payload.get('tagName')!r}")
    published_at = parse_github_timestamp(payload.get("publishedAt"), f"release {tag} publishedAt")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise CleanupError(f"release {tag} lacks an assets array")
    limits = _release_asset_limits(version)
    seen: dict[str, ReleaseAsset] = {}
    counts: dict[str, int] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            raise CleanupError(f"release {tag} contains malformed asset metadata")
        name = str(item.get("name", ""))
        if name not in limits:
            continue
        counts[name] = counts.get(name, 0) + 1
        digest = str(item.get("digest", ""))
        if not SHA256_DIGEST_RE.fullmatch(digest):
            raise CleanupError(f"release asset {name} lacks a verifiable SHA-256 digest")
        if item.get("state") != "uploaded":
            raise CleanupError(f"release asset {name} is not in uploaded state: {item.get('state')!r}")
        created_at = parse_github_timestamp(item.get("createdAt"), f"release asset {name} createdAt")
        updated_at = parse_github_timestamp(item.get("updatedAt"), f"release asset {name} updatedAt")
        if updated_at < created_at:
            raise CleanupError(f"release asset {name} updatedAt precedes createdAt")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > limits[name]:
            raise CleanupError(
                f"release asset {name} size violates bounded 1..{limits[name]} byte contract: {size!r}"
            )
        seen[name] = ReleaseAsset(name, digest.lower(), size, created_at, updated_at)
    missing = sorted(name for name in limits if counts.get(name) != 1)
    if missing:
        raise CleanupError(f"release {tag} does not have exactly one of each required asset: {', '.join(missing)}")
    commit = run_gh(["api", f"repos/{repository}/commits/{tag}", "--jq", ".sha"]).stdout.strip()
    if not SHA1_RE.fullmatch(commit):
        raise CleanupError(f"release tag {tag} did not resolve to a 40-hex commit")
    if commit.lower() != expected_build_sha.lower():
        raise CleanupError(
            f"release tag {tag} resolves to {commit}, not validated build {expected_build_sha}; cleanup is forbidden"
        )
    return ReleaseIdentity(
        tag, commit.lower(), published_at, tuple(sorted(seen.values(), key=lambda item: item.name))
    )


def verify_healthy_release(repository: str, version: str, expected_build_sha: str) -> None:
    # Compatibility wrapper used by focused callers/tests. Destructive cleanup uses
    # prepare_release_cleanup_proof(), which additionally verifies the immutable bytes.
    read_release_identity(repository, version, expected_build_sha)
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_release_manifest(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CleanupError(f"could not read release manifest {path.name}: {exc}") from exc
    fields: dict[str, str] = {}
    saw_packaged_files = False
    for raw in text.splitlines():
        if raw == "packaged_files:":
            saw_packaged_files = True
            break
        if not raw:
            continue
        if "=" not in raw:
            raise CleanupError(f"release manifest contains malformed metadata line: {raw!r}")
        key, value = raw.split("=", 1)
        if not key or key in fields:
            raise CleanupError(f"release manifest contains invalid/duplicate key: {key!r}")
        fields[key] = value
    if not saw_packaged_files:
        raise CleanupError("release manifest lacks packaged_files section")
    return fields


def run_release_archive_validator(archive: Path) -> None:
    validator = Path(__file__).with_name("validate_release_archive.py")
    try:
        result = subprocess.run(
            [sys.executable, str(validator), str(archive)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanupError("release archive safety validation timed out after 600s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "release archive validator failed").strip()
        raise CleanupError(f"release archive safety/completeness validation failed: {detail}")


def verify_release_archive_bytes(
    repository: str,
    version: str,
    expected_build_sha: str,
    identity: ReleaseIdentity,
) -> None:
    expected_identity = read_release_identity(repository, version, expected_build_sha)
    if expected_identity != identity:
        raise CleanupError("release identity changed before immutable-byte validation; cleanup is forbidden")
    package_name = f"chromium-{version}-linux-i686.tar.xz"
    checksum_name = f"{package_name}.sha256"
    manifest_name = f"chromium-{version}-linux-i686-manifest.txt"
    by_name = {asset.name: asset for asset in identity.assets}

    with tempfile.TemporaryDirectory(prefix="chromium-release-cleanup-proof-") as temp:
        root = Path(temp)
        # Never derive a local filesystem path from release metadata or CLI input.
        # The three remotely validated asset names are downloaded one-at-a-time into
        # fixed internal filenames; remote names remain data used only by the GitHub CLI.
        package = root / "release-package.tar.xz"
        checksum = root / "release-package.sha256"
        manifest = root / "release-manifest.txt"
        downloads = (
            (package_name, package),
            (checksum_name, checksum),
            (manifest_name, manifest),
        )
        for remote_name, local_path in downloads:
            run_gh(
                [
                    "release",
                    "download",
                    identity.tag,
                    "--repo",
                    repository,
                    "--pattern",
                    remote_name,
                    "--output",
                    str(local_path),
                ],
                timeout=600,
            )
            if not local_path.is_file() or local_path.is_symlink():
                raise CleanupError(
                    f"release download did not materialize a regular {remote_name} asset"
                )
            actual_size = local_path.stat().st_size
            asset = by_name[remote_name]
            if actual_size != asset.size_bytes:
                raise CleanupError(
                    f"release asset {remote_name} size changed: "
                    f"metadata={asset.size_bytes}, downloaded={actual_size}"
                )
            actual_digest = _sha256_file(local_path)
            expected_digest = asset.digest.removeprefix("sha256:")
            if actual_digest.lower() != expected_digest.lower():
                raise CleanupError(
                    f"release asset {remote_name} does not match its GitHub SHA-256 digest"
                )

        run_release_archive_validator(package)
        package_sha = _sha256_file(package)
        try:
            checksum_text = checksum.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise CleanupError(f"could not read release checksum sidecar: {exc}") from exc
        checksum_match = CHECKSUM_LINE_RE.fullmatch(checksum_text)
        if not checksum_match or checksum_match.group(2) != package_name:
            raise CleanupError("release checksum sidecar does not name exactly the expected package")
        if checksum_match.group(1).lower() != package_sha.lower():
            raise CleanupError("release checksum sidecar does not match the immutable package bytes")



        fields = _parse_release_manifest(manifest)
        required_fields = {
            "manifest_schema": "2",
            "version": version,
            "target_cpu": "x86",
            "target_os": "linux",
            "package_sha256": package_sha,
            "github_sha": expected_build_sha.lower(),
        }
        for key, expected in required_fields.items():
            actual = fields.get(key, "")
            if key in {"package_sha256", "github_sha"}:
                actual = actual.lower()
            if actual != expected:
                raise CleanupError(
                    f"release manifest {key} does not match cleanup proof: expected {expected!r}, got {fields.get(key)!r}"
                )



def _normalized_workflow_path(value: object) -> str:
    return str(value or "").split("@", 1)[0]


def verify_release_workflow_proof(
    repository: str,
    version: str,
    default_branch: str,
    release_workflow_run_id: str,
    identity: ReleaseIdentity,
) -> None:
    if not ID_RE.fullmatch(release_workflow_run_id):
        raise CleanupError(f"invalid release workflow run ID: {release_workflow_run_id!r}")
    run = parse_object(
        run_gh(["api", f"repos/{repository}/actions/runs/{release_workflow_run_id}"]),
        "release workflow run",
    )
    if _normalized_workflow_path(run.get("path")) != PUBLISH_WORKFLOW_PATH:
        raise CleanupError("release workflow proof does not come from the trusted publication workflow")
    head_repository = run.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != repository:
        raise CleanupError("release workflow proof comes from an unexpected repository")
    if run.get("head_branch") != default_branch:
        raise CleanupError("release workflow proof does not run on the default branch")
    if run.get("event") not in {"workflow_run", "workflow_dispatch"}:
        raise CleanupError(f"release workflow proof has unexpected event: {run.get('event')!r}")
    title = str(run.get("display_title", ""))
    if not title.startswith(f"Publish Chromium i686 {version} "):
        raise CleanupError(f"release workflow proof title is not scoped to Chromium {version}: {title!r}")
    if run.get("status") not in {"in_progress", "completed"}:
        raise CleanupError(f"release workflow proof has unexpected status: {run.get('status')!r}")

    jobs = parse_object(
        run_gh(["api", f"repos/{repository}/actions/runs/{release_workflow_run_id}/jobs?per_page=100&page=1"]),
        "release workflow jobs",
    )
    total = jobs.get("total_count")
    raw_jobs = jobs.get("jobs")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 3
        or total > MAX_RELEASE_WORKFLOW_JOBS
        or not isinstance(raw_jobs, list)
        or len(raw_jobs) != total
    ):
        raise CleanupError("release workflow job listing violates bounded complete-list contract")
    required = {"validate", "smoke", "publish"}
    selected: dict[str, dict[str, object]] = {}
    counts: dict[str, int] = {}
    for job in raw_jobs:
        if not isinstance(job, dict):
            raise CleanupError("release workflow job listing contains malformed metadata")
        name = str(job.get("name", ""))
        if name not in required:
            continue
        counts[name] = counts.get(name, 0) + 1
        selected[name] = job
    for name in sorted(required):
        job = selected.get(name)
        if (
            counts.get(name) != 1
            or job is None
            or job.get("status") != "completed"
            or job.get("conclusion") != "success"
        ):
            raise CleanupError(f"release workflow job {name!r} is not uniquely completed/successful")

    publish_job = selected["publish"]
    raw_steps = publish_job.get("steps")
    if not isinstance(raw_steps, list):
        raise CleanupError("publish job lacks step metadata for release-byte provenance")
    publish_step_name = "Create or resume transactional immutable release"
    publish_steps = [step for step in raw_steps if isinstance(step, dict) and step.get("name") == publish_step_name]
    if len(publish_steps) != 1:
        raise CleanupError(f"publish job does not contain exactly one {publish_step_name!r} step")
    publish_step = publish_steps[0]
    if publish_step.get("status") != "completed" or publish_step.get("conclusion") != "success":
        raise CleanupError("transactional release publication step did not complete successfully")
    step_started = parse_github_timestamp(publish_step.get("started_at"), "publish step started_at")
    step_completed = parse_github_timestamp(publish_step.get("completed_at"), "publish step completed_at")
    if step_completed < step_started:
        raise CleanupError("publish step completion precedes its start")
    # A transactional retry may resume a release object created by an earlier failed run.
    # The current assets must still be created/last-updated by this exact successful publish step.
    if identity.published_at > step_completed:
        raise CleanupError("current release publishedAt postdates the supplied successful publish step")
    for asset in identity.assets:
        if not (step_started <= asset.created_at <= asset.updated_at <= step_completed):
            raise CleanupError(
                f"release asset {asset.name} creation/update timestamps are not bound to the supplied publish step"
            )


def prepare_release_cleanup_proof(
    repository: str,
    version: str,
    default_branch: str,
    expected_build_sha: str,
    release_workflow_run_id: str | None,
    *,
    require_runtime_proof: bool,
) -> ReleaseIdentity:
    validate_inputs(repository, version, default_branch, expected_build_sha)
    identity = read_release_identity(repository, version, expected_build_sha)
    verify_release_archive_bytes(repository, version, expected_build_sha, identity)
    if require_runtime_proof:
        if not release_workflow_run_id:
            raise CleanupError("--apply requires a trusted release workflow run ID with successful validate/smoke/publish jobs")
        verify_release_workflow_proof(
            repository, version, default_branch, release_workflow_run_id, identity
        )
    elif release_workflow_run_id:
        verify_release_workflow_proof(
            repository, version, default_branch, release_workflow_run_id, identity
        )
    if read_release_identity(repository, version, expected_build_sha) != identity:
        raise CleanupError("release identity changed while cleanup proof was running; cleanup is forbidden")
    return identity


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


def revalidate_release_identity(
    repository: str, version: str, expected_build_sha: str, identity: ReleaseIdentity
) -> None:
    if read_release_identity(repository, version, expected_build_sha) != identity:
        raise CleanupError("release identity changed after immutable-byte proof; cleanup is forbidden")


def cleanup_released_version(
    repository: str,
    version: str,
    default_branch: str,
    expected_build_sha: str,
    *,
    dry_run: bool,
    release_identity: ReleaseIdentity | None = None,
) -> tuple[list[str], int]:
    validate_inputs(repository, version, default_branch, expected_build_sha)
    if release_identity is None:
        if not dry_run:
            raise CleanupError("destructive checkpoint cleanup requires a verified immutable release proof")
        verify_healthy_release(repository, version, expected_build_sha)
    else:
        revalidate_release_identity(repository, version, expected_build_sha, release_identity)
    ensure_no_active_build_for_version(repository, version, default_branch)
    candidates = find_version_checkpoints(repository, version, default_branch)
    total_bytes = sum(item.size_bytes for item in candidates)
    if not dry_run:
        assert release_identity is not None
        revalidate_release_identity(repository, version, expected_build_sha, release_identity)
        ensure_no_active_build_for_version(repository, version, default_branch)
    results: list[str] = []
    for item in candidates:
        if dry_run:
            line = f"dry-run:{item.artifact_id}:run={item.run_id}:stage={item.stage}:bytes={item.size_bytes}"
        else:
            line = delete_checkpoint(repository, item)
        print(line, flush=True)
        results.append(line)
    return results, total_bytes



def source_cache_filters(version: str) -> list[tuple[str, re.Pattern[str]]]:
    escaped = re.escape(version)
    return [
        (f"chromium-src-v4-{version}-", re.compile(rf"^chromium-src-v4-{escaped}-[1-9][0-9]{{0,39}}$")),
        (f"chromium-src-v3-{version}", re.compile(rf"^chromium-src-v3-{escaped}$")),
        (f"chromium-src-v2-{version}", re.compile(rf"^chromium-src-v2-{escaped}$")),
        (f"chromium-src-{version}", re.compile(rf"^chromium-src-{escaped}$")),
    ]


def list_source_caches(repository: str, version: str, default_branch: str) -> list[SourceCache]:
    expected_ref = f"refs/heads/{default_branch}"
    encoded_ref = urllib.parse.quote(expected_ref, safe="")
    found: dict[int, SourceCache] = {}
    for key_filter, key_pattern in source_cache_filters(version):
        encoded_key = urllib.parse.quote(key_filter, safe="")
        payload = parse_object(
            run_gh([
                "api",
                f"repos/{repository}/actions/caches?ref={encoded_ref}&key={encoded_key}&per_page={PER_PAGE}",
            ]),
            f"source cache listing for {key_filter}",
        )
        total = payload.get("total_count")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or total > MAX_SOURCE_CACHES_PER_FILTER
        ):
            raise CleanupError(
                f"source cache count for {key_filter} exceeds bounded 0..{MAX_SOURCE_CACHES_PER_FILTER} contract: {total!r}"
            )
        raw = payload.get("actions_caches")
        if not isinstance(raw, list):
            raise CleanupError(f"source cache listing for {key_filter} lacks an actions_caches array")
        if len(raw) != total:
            raise CleanupError(
                f"source cache listing for {key_filter} returned {len(raw)} of {total} advertised caches"
            )
        for item in raw:
            if not isinstance(item, dict):
                raise CleanupError(f"source cache listing for {key_filter} contains malformed metadata")
            key = str(item.get("key", ""))
            ref = str(item.get("ref", ""))
            if ref != expected_ref or not key_pattern.fullmatch(key):
                raise CleanupError(
                    f"source cache filter {key_filter!r} returned unexpected cache key/ref {key!r} / {ref!r}"
                )
            cache_id = str(item.get("id", ""))
            if not ID_RE.fullmatch(cache_id):
                raise CleanupError(f"source cache {key!r} has invalid id: {cache_id!r}")
            size = item.get("size_in_bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise CleanupError(f"source cache {cache_id} has invalid size metadata: {size!r}")
            numeric_id = int(cache_id)
            candidate = SourceCache(numeric_id, key, size)
            previous = found.get(numeric_id)
            if previous is not None and previous != candidate:
                raise CleanupError(f"source cache id {numeric_id} appeared with conflicting metadata")
            found[numeric_id] = candidate
    return sorted(found.values(), key=lambda item: (item.key, item.cache_id))


def source_cache_is_missing(
    repository: str, version: str, default_branch: str, cache_id: int
) -> bool:
    return all(item.cache_id != cache_id for item in list_source_caches(repository, version, default_branch))


def delete_source_cache(
    repository: str, version: str, default_branch: str, cache: SourceCache
) -> str:
    result = run_gh(
        ["api", "--method", "DELETE", f"repos/{repository}/actions/caches/{cache.cache_id}"],
        timeout=120,
        check=False,
    )
    missing = source_cache_is_missing(repository, version, default_branch, cache.cache_id)
    if result.returncode != 0 and not missing:
        detail = (result.stderr or result.stdout or "GitHub CLI failure").strip()
        raise CleanupError(f"source cache delete failed for {cache.cache_id}: {detail}")
    if not missing:
        raise CleanupError(f"source cache {cache.cache_id} is still visible after DELETE")
    return f"deleted-cache:{cache.cache_id}:key={cache.key}"


def cleanup_released_source_caches(
    repository: str,
    version: str,
    default_branch: str,
    expected_build_sha: str,
    *,
    dry_run: bool,
    release_identity: ReleaseIdentity | None = None,
) -> tuple[list[str], int]:
    # Revalidate independently from checkpoint deletion so a same-version build that
    # starts between the two phases protects its source cache.
    validate_inputs(repository, version, default_branch, expected_build_sha)
    if release_identity is None:
        if not dry_run:
            raise CleanupError("destructive source-cache cleanup requires a verified immutable release proof")
        verify_healthy_release(repository, version, expected_build_sha)
    else:
        revalidate_release_identity(repository, version, expected_build_sha, release_identity)
    ensure_no_active_build_for_version(repository, version, default_branch)
    caches = list_source_caches(repository, version, default_branch)
    total_bytes = sum(item.size_bytes for item in caches)
    if not dry_run:
        assert release_identity is not None
        revalidate_release_identity(repository, version, expected_build_sha, release_identity)
        ensure_no_active_build_for_version(repository, version, default_branch)
    results: list[str] = []
    for item in caches:
        if dry_run:
            line = f"dry-run-cache:{item.cache_id}:key={item.key}:bytes={item.size_bytes}"
        else:
            line = delete_source_cache(repository, version, default_branch, item)
        print(line, flush=True)
        results.append(line)
    return results, total_bytes

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--expected-build-sha", required=True)
    parser.add_argument(
        "--release-workflow-run-id",
        default="",
        help="Trusted publication workflow run; required for --apply runtime proof",
    )
    parser.add_argument("--apply", action="store_true", help="Actually delete; default is dry-run")
    args = parser.parse_args()
    try:
        release_identity = prepare_release_cleanup_proof(
            args.repository,
            args.version,
            args.default_branch,
            args.expected_build_sha,
            args.release_workflow_run_id or None,
            require_runtime_proof=args.apply,
        )
        results, total_bytes = cleanup_released_version(
            args.repository,
            args.version,
            args.default_branch,
            args.expected_build_sha,
            dry_run=not args.apply,
            release_identity=release_identity,
        )
        cache_results, cache_bytes = cleanup_released_source_caches(
            args.repository,
            args.version,
            args.default_branch,
            args.expected_build_sha,
            dry_run=not args.apply,
            release_identity=release_identity,
        )
    except CleanupError as exc:
        parser.error(str(exc))
    print(f"checkpoint_count={len(results)}")
    print(f"checkpoint_bytes={total_bytes}")
    print(f"source_cache_count={len(cache_results)}")
    print(f"source_cache_bytes={cache_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
