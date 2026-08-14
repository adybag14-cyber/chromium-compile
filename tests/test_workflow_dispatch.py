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


if __name__ == "__main__":
    unittest.main()
