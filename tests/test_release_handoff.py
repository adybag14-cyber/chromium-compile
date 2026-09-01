import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "github_release_handoff.py"
SPEC = importlib.util.spec_from_file_location("github_release_handoff", PATH)
assert SPEC and SPEC.loader
handoff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = handoff
SPEC.loader.exec_module(handoff)

REPO = "owner/repo"
VERSION = "151.0.7922.137"
BRANCH = "main"
SHA = "a" * 40
RUN_ID = "123456"


def run_payload(
    *,
    status="completed",
    conclusion="success",
    sha=SHA,
    version=VERSION,
    lane="linux",
):
    workflow = "chromium-i686.yml" if lane == "linux" else "chromium-windows-i686.yml"
    label = "Chromium i686" if lane == "linux" else "Chromium Windows i686"
    return {
        "path": f".github/workflows/{workflow}@refs/heads/main",
        "head_repository": {"full_name": REPO},
        "head_branch": BRANCH,
        "head_sha": sha,
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": conclusion,
        "display_title": f"{label} {version} - stage 3 - attempt 0",
    }


class ReleaseHandoffTests(unittest.TestCase):
    def test_waits_for_parent_to_become_terminal_success(self):
        sleeper = mock.Mock()
        with mock.patch.object(
            handoff,
            "read_build_run",
            side_effect=[run_payload(status="in_progress", conclusion=None), run_payload()],
        ):
            result = handoff.wait_for_successful_build(
                REPO, RUN_ID, VERSION, BRANCH, SHA, attempts=2, delay_seconds=3, sleeper=sleeper
            )
        self.assertEqual(result["conclusion"], "success")
        sleeper.assert_called_once_with(3)

    def test_terminal_failure_is_rejected(self):
        with mock.patch.object(handoff, "read_build_run", return_value=run_payload(conclusion="failure")), self.assertRaisesRegex(
            handoff.HandoffError, "conclusion 'failure'"
        ):
            handoff.wait_for_successful_build(REPO, RUN_ID, VERSION, BRANCH, SHA, attempts=1)

    def test_identity_is_bound_to_workflow_repo_branch_version_and_sha(self):
        cases = [
            {"path": ".github/workflows/other.yml"},
            {"head_repository": {"full_name": "other/repo"}},
            {"head_branch": "dev"},
            {"head_sha": "b" * 40},
            {"event": "push"},
            {"display_title": "Chromium i686 151.0.7922.999 - stage 3 - attempt 0"},
        ]
        for changes in cases:
            payload = run_payload()
            payload.update(changes)
            with self.subTest(changes=changes), self.assertRaises(handoff.HandoffError):
                handoff.validate_build_identity(payload, REPO, VERSION, BRANCH, SHA)

    def test_existing_workflow_run_publisher_prevents_duplicate_manual_dispatch(self):
        created = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
        with (
            mock.patch.object(handoff, "wait_for_successful_build", return_value=run_payload()),
            mock.patch.object(handoff.dispatch, "workflow_run_created_at", return_value=created),
            mock.patch.object(handoff, "wait_for_legacy_publisher", return_value=True) as existing,
            mock.patch.object(handoff.dispatch, "dispatch_once") as dispatch_once,
        ):
            result = handoff.handoff_release(REPO, RUN_ID, VERSION, BRANCH, SHA)
        self.assertEqual(result, "workflow-run-publisher-present")
        self.assertEqual(existing.call_args.args[1], f"Chromium i686 {VERSION} - stage 3 - attempt 0")
        dispatch_once.assert_not_called()


    def test_legacy_publisher_gets_bounded_materialization_grace(self):
        created = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
        sleeper = mock.Mock()
        with mock.patch.object(handoff.dispatch, "exact_exists_since", side_effect=[False, False, True]) as existing:
            self.assertTrue(
                handoff.wait_for_legacy_publisher(
                    REPO,
                    f"Chromium i686 {VERSION} - stage 3 - attempt 0",
                    BRANCH,
                    SHA,
                    created,
                    attempts=3,
                    delay_seconds=2,
                    sleeper=sleeper,
                )
            )
        self.assertEqual(existing.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)
        sleeper.assert_has_calls([mock.call(2), mock.call(2)])

    def test_missing_workflow_run_publisher_dispatches_lineage_bound_manual_publisher(self):
        created = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
        with (
            mock.patch.object(handoff, "wait_for_successful_build", return_value=run_payload()),
            mock.patch.object(handoff.dispatch, "workflow_run_created_at", return_value=created),
            mock.patch.object(handoff, "wait_for_legacy_publisher", return_value=False),
            mock.patch.object(
                handoff.dispatch, "dispatch_once", return_value="accepted-confirmed"
            ) as dispatch_once,
        ):
            result = handoff.handoff_release(REPO, RUN_ID, VERSION, BRANCH, SHA)
        self.assertEqual(result, "accepted-confirmed")
        args = dispatch_once.call_args.args
        kwargs = dispatch_once.call_args.kwargs
        self.assertEqual(args[1], "publish-i686-release.yml")
        self.assertEqual(args[2], BRANCH)
        self.assertEqual(args[3], f"Publish Chromium i686 {VERSION} from build run {RUN_ID}")
        self.assertIn(f"build_run_id={RUN_ID}", args[4])
        self.assertIn(f"version={VERSION}", args[4])
        self.assertTrue(kwargs["dedupe_completed"])
        self.assertEqual(kwargs["dedupe_since_run_id"], RUN_ID)
        self.assertEqual(kwargs["expected_head_sha"], SHA)

    def test_windows_publisher_receives_independently_verified_build_sha(self):
        created = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
        with (
            mock.patch.object(
                handoff,
                "wait_for_successful_build",
                return_value=run_payload(lane="windows"),
            ),
            mock.patch.object(
                handoff.dispatch,
                "workflow_run_created_at",
                return_value=created,
            ),
            mock.patch.object(handoff, "wait_for_legacy_publisher", return_value=False),
            mock.patch.object(
                handoff.dispatch,
                "dispatch_once",
                return_value="accepted-confirmed",
            ) as dispatch_once,
        ):
            result = handoff.handoff_release(
                REPO,
                RUN_ID,
                VERSION,
                BRANCH,
                SHA,
                lane="windows",
            )
        self.assertEqual(result, "accepted-confirmed")
        inputs = dispatch_once.call_args.args[4]
        self.assertEqual(dispatch_once.call_args.args[1], "publish-windows-i686-release.yml")
        self.assertIn(f"build_run_id={RUN_ID}", inputs)
        self.assertIn(f"version={VERSION}", inputs)
        self.assertIn(f"build_sha={SHA}", inputs)

    def test_cli_inputs_fail_closed_before_github(self):
        invalid = [
            ("bad repo", RUN_ID, VERSION, BRANCH, SHA),
            (REPO, "0", VERSION, BRANCH, SHA),
            (REPO, RUN_ID, "bad", BRANCH, SHA),
            (REPO, RUN_ID, VERSION, "../main", SHA),
            (REPO, RUN_ID, VERSION, BRANCH, "bad"),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(handoff.HandoffError):
                handoff.validate_inputs(*args)

    def test_handoff_workflow_and_final_build_dispatch_are_lineage_bound(self):
        handoff_workflow = (ROOT / ".github/workflows/publish-i686-release-handoff.yml").read_text(encoding="utf-8")
        build_workflow = (ROOT / ".github/workflows/chromium-i686.yml").read_text(encoding="utf-8")
        dispatch_block = build_workflow[
            build_workflow.index("  dispatch_release_handoff:"):
            build_workflow.index("  recover_bad_runner:")
        ]
        self.assertIn("needs.build.outputs.complete == 'true'", dispatch_block)
        self.assertIn("github.ref_name == github.event.repository.default_branch", dispatch_block)
        self.assertIn("actions: write", dispatch_block)
        self.assertIn("contents: read", dispatch_block)
        self.assertIn("ref: ${{ github.sha }}", dispatch_block)
        self.assertIn("--workflow publish-i686-release-handoff.yml", dispatch_block)
        self.assertIn('--expected-head-sha "${LINEAGE_SHA}"', dispatch_block)
        self.assertIn("--dedupe-completed", dispatch_block)
        self.assertIn('--dedupe-since-run-id "${GITHUB_RUN_ID}"', dispatch_block)
        self.assertIn('--input "build_run_id=${GITHUB_RUN_ID}"', dispatch_block)
        self.assertIn("github.ref_name == github.event.repository.default_branch", handoff_workflow)
        self.assertIn("actions: write", handoff_workflow)
        self.assertIn("contents: read", handoff_workflow)
        self.assertIn("ref: ${{ github.workflow_sha }}", handoff_workflow)
        self.assertIn('test "${WORKFLOW_SHA}" = "${LINEAGE_SHA}"', handoff_workflow)
        self.assertIn("scripts/github_release_handoff.py", handoff_workflow)
        self.assertNotIn("pull_request_target", handoff_workflow)

        windows_publish = (
            ROOT / ".github/workflows/publish-windows-i686-release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("build_sha:", windows_publish)
        self.assertIn("BUILD_SHA: ${{ inputs.build_sha }}", windows_publish)
        self.assertIn('--expected-sha "${BUILD_SHA}"', windows_publish)
        self.assertNotIn('--expected-sha "${WORKFLOW_SHA}"', windows_publish)


if __name__ == "__main__":
    unittest.main()
