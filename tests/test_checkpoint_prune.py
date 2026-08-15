import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "github_checkpoint_prune.py"
SPEC = importlib.util.spec_from_file_location("github_checkpoint_prune", PATH)
assert SPEC and SPEC.loader
prune = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prune
SPEC.loader.exec_module(prune)

REPO = "owner/repo"
VERSION = "151.0.7922.108"
RUN_ID = "12345"
BRANCH = "main"

def completed(payload):
    return subprocess.CompletedProcess(["gh"], 0, json.dumps(payload), "")

def run_payload(stage=2, **changes):
    payload = {
        "status": "completed",
        "actor": {"login": "github-actions[bot]"},
        "path": ".github/workflows/chromium-i686.yml",
        "head_repository": {"full_name": REPO},
        "head_branch": BRANCH,
        "event": "workflow_dispatch",
        "display_title": f"Chromium i686 {VERSION} - stage {stage} - attempt 0",
    }
    payload.update(changes)
    return payload

def artifacts_payload(stage=2, artifact_id=777, *, expired=False, duplicate=False):
    item = {"id": artifact_id, "name": f"chromium-i686-out-stage-{stage}", "expired": expired}
    items = [item, dict(item)] if duplicate else [item]
    return {"total_count": len(items), "artifacts": items}


class CheckpointPruneTests(unittest.TestCase):
    def test_resolve_accepts_exact_checkpoint_provenance(self):
        with mock.patch.object(prune, "run_gh", side_effect=[completed(run_payload()), completed(artifacts_payload())]):
            self.assertEqual(prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH), 777)

    def test_only_bot_producers_are_prunable_and_ref_suffixed_paths_are_valid(self):
        with mock.patch.object(
            prune, "run_gh",
            side_effect=[
                completed(run_payload(path=".github/workflows/chromium-i686.yml@refs/heads/main")),
                completed(artifacts_payload()),
            ],
        ):
            self.assertEqual(prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH), 777)

        with mock.patch.object(
            prune, "run_gh", side_effect=[completed(run_payload(actor={"login": "human"}))]
        ), self.assertRaisesRegex(prune.PruneError, "not github-actions"):
            prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH)

        with mock.patch.object(
            prune, "run_gh", side_effect=[completed(run_payload(actor=None))]
        ), self.assertRaisesRegex(prune.PruneError, "not github-actions"):
            prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH)

    def test_missing_or_expired_checkpoint_is_noop(self):
        cases = [
            {"total_count": 0, "artifacts": []},
            artifacts_payload(expired=True),
        ]
        for artifacts in cases:
            with self.subTest(artifacts=artifacts), mock.patch.object(
                prune, "run_gh", side_effect=[completed(run_payload()), completed(artifacts)]
            ):
                self.assertIsNone(prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH))

    def test_wrong_workflow_branch_stage_or_duplicate_fails_closed(self):
        cases = [
            (run_payload(path=".github/workflows/other.yml"), artifacts_payload()),
            (run_payload(head_branch="feature"), artifacts_payload()),
            (run_payload(stage=3), artifacts_payload(stage=2)),
            (run_payload(), artifacts_payload(duplicate=True)),
        ]
        for run, artifacts in cases:
            with self.subTest(run=run, artifacts=artifacts), mock.patch.object(
                prune, "run_gh", side_effect=[completed(run), completed(artifacts)]
            ), self.assertRaises(prune.PruneError):
                prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH)

    def test_inputs_and_protected_current_run_are_bounded(self):
        bad = [
            ("owner/repo", "0", VERSION, "2", BRANCH),
            ("owner/repo", "9" * 21, VERSION, "2", BRANCH),
            ("owner/repo", RUN_ID, VERSION, "0", BRANCH),
            ("owner/repo", RUN_ID, VERSION, "51", BRANCH),
            ("bad repo", RUN_ID, VERSION, "2", BRANCH),
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(prune.PruneError):
                prune.resolve_checkpoint_artifact(*args)
        with self.assertRaisesRegex(prune.PruneError, "protected current run"):
            prune.prune_checkpoint(REPO, RUN_ID, VERSION, "2", BRANCH, protect_run_id=RUN_ID)

    def test_incomplete_run_or_malformed_expiry_fails_closed(self):
        with mock.patch.object(
            prune, "run_gh", side_effect=[completed(run_payload(status="in_progress"))]
        ), self.assertRaisesRegex(prune.PruneError, "not completed"):
            prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH)

        malformed = artifacts_payload()
        malformed["artifacts"][0].pop("expired")
        with mock.patch.object(
            prune, "run_gh", side_effect=[completed(run_payload()), completed(malformed)]
        ), self.assertRaisesRegex(prune.PruneError, "malformed expired"):
            prune.resolve_checkpoint_artifact(REPO, RUN_ID, VERSION, "2", BRANCH)

    def test_dry_run_never_deletes(self):
        with mock.patch.object(prune, "resolve_checkpoint_artifact", return_value=777), \
             mock.patch.object(prune, "run_gh") as run_gh:
            self.assertEqual(
                prune.prune_checkpoint(REPO, RUN_ID, VERSION, "2", BRANCH, dry_run=True),
                "dry-run:777",
            )
        run_gh.assert_not_called()

    def test_delete_is_confirmed_and_uncertain_write_is_not_retried(self):
        with mock.patch.object(prune, "resolve_checkpoint_artifact", side_effect=[777, None]), \
             mock.patch.object(prune, "run_gh", return_value=subprocess.CompletedProcess(["gh"], 0, "", "")) as run_gh:
            self.assertEqual(prune.prune_checkpoint(REPO, RUN_ID, VERSION, "2", BRANCH), "deleted:777")
            self.assertEqual(run_gh.call_count, 1)

        with mock.patch.object(prune, "resolve_checkpoint_artifact", side_effect=[777, None]), \
             mock.patch.object(prune, "run_gh", side_effect=prune.PruneError("timeout")) as run_gh:
            self.assertEqual(
                prune.prune_checkpoint(REPO, RUN_ID, VERSION, "2", BRANCH),
                "deleted-after-client-error:777",
            )
            self.assertEqual(run_gh.call_count, 1)


if __name__ == "__main__":
    unittest.main()
