import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "github_released_checkpoint_cleanup.py"
SPEC = importlib.util.spec_from_file_location("github_released_checkpoint_cleanup", PATH)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)

REPO = "owner/repo"
VERSION = "151.0.7922.108"
BRANCH = "main"
BUILD_SHA = "b" * 40


def done(payload=None, *, stdout=None, code=0, stderr=""):
    if stdout is None:
        stdout = json.dumps(payload if payload is not None else {})
    return subprocess.CompletedProcess(["gh"], code, stdout, stderr)


def release_payload(version=VERSION, *, draft=False, prerelease=False, missing_digest=False):
    names = [
        f"chromium-{version}-linux-i686.tar.xz",
        f"chromium-{version}-linux-i686.tar.xz.sha256",
        f"chromium-{version}-linux-i686-manifest.txt",
    ]
    sizes = [123456, 159, 12000]
    assets = []
    for idx, (name, size) in enumerate(zip(names, sizes, strict=True)):
        assets.append({
            "name": name,
            "digest": "" if missing_digest and idx == 0 else "sha256:" + "a" * 64,
            "size": size,
            "state": "uploaded",
            "createdAt": "2026-08-16T10:00:10Z",
            "updatedAt": "2026-08-16T10:00:11Z",
        })
    return {
        "isDraft": draft,
        "isPrerelease": prerelease,
        "tagName": f"chromium-{version}-linux-i686",
        "publishedAt": "2026-08-16T10:00:12Z",
        "assets": assets,
    }


def release_identity(*, published_at="2026-08-16T10:00:12Z", asset_created_at="2026-08-16T10:00:10Z", asset_updated_at="2026-08-16T10:00:11Z"):
    payload = release_payload()
    assets = tuple(
        sorted(
            (
                cleanup.ReleaseAsset(
                    item["name"],
                    item["digest"],
                    item["size"],
                    cleanup.parse_github_timestamp(asset_created_at, "fixture asset created"),
                    cleanup.parse_github_timestamp(asset_updated_at, "fixture asset updated"),
                )
                for item in payload["assets"]
            ),
            key=lambda item: item.name,
        )
    )
    return cleanup.ReleaseIdentity(
        f"chromium-{VERSION}-linux-i686",
        BUILD_SHA,
        cleanup.parse_github_timestamp(published_at, "fixture release published"),
        assets,
    )


def release_workflow_run_payload(**changes):
    payload = {
        "path": ".github/workflows/publish-i686-release.yml",
        "head_repository": {"full_name": REPO},
        "head_branch": BRANCH,
        "event": "workflow_run",
        "status": "in_progress",
        "display_title": f"Publish Chromium i686 {VERSION} - stage 6 - attempt 1",
    }
    payload.update(changes)
    return payload


def release_workflow_jobs_payload(*, smoke_conclusion="success", publish_step_conclusion="success"):
    jobs = [
        {"name": "validate", "status": "completed", "conclusion": "success", "steps": []},
        {"name": "smoke", "status": "completed", "conclusion": smoke_conclusion, "steps": []},
        {
            "name": "publish",
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {
                    "name": "Create or resume transactional immutable release",
                    "status": "completed",
                    "conclusion": publish_step_conclusion,
                    "started_at": "2026-08-16T10:00:00Z",
                    "completed_at": "2026-08-16T10:00:20Z",
                }
            ],
        },
    ]
    return {"total_count": len(jobs), "jobs": jobs}

def artifact(artifact_id, run_id, stage, size=1000, expired=False):
    return {
        "id": artifact_id,
        "name": f"chromium-i686-out-stage-{stage}",
        "size_in_bytes": size,
        "expired": expired,
        "workflow_run": {"id": run_id},
    }



def source_cache(cache_id, key, size=1000, ref="refs/heads/main"):
    return {"id": cache_id, "key": key, "size_in_bytes": size, "ref": ref}

def run_payload(version=VERSION, stage=2, **changes):
    payload = {
        "path": ".github/workflows/chromium-i686.yml",
        "head_repository": {"full_name": REPO},
        "head_branch": BRANCH,
        "event": "workflow_dispatch",
        "status": "completed",
        "display_title": f"Chromium i686 {version} - stage {stage} - attempt 0",
    }
    payload.update(changes)
    return payload


class ReleasedCheckpointCleanupTests(unittest.TestCase):
    def test_release_must_be_published_and_digest_complete(self):
        for payload in [release_payload(draft=True), release_payload(prerelease=True), release_payload(missing_digest=True)]:
            with self.subTest(payload=payload), mock.patch.object(cleanup, "run_gh", return_value=done(payload)), self.assertRaises(cleanup.CleanupError):
                cleanup.verify_healthy_release(REPO, VERSION, BUILD_SHA)

    def test_healthy_release_resolves_tag_commit(self):
        with mock.patch.object(cleanup, "run_gh", side_effect=[done(release_payload()), done(stdout="b" * 40 + "\n")]):
            self.assertIsNone(cleanup.verify_healthy_release(REPO, VERSION, BUILD_SHA))

    def test_release_tag_must_match_validated_build_sha(self):
        with mock.patch.object(cleanup, "run_gh", side_effect=[done(release_payload()), done(stdout="c" * 40 + "\n")]), self.assertRaises(cleanup.CleanupError):
            cleanup.verify_healthy_release(REPO, VERSION, BUILD_SHA)

    def test_release_workflow_proof_is_bound_to_successful_publish_step(self):
        identity = release_identity()
        with mock.patch.object(
            cleanup,
            "run_gh",
            side_effect=[done(release_workflow_run_payload()), done(release_workflow_jobs_payload())],
        ):
            cleanup.verify_release_workflow_proof(REPO, VERSION, BRANCH, "123456", identity)

    def test_release_workflow_proof_allows_resumed_release_object_with_current_assets(self):
        identity = release_identity(
            published_at="2026-08-16T09:00:00Z",
            asset_created_at="2026-08-16T09:30:00Z",
            asset_updated_at="2026-08-16T09:31:00Z",
        )
        with mock.patch.object(
            cleanup,
            "run_gh",
            side_effect=[done(release_workflow_run_payload()), done(release_workflow_jobs_payload())],
        ):
            cleanup.verify_release_workflow_proof(REPO, VERSION, BRANCH, "123456", identity)
    def test_release_workflow_proof_rejects_failed_smoke_and_replaced_assets(self):
        identity = release_identity()
        with mock.patch.object(
            cleanup,
            "run_gh",
            side_effect=[
                done(release_workflow_run_payload()),
                done(release_workflow_jobs_payload(smoke_conclusion="failure")),
            ],
        ), self.assertRaises(cleanup.CleanupError):
            cleanup.verify_release_workflow_proof(REPO, VERSION, BRANCH, "123456", identity)

        replaced = release_identity(asset_updated_at="2026-08-16T10:01:00Z")
        with mock.patch.object(
            cleanup,
            "run_gh",
            side_effect=[done(release_workflow_run_payload()), done(release_workflow_jobs_payload())],
        ), self.assertRaises(cleanup.CleanupError):
            cleanup.verify_release_workflow_proof(REPO, VERSION, BRANCH, "123456", replaced)

    def test_release_workflow_proof_rejects_failed_publish_step(self):
        identity = release_identity()
        with mock.patch.object(
            cleanup,
            "run_gh",
            side_effect=[
                done(release_workflow_run_payload()),
                done(release_workflow_jobs_payload(publish_step_conclusion="failure")),
            ],
        ), self.assertRaisesRegex(cleanup.CleanupError, "publication step did not complete successfully"):
            cleanup.verify_release_workflow_proof(REPO, VERSION, BRANCH, "123456", identity)

    def test_release_archive_bytes_cross_bind_package_checksum_and_manifest(self):
        package_name = f"chromium-{VERSION}-linux-i686.tar.xz"
        checksum_name = f"{package_name}.sha256"
        manifest_name = f"chromium-{VERSION}-linux-i686-manifest.txt"
        package_bytes = b"synthetic-package-bytes"
        package_sha = hashlib.sha256(package_bytes).hexdigest()
        checksum_bytes = f"{package_sha}  {package_name}\n".encode()
        manifest_bytes = (
            "manifest_schema=2\n"
            f"version={VERSION}\n"
            "target_cpu=x86\n"
            "target_os=linux\n"
            f"package_sha256={package_sha}\n"
            f"github_sha={BUILD_SHA}\n"
            "packaged_files:\n"
            "chrome\n"
        ).encode()
        created = cleanup.parse_github_timestamp("2026-08-16T10:00:10Z", "fixture")
        assets = tuple(
            sorted(
                [
                    cleanup.ReleaseAsset(
                        name,
                        "sha256:" + hashlib.sha256(data).hexdigest(),
                        len(data),
                        created,
                        created,
                    )
                    for name, data in [
                        (package_name, package_bytes),
                        (checksum_name, checksum_bytes),
                        (manifest_name, manifest_bytes),
                    ]
                ],
                key=lambda item: item.name,
            )
        )
        identity = cleanup.ReleaseIdentity(
            f"chromium-{VERSION}-linux-i686", BUILD_SHA, created, assets
        )

        asset_bytes = {
            package_name: package_bytes,
            checksum_name: checksum_bytes,
            manifest_name: manifest_bytes,
        }

        download_outputs = []

        def fake_gh(args, **kwargs):
            if args[:2] == ["release", "download"]:
                remote_name = args[args.index("--pattern") + 1]
                output = pathlib.Path(args[args.index("--output") + 1])
                download_outputs.append((remote_name, output, kwargs.get("timeout")))
                output.write_bytes(asset_bytes[remote_name])
                return done()
            raise AssertionError(args)

        with mock.patch.object(cleanup, "read_release_identity", return_value=identity), \
             mock.patch.object(cleanup, "run_gh", side_effect=fake_gh), \
             mock.patch.object(cleanup, "run_release_archive_validator") as validator:
            cleanup.verify_release_archive_bytes(REPO, VERSION, BUILD_SHA, identity)
        validator.assert_called_once()
        self.assertEqual(
            [output.name for _, output, _ in download_outputs],
            ["release-package.tar.xz", "release-package.sha256", "release-manifest.txt"],
        )
        self.assertEqual(len({output.parent for _, output, _ in download_outputs}), 1)
        self.assertTrue(all(output.name != remote for remote, output, _ in download_outputs))
        self.assertEqual([timeout for _, _, timeout in download_outputs], [600, 120, 120])

    def test_release_byte_proof_fails_before_download_when_temp_space_is_insufficient(self):
        identity = release_identity()
        with mock.patch.object(cleanup, "read_release_identity", return_value=identity), \
             mock.patch.object(cleanup.shutil, "disk_usage", return_value=mock.Mock(free=1)), \
             mock.patch.object(cleanup, "run_gh") as gh, \
             self.assertRaisesRegex(cleanup.CleanupError, "insufficient temporary-disk space"):
            cleanup.verify_release_archive_bytes(REPO, VERSION, BUILD_SHA, identity)
        gh.assert_not_called()

    def test_archive_completeness_failure_precedes_legacy_manifest_parsing(self):
        package_name = f"chromium-{VERSION}-linux-i686.tar.xz"
        checksum_name = f"{package_name}.sha256"
        manifest_name = f"chromium-{VERSION}-linux-i686-manifest.txt"
        package_bytes = b"incomplete-runtime-package"
        package_sha = hashlib.sha256(package_bytes).hexdigest()
        checksum_bytes = f"{package_sha}  {package_name}\n".encode()
        manifest_bytes = b"legacy-file-list-without-schema\n"
        created = cleanup.parse_github_timestamp("2026-08-16T10:00:10Z", "fixture")
        assets = tuple(
            sorted(
                [
                    cleanup.ReleaseAsset(name, "sha256:" + hashlib.sha256(data).hexdigest(), len(data), created, created)
                    for name, data in [
                        (package_name, package_bytes),
                        (checksum_name, checksum_bytes),
                        (manifest_name, manifest_bytes),
                    ]
                ],
                key=lambda item: item.name,
            )
        )
        identity = cleanup.ReleaseIdentity(
            f"chromium-{VERSION}-linux-i686", BUILD_SHA, created, assets
        )

        asset_bytes = {
            package_name: package_bytes,
            checksum_name: checksum_bytes,
            manifest_name: manifest_bytes,
        }

        def fake_gh(args, **kwargs):
            if args[:2] == ["release", "download"]:
                remote_name = args[args.index("--pattern") + 1]
                output = pathlib.Path(args[args.index("--output") + 1])
                output.write_bytes(asset_bytes[remote_name])
                return done()
            raise AssertionError(args)

        with mock.patch.object(cleanup, "read_release_identity", return_value=identity), \
             mock.patch.object(cleanup, "run_gh", side_effect=fake_gh), \
             mock.patch.object(
                 cleanup,
                 "run_release_archive_validator",
                 side_effect=cleanup.CleanupError("archive missing required runtime paths"),
             ), \
             mock.patch.object(cleanup, "_parse_release_manifest") as parse_manifest, \
             self.assertRaisesRegex(cleanup.CleanupError, "missing required runtime paths"):
            cleanup.verify_release_archive_bytes(REPO, VERSION, BUILD_SHA, identity)
        parse_manifest.assert_not_called()
    def test_release_archive_validator_oserror_is_cleanup_error(self):
        archive = ROOT / "missing-release-archive.tar.xz"
        with mock.patch.object(cleanup.subprocess, "run", side_effect=OSError("validator unavailable")), \
             self.assertRaisesRegex(cleanup.CleanupError, "could not run release archive validator"):
            cleanup.run_release_archive_validator(archive)

    def test_apply_without_workflow_proof_fails_before_release_io(self):
        with mock.patch.object(cleanup, "read_release_identity") as identity, \
             mock.patch.object(cleanup, "verify_release_archive_bytes") as byte_proof, \
             self.assertRaisesRegex(cleanup.CleanupError, "--apply requires a trusted release workflow run ID"):
            cleanup.prepare_release_cleanup_proof(
                REPO, VERSION, BRANCH, BUILD_SHA, None, require_runtime_proof=True
            )
        identity.assert_not_called()
        byte_proof.assert_not_called()

    def test_apply_proof_requires_trusted_runtime_workflow(self):
        identity = release_identity()
        with mock.patch.object(cleanup, "read_release_identity", return_value=identity), \
             mock.patch.object(cleanup, "verify_release_archive_bytes"):
            with self.assertRaises(cleanup.CleanupError):
                cleanup.prepare_release_cleanup_proof(
                    REPO, VERSION, BRANCH, BUILD_SHA, None, require_runtime_proof=True
                )

    def test_destructive_helpers_require_prepared_release_identity(self):
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_released_version(REPO, VERSION, BRANCH, BUILD_SHA, dry_run=False)
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_released_source_caches(REPO, VERSION, BRANCH, BUILD_SHA, dry_run=False)

    def test_publisher_cleanup_exports_exact_proof_inputs(self):
        workflow = (ROOT / ".github/workflows/publish-i686-release.yml").read_text(encoding="utf-8")
        section = workflow.split("  cleanup_published_checkpoints:\n", 1)[1]
        self.assertIn("VALIDATED_BUILD_SHA: ${{ needs.validate.outputs.head_sha }}", section)
        self.assertIn("RELEASE_WORKFLOW_RUN_ID: ${{ github.run_id }}", section)
        self.assertIn('--release-workflow-run-id "${RELEASE_WORKFLOW_RUN_ID}"', section)
        self.assertIn('--expected-build-sha "${VALIDATED_BUILD_SHA}"', section)
    def test_invalid_inputs_fail_closed(self):
        invalid_cases = [
            ("owner repo", VERSION, BRANCH, BUILD_SHA),
            (REPO, "not-a-version", BRANCH, BUILD_SHA),
            (REPO, VERSION, "../main", BUILD_SHA),
            (REPO, VERSION, BRANCH, "not-a-sha"),
        ]
        for args in invalid_cases:
            with self.subTest(args=args), self.assertRaises(cleanup.CleanupError):
                cleanup.validate_inputs(*args)

    def test_artifact_state_non_404_failure_is_fatal(self):
        with mock.patch.object(cleanup, "run_gh", return_value=done(code=1, stderr="gh: server exploded (HTTP 500)")), self.assertRaises(cleanup.CleanupError):
            cleanup.artifact_is_missing(REPO, 101)

    def test_checkpoint_metadata_fail_closed_and_unowned_artifact_skips(self):
        malformed = artifact(101, 201, 2)
        malformed["expired"] = "false"
        with mock.patch.object(cleanup, "list_checkpoint_artifacts", return_value=[malformed]), self.assertRaises(cleanup.CleanupError):
            cleanup.find_version_checkpoints(REPO, VERSION, BRANCH)

        unowned = artifact(102, 202, 2)
        unowned["workflow_run"] = None
        with mock.patch.object(cleanup, "list_checkpoint_artifacts", return_value=[unowned]), mock.patch.object(cleanup, "run_gh") as gh:
            self.assertEqual(cleanup.find_version_checkpoints(REPO, VERSION, BRANCH), [])
            gh.assert_not_called()

    def test_apply_deletes_each_selected_checkpoint_once(self):
        items = [
            cleanup.CheckpointArtifact(101, 201, 2, 111),
            cleanup.CheckpointArtifact(102, 202, 3, 222),
        ]
        identity = mock.sentinel.release_identity
        with mock.patch.object(cleanup, "revalidate_release_identity") as revalidate, \
             mock.patch.object(cleanup, "ensure_no_active_build_for_version") as active, \
             mock.patch.object(cleanup, "find_version_checkpoints", return_value=items), \
             mock.patch.object(cleanup, "delete_checkpoint", side_effect=["deleted:101", "deleted:102"]) as delete:
            results, total = cleanup.cleanup_released_version(
                REPO, VERSION, BRANCH, BUILD_SHA, dry_run=False, release_identity=identity
            )
        self.assertEqual(revalidate.call_count, 2)
        self.assertEqual(active.call_count, 2)
        self.assertEqual(delete.call_count, 2)
        self.assertEqual(results, ["deleted:101", "deleted:102"])
        self.assertEqual(total, 333)

    def test_same_version_active_build_defers_cleanup(self):
        active = {
            "workflow_runs": [
                {
                    "status": "in_progress",
                    "display_title": f"Chromium i686 {VERSION} · stage 4 · attempt 0",
                }
            ]
        }
        with mock.patch.object(cleanup, "run_gh", return_value=done(active)), self.assertRaises(cleanup.CleanupError):
            cleanup.ensure_no_active_build_for_version(REPO, VERSION, BRANCH)

        safe = {
            "workflow_runs": [
                {
                    "status": "completed",
                    "display_title": f"Chromium i686 {VERSION} - stage 4 - attempt 0",
                },
                {
                    "status": "in_progress",
                    "display_title": "Chromium i686 151.0.7922.137 - stage 1 - attempt 0",
                },
            ]
        }
        with mock.patch.object(cleanup, "run_gh", return_value=done(safe)):
            cleanup.ensure_no_active_build_for_version(REPO, VERSION, BRANCH)

    def test_artifact_pagination_is_bounded(self):
        with mock.patch.object(
            cleanup,
            "run_gh",
            return_value=done({"total_count": cleanup.MAX_ARTIFACTS_PER_STAGE + 1, "artifacts": []}),
        ), self.assertRaises(cleanup.CleanupError):
            cleanup.list_checkpoint_artifacts(REPO)

    def test_only_exact_version_workflow_checkpoints_are_selected(self):
        stage_artifacts = {
            2: [
                artifact(101, 201, 2, 111),
                artifact(103, 203, 2, 333),
            ],
            3: [artifact(102, 202, 3, 222)],
        }

        def fake(args, **kwargs):
            joined = " ".join(args)
            if "actions/artifacts?name=chromium-i686-out-stage-" in joined:
                match = __import__("re").search(r"name=chromium-i686-out-stage-(\d+)", joined)
                assert match
                values = stage_artifacts.get(int(match.group(1)), [])
                return done({"total_count": len(values), "artifacts": values})
            if joined.endswith("actions/runs/201"):
                return done(run_payload(stage=2, path=".github/workflows/chromium-i686.yml@refs/heads/main"))
            if joined.endswith("actions/runs/202"):
                return done(run_payload(version="151.0.7922.137", stage=3))
            if joined.endswith("actions/runs/203"):
                return done(run_payload(stage=2, display_title="legacy title"))
            raise AssertionError(args)

        with mock.patch.object(cleanup, "run_gh", side_effect=fake):
            found = cleanup.find_version_checkpoints(REPO, VERSION, BRANCH)
        self.assertEqual(found, [cleanup.CheckpointArtifact(101, 201, 2, 111)])

    def test_checkpoint_name_filter_must_be_honored(self):
        payload = {"total_count": 1, "artifacts": [artifact(101, 201, 2)]}
        with mock.patch.object(cleanup, "run_gh", return_value=done(payload)), self.assertRaises(cleanup.CleanupError):
            cleanup.list_checkpoint_artifacts(REPO)

    def test_dry_run_never_deletes(self):
        item = cleanup.CheckpointArtifact(101, 201, 2, 111)
        with mock.patch.object(cleanup, "verify_healthy_release", return_value=BUILD_SHA), \
             mock.patch.object(cleanup, "ensure_no_active_build_for_version"), \
             mock.patch.object(cleanup, "find_version_checkpoints", return_value=[item]), \
             mock.patch.object(cleanup, "delete_checkpoint") as delete:
            results, total = cleanup.cleanup_released_version(REPO, VERSION, BRANCH, BUILD_SHA, dry_run=True)
        delete.assert_not_called()
        self.assertEqual(total, 111)
        self.assertEqual(results, ["dry-run:101:run=201:stage=2:bytes=111"])

    def test_delete_is_read_confirmed_and_uncertain_write_is_not_retried(self):
        item = cleanup.CheckpointArtifact(101, 201, 2, 111)
        with mock.patch.object(cleanup, "run_gh", side_effect=[done(code=0), done(code=1, stderr="gh: Not Found (HTTP 404)")]) as gh:
            self.assertEqual(cleanup.delete_checkpoint(REPO, item), "deleted:101")
            self.assertEqual(gh.call_count, 2)
        with mock.patch.object(cleanup, "run_gh", side_effect=[done(code=1, stderr="timeout"), done(code=1, stderr="gh: Not Found (HTTP 404)"), done(code=1, stderr="gh: Not Found (HTTP 404)")]) as gh:
            self.assertEqual(cleanup.delete_checkpoint(REPO, item), "deleted:101")
            self.assertEqual(gh.call_count, 3)


    def test_source_cache_filters_accept_only_version_scoped_contracts(self):
        payloads = {
            f"chromium-src-v4-{VERSION}-": [source_cache(301, f"chromium-src-v4-{VERSION}-12345", 5000)],
            f"chromium-src-v3-{VERSION}": [source_cache(302, f"chromium-src-v3-{VERSION}", 4000)],
            f"chromium-src-v2-{VERSION}": [],
            f"chromium-src-{VERSION}": [source_cache(303, f"chromium-src-{VERSION}", 3000)],
        }

        def fake(args, **kwargs):
            joined = " ".join(args)
            for key_filter, values in payloads.items():
                encoded = __import__("urllib.parse", fromlist=["quote"]).quote(key_filter, safe="")
                if f"key={encoded}" in joined:
                    return done({"total_count": len(values), "actions_caches": values})
            raise AssertionError(args)

        with mock.patch.object(cleanup, "run_gh", side_effect=fake):
            found = cleanup.list_source_caches(REPO, VERSION, BRANCH)
        self.assertEqual(
            found,
            [
                cleanup.SourceCache(303, f"chromium-src-{VERSION}", 3000),
                cleanup.SourceCache(302, f"chromium-src-v3-{VERSION}", 4000),
                cleanup.SourceCache(301, f"chromium-src-v4-{VERSION}-12345", 5000),
            ],
        )

    def test_source_cache_listing_is_bounded_and_filter_is_enforced(self):
        too_many = {"total_count": cleanup.MAX_SOURCE_CACHES_PER_FILTER + 1, "actions_caches": []}
        with mock.patch.object(cleanup, "run_gh", return_value=done(too_many)), self.assertRaises(cleanup.CleanupError):
            cleanup.list_source_caches(REPO, VERSION, BRANCH)

        wrong = {
            "total_count": 1,
            "actions_caches": [source_cache(301, "chromium-src-v4-151.0.7922.137-12345")],
        }
        with mock.patch.object(cleanup, "run_gh", return_value=done(wrong)), self.assertRaises(cleanup.CleanupError):
            cleanup.list_source_caches(REPO, VERSION, BRANCH)

        wrong_ref = {
            "total_count": 1,
            "actions_caches": [source_cache(301, f"chromium-src-v4-{VERSION}-12345", ref="refs/heads/dev")],
        }
        with mock.patch.object(cleanup, "run_gh", return_value=done(wrong_ref)), self.assertRaises(cleanup.CleanupError):
            cleanup.list_source_caches(REPO, VERSION, BRANCH)

    def test_source_cache_delete_is_read_confirmed_without_write_retry(self):
        item = cleanup.SourceCache(301, f"chromium-src-v4-{VERSION}-12345", 5000)
        with mock.patch.object(cleanup, "run_gh", return_value=done(code=1, stderr="timeout")) as gh, \
             mock.patch.object(cleanup, "source_cache_is_missing", return_value=True):
            self.assertEqual(
                cleanup.delete_source_cache(REPO, VERSION, BRANCH, item),
                f"deleted-cache:301:key={item.key}",
            )
            self.assertEqual(gh.call_count, 1)

        with mock.patch.object(cleanup, "run_gh", return_value=done(code=1, stderr="timeout")) as gh, \
             mock.patch.object(cleanup, "source_cache_is_missing", return_value=False), \
             self.assertRaises(cleanup.CleanupError):
            cleanup.delete_source_cache(REPO, VERSION, BRANCH, item)
        self.assertEqual(gh.call_count, 1)

    def test_released_source_cache_cleanup_revalidates_and_dry_run_never_deletes(self):
        items = [cleanup.SourceCache(301, f"chromium-src-v4-{VERSION}-12345", 5000)]
        with mock.patch.object(cleanup, "verify_healthy_release"), \
             mock.patch.object(cleanup, "ensure_no_active_build_for_version"), \
             mock.patch.object(cleanup, "list_source_caches", return_value=items), \
             mock.patch.object(cleanup, "delete_source_cache") as delete:
            results, total = cleanup.cleanup_released_source_caches(
                REPO, VERSION, BRANCH, BUILD_SHA, dry_run=True
            )
        delete.assert_not_called()
        self.assertEqual(total, 5000)
        self.assertEqual(
            results,
            [f"dry-run-cache:301:key=chromium-src-v4-{VERSION}-12345:bytes=5000"],
        )

    def test_released_source_cache_apply_deletes_each_cache_once(self):
        items = [
            cleanup.SourceCache(301, f"chromium-src-v4-{VERSION}-12345", 5000),
            cleanup.SourceCache(302, f"chromium-src-v3-{VERSION}", 4000),
        ]
        identity = mock.sentinel.release_identity
        with mock.patch.object(cleanup, "revalidate_release_identity") as revalidate, \
             mock.patch.object(cleanup, "ensure_no_active_build_for_version") as active, \
             mock.patch.object(cleanup, "list_source_caches", return_value=items), \
             mock.patch.object(
                 cleanup,
                 "delete_source_cache",
                 side_effect=[
                     f"deleted-cache:301:key={items[0].key}",
                     f"deleted-cache:302:key={items[1].key}",
                 ],
             ) as delete:
            results, total = cleanup.cleanup_released_source_caches(
                REPO, VERSION, BRANCH, BUILD_SHA, dry_run=False, release_identity=identity
            )
        self.assertEqual(revalidate.call_count, 2)
        self.assertEqual(active.call_count, 2)
        self.assertEqual(delete.call_count, 2)
        self.assertEqual(total, 9000)
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
