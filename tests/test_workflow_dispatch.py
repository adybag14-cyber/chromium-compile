import importlib.util
import pathlib
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "github_workflow_dispatch.py"
SPEC = importlib.util.spec_from_file_location("github_workflow_dispatch", MODULE_PATH)
assert SPEC and SPEC.loader
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)


class WorkflowDispatchTests(unittest.TestCase):
    def test_run_lookup_uses_long_horizon(self):
        payload = "[]"
        with mock.patch.object(
            dispatch,
            "run_gh",
            return_value=subprocess.CompletedProcess(["gh"], 0, payload, ""),
        ) as run_gh:
            dispatch.list_recent_runs("owner/repo", "workflow.yml", attempts=1)
        args = run_gh.call_args.args[0]
        self.assertIn(str(dispatch.RUN_LOOKUP_LIMIT), args)
        self.assertGreaterEqual(dispatch.RUN_LOOKUP_LIMIT, 1000)

    def test_saturated_run_lookup_without_exact_title_fails_closed(self):
        runs = [
            {"displayTitle": f"other-{i}", "status": "completed", "createdAt": "2026-01-01T00:00:00Z"}
            for i in range(dispatch.RUN_LOOKUP_LIMIT)
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.exact_active_exists("owner/repo", "workflow.yml", "Expected title")

    def test_active_exact_run_prevents_duplicate_dispatch(self):
        with mock.patch.object(dispatch, "exact_active_exists", return_value=True),              mock.patch.object(dispatch, "run_gh") as run_gh:
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["version=1.2.3.4"],
            )
        self.assertEqual(result, "already-active")
        run_gh.assert_not_called()

    def test_successful_dispatch_is_called_once(self):
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False),              mock.patch.object(dispatch, "run_gh") as run_gh:
            run_gh.return_value = subprocess.CompletedProcess(["gh"], 0, "", "")
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["version=1.2.3.4", "stage=2"],
            )
        self.assertEqual(result, "accepted")
        self.assertEqual(run_gh.call_count, 1)

    def test_uncertain_dispatch_confirms_acceptance_without_second_write(self):
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False),              mock.patch.object(dispatch, "run_gh", side_effect=dispatch.DispatchError("timeout")) as run_gh,              mock.patch.object(dispatch, "exact_recent_exists", return_value=True),              mock.patch.object(dispatch.time, "sleep"):
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["version=1.2.3.4"],
            )
        self.assertEqual(result, "accepted-after-client-error")
        self.assertEqual(run_gh.call_count, 1)

    def test_unconfirmed_dispatch_error_fails_without_write_retry(self):
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False),              mock.patch.object(dispatch, "run_gh", side_effect=dispatch.DispatchError("timeout")) as run_gh,              mock.patch.object(dispatch, "exact_recent_exists", return_value=False),              mock.patch.object(dispatch.time, "sleep"):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.dispatch_once(
                    "owner/repo",
                    "workflow.yml",
                    "main",
                    "Expected title",
                    ["version=1.2.3.4"],
                    confirm_attempts=2,
                )
        self.assertEqual(run_gh.call_count, 1)

    def test_invalid_input_is_rejected_before_dispatch(self):
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False),              mock.patch.object(dispatch, "run_gh") as run_gh:
            with self.assertRaises(ValueError):
                dispatch.dispatch_once(
                    "owner/repo", "workflow.yml", "main", "Expected", ["broken"]
                )
        run_gh.assert_not_called()


    def test_completed_exact_run_can_be_deduped_for_job_reruns(self):
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(dispatch, "workflow_run_created_at", return_value=parent_started), \
             mock.patch.object(dispatch, "exact_exists_since", return_value=True), \
             mock.patch.object(dispatch, "run_gh") as run_gh:
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["stage=2"],
                dedupe_completed=True,
                dedupe_since_run_id="12345",
            )
        self.assertEqual(result, "already-seen")
        run_gh.assert_not_called()

    def test_historical_same_title_does_not_block_fresh_build_lineage(self):
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        old_run = {
            "displayTitle": "Expected title",
            "status": "completed",
            "createdAt": "2026-08-01T00:00:00Z",
        }
        with mock.patch.object(dispatch, "workflow_run_created_at", return_value=parent_started), \
             mock.patch.object(dispatch, "list_recent_runs", return_value=[old_run]), \
             mock.patch.object(
                 dispatch,
                 "run_gh",
                 return_value=subprocess.CompletedProcess(["gh"], 0, "", ""),
             ) as run_gh:
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["stage=2"],
                dedupe_completed=True,
                dedupe_since_run_id="12345",
            )
        self.assertEqual(result, "accepted")
        self.assertEqual(run_gh.call_count, 1)

    def test_completed_dedupe_requires_parent_run_scope(self):
        with self.assertRaises(ValueError):
            dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["stage=2"],
                dedupe_completed=True,
            )


if __name__ == "__main__":
    unittest.main()
