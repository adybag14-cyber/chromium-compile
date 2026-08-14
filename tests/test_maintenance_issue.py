import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "github_maintenance_issue.py"
SPEC = importlib.util.spec_from_file_location("github_maintenance_issue", MODULE_PATH)
assert SPEC and SPEC.loader
issues = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = issues
SPEC.loader.exec_module(issues)


class MaintenanceIssueTests(unittest.TestCase):
    def test_find_issue_requires_unique_exact_title(self):
        payload = json.dumps([
            {"number": 1, "title": "target"},
            {"number": 2, "title": "other"},
        ])
        with mock.patch.object(
            issues,
            "run_gh",
            return_value=subprocess.CompletedProcess(["gh"], 0, payload, ""),
        ):
            self.assertEqual(issues.find_issue("owner/repo", "target"), 1)

    def test_issue_lookup_horizon_saturation_fails_closed(self):
        payload = json.dumps([{"number": i + 1, "title": f"issue-{i}"} for i in range(issues.ISSUE_LIST_LIMIT)])
        with mock.patch.object(
            issues,
            "run_gh",
            return_value=subprocess.CompletedProcess(["gh"], 0, payload, ""),
        ), mock.patch.object(issues.time, "sleep"):
            with self.assertRaises(issues.IssueError):
                issues.find_issue("owner/repo", "target", attempts=1)

    def test_duplicate_exact_titles_fail_closed(self):
        payload = json.dumps([
            {"number": 1, "title": "target"},
            {"number": 2, "title": "target"},
        ])
        with mock.patch.object(
            issues,
            "run_gh",
            return_value=subprocess.CompletedProcess(["gh"], 0, payload, ""),
        ), mock.patch.object(issues.time, "sleep"):
            with self.assertRaises(issues.IssueError):
                issues.find_issue("owner/repo", "target", attempts=1)

    def test_existing_issue_comment_failure_does_not_create_duplicate(self):
        with mock.patch.object(issues, "find_issue", return_value=7),              mock.patch.object(issues, "run_gh", side_effect=issues.IssueError("comment timeout")) as run_gh:
            action, number = issues.upsert_issue("owner/repo", "target", "body.md")
        self.assertEqual((action, number), ("updated", 7))
        self.assertEqual(run_gh.call_count, 1)

    def test_uncertain_create_confirms_issue_instead_of_retrying_write(self):
        with mock.patch.object(issues, "find_issue", side_effect=[None, 9]),              mock.patch.object(issues, "run_gh", side_effect=issues.IssueError("create timeout")) as run_gh,              mock.patch.object(issues.time, "sleep"):
            action, number = issues.upsert_issue("owner/repo", "target", "body.md")
        self.assertEqual((action, number), ("created-after-client-error", 9))
        self.assertEqual(run_gh.call_count, 1)

    def test_successful_create_is_confirmed(self):
        with mock.patch.object(issues, "find_issue", side_effect=[None, 11]),              mock.patch.object(
                 issues,
                 "run_gh",
                 return_value=subprocess.CompletedProcess(["gh"], 0, "https://github.com/x/11\n", ""),
             ) as run_gh:
            action, number = issues.upsert_issue("owner/repo", "target", "body.md")
        self.assertEqual((action, number), ("created", 11))
        self.assertEqual(run_gh.call_count, 1)


    def test_close_if_open_is_idempotent(self):
        with mock.patch.object(issues, "find_issue", return_value=None), \
             mock.patch.object(issues, "run_gh") as run_gh:
            self.assertEqual(
                issues.close_issue_if_open("owner/repo", "target"),
                ("already-closed", None),
            )
        run_gh.assert_not_called()

    def test_close_timeout_confirms_closed_state(self):
        with mock.patch.object(issues, "find_issue", side_effect=[7, None]), \
             mock.patch.object(issues, "run_gh", side_effect=issues.IssueError("timeout")):
            self.assertEqual(
                issues.close_issue_if_open("owner/repo", "target"),
                ("closed-after-client-error", 7),
            )


if __name__ == "__main__":
    unittest.main()
