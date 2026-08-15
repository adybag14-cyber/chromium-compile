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
    assets = []
    for idx, name in enumerate(names):
        assets.append({"name": name, "digest": "" if missing_digest and idx == 0 else "sha256:" + "a" * 64})
    return {"isDraft": draft, "isPrerelease": prerelease, "assets": assets}


def artifact(artifact_id, run_id, stage, size=1000, expired=False):
    return {
        "id": artifact_id,
        "name": f"chromium-i686-out-stage-{stage}",
        "size_in_bytes": size,
        "expired": expired,
        "workflow_run": {"id": run_id},
    }


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
                cleanup.verify_healthy_release(REPO, VERSION)

    def test_healthy_release_resolves_tag_commit(self):
        with mock.patch.object(cleanup, "run_gh", side_effect=[done(release_payload()), done(stdout="b" * 40 + "\n")]):
            self.assertEqual(cleanup.verify_healthy_release(REPO, VERSION), "b" * 40)

    def test_artifact_pagination_is_bounded(self):
        with mock.patch.object(cleanup, "run_gh", return_value=done({"total_count": cleanup.MAX_ARTIFACTS + 1, "artifacts": []})), self.assertRaises(cleanup.CleanupError):
            cleanup.list_artifacts(REPO)

    def test_only_exact_version_workflow_checkpoints_are_selected(self):
        artifacts = {
            "total_count": 4,
            "artifacts": [
                artifact(101, 201, 2, 111),
                artifact(102, 202, 3, 222),
                artifact(103, 203, 2, 333),
                {"id": 104, "name": "not-a-checkpoint", "size_in_bytes": 1, "expired": False, "workflow_run": {"id": 204}},
            ],
        }
        def fake(args, **kwargs):
            joined = " ".join(args)
            if "actions/artifacts?" in joined:
                return done(artifacts)
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

    def test_dry_run_never_deletes(self):
        item = cleanup.CheckpointArtifact(101, 201, 2, 111)
        with mock.patch.object(cleanup, "verify_healthy_release", return_value="b" * 40), \
             mock.patch.object(cleanup, "find_version_checkpoints", return_value=[item]), \
             mock.patch.object(cleanup, "delete_checkpoint") as delete:
            results, total = cleanup.cleanup_released_version(REPO, VERSION, BRANCH, dry_run=True)
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


if __name__ == "__main__":
    unittest.main()
