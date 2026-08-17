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
    def test_dispatch_lookup_env_is_validated_and_clamped(self):
        with mock.patch.dict(dispatch.os.environ, {"X": "garbage"}, clear=False):
            self.assertEqual(dispatch._bounded_int_env("X", 1000, 100, 5000), 1000)
        with mock.patch.dict(dispatch.os.environ, {"X": "1"}, clear=False):
            self.assertEqual(dispatch._bounded_int_env("X", 1000, 100, 5000), 100)
        with mock.patch.dict(dispatch.os.environ, {"X": "999999"}, clear=False):
            self.assertEqual(dispatch._bounded_int_env("X", 1000, 100, 5000), 5000)

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
        self.assertIn("databaseId,displayTitle,headBranch,headSha,status,conclusion,createdAt", args)
        self.assertGreaterEqual(dispatch.RUN_LOOKUP_LIMIT, 1000)

    def test_run_lookup_pushes_branch_commit_and_time_filters_to_github(self):
        expected_sha = "a" * 40
        created_after = datetime(2026, 8, 14, 12, 0, 0, 987654, tzinfo=timezone.utc)
        with mock.patch.object(
            dispatch,
            "run_gh",
            return_value=subprocess.CompletedProcess(["gh"], 0, "[]", ""),
        ) as run_gh:
            dispatch.list_recent_runs(
                "owner/repo",
                "workflow.yml",
                attempts=1,
                branch="feature/slash",
                commit=expected_sha.upper(),
                created_after=created_after,
                status="in_progress",
            )
        args = run_gh.call_args.args[0]
        self.assertIn("--branch=feature/slash", args)
        self.assertIn(f"--commit={expected_sha}", args)
        self.assertIn("--created=>=2026-08-14T12:00:00Z", args)
        self.assertIn("--status=in_progress", args)

    def test_run_lookup_rejects_timezone_naive_created_filter_before_github(self):
        with mock.patch.object(dispatch, "run_gh") as run_gh, self.assertRaisesRegex(
            ValueError, "timezone-aware"
        ):
            dispatch.list_recent_runs(
                "owner/repo",
                "workflow.yml",
                attempts=1,
                created_after=datetime(2026, 8, 14, 12, 0),
            )
        run_gh.assert_not_called()

    def test_exact_lineage_lookup_scopes_to_branch_and_commit(self):
        expected_sha = "a" * 40
        run = {
            "displayTitle": "Expected title",
            "headBranch": "main",
            "headSha": expected_sha,
            "status": "in_progress",
            "createdAt": "2026-08-14T12:00:00Z",
        }
        with mock.patch.object(dispatch, "list_recent_runs", return_value=[run]) as listing:
            self.assertTrue(
                dispatch.exact_active_exists(
                    "owner/repo",
                    "workflow.yml",
                    "Expected title",
                    "main",
                    expected_head_sha=expected_sha,
                )
            )
        self.assertEqual(listing.call_args.kwargs["branch"], "main")
        self.assertEqual(listing.call_args.kwargs["commit"], expected_sha)
        self.assertIsNone(listing.call_args.kwargs["created_after"])

    def test_recent_dispatch_confirmation_keeps_wrong_sha_visible(self):
        expected_sha = "a" * 40
        started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        wrong = {
            "displayTitle": "Expected title",
            "headBranch": "main",
            "headSha": "b" * 40,
            "status": "queued",
            "createdAt": "2026-08-14T12:00:01Z",
        }
        with mock.patch.object(dispatch, "list_recent_runs", return_value=[wrong]) as listing:
            self.assertEqual(
                dispatch.recent_dispatch_head_state(
                    "owner/repo",
                    "workflow.yml",
                    "Expected title",
                    "main",
                    expected_sha,
                    started,
                ),
                "mismatch",
            )
        self.assertEqual(listing.call_args.kwargs["branch"], "main")
        self.assertNotIn("commit", listing.call_args.kwargs)
        self.assertEqual(
            listing.call_args.kwargs["created_after"],
            datetime(2026, 8, 14, 11, 59, 30, tzinfo=timezone.utc),
        )

    def test_parent_scoped_dedupe_pushes_created_filter_server_side(self):
        expected_sha = "a" * 40
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        run = {
            "displayTitle": "Expected title",
            "headBranch": "main",
            "headSha": expected_sha,
            "status": "completed",
            "createdAt": "2026-08-14T12:00:01Z",
        }
        with mock.patch.object(dispatch, "list_recent_runs", return_value=[run]) as listing:
            self.assertTrue(
                dispatch.exact_exists_since(
                    "owner/repo",
                    "workflow.yml",
                    "Expected title",
                    "main",
                    parent_started,
                    grace_seconds=30,
                    expected_head_sha=expected_sha,
                )
            )
        self.assertEqual(listing.call_args.kwargs["branch"], "main")
        self.assertEqual(listing.call_args.kwargs["commit"], expected_sha)
        self.assertEqual(
            listing.call_args.kwargs["created_after"],
            datetime(2026, 8, 14, 11, 59, 30, tzinfo=timezone.utc),
        )

    def test_active_lookup_is_server_scoped_to_incomplete_statuses(self):
        expected_sha = "a" * 40
        active_run = {
            "displayTitle": "Expected title",
            "headBranch": "main",
            "headSha": expected_sha,
            "status": "queued",
            "createdAt": "2026-08-14T12:00:00Z",
        }

        def fake_exact(*args, **kwargs):
            self.assertIn(kwargs["status"], dispatch.ACTIVE_STATUSES)
            return [active_run] if kwargs["status"] == "queued" else []

        with mock.patch.object(dispatch, "_exact_runs_or_fail_closed", side_effect=fake_exact) as exact:
            self.assertTrue(
                dispatch.exact_active_exists(
                    "owner/repo",
                    "workflow.yml",
                    "Expected title",
                    "main",
                    expected_head_sha=expected_sha,
                )
            )
        self.assertEqual(exact.call_count, 2)
        self.assertEqual(exact.call_args_list[0].kwargs["status"], "in_progress")
        self.assertEqual(exact.call_args_list[1].kwargs["status"], "queued")

    def test_completed_history_cannot_consume_active_lookup_budget(self):
        expected_sha = "a" * 40
        observed_statuses = []

        def fake_listing(*args, **kwargs):
            observed_statuses.append(kwargs.get("status"))
            return []

        with mock.patch.object(dispatch, "list_recent_runs", side_effect=fake_listing):
            self.assertFalse(
                dispatch.exact_active_exists(
                    "owner/repo",
                    "workflow.yml",
                    "Expected title",
                    "main",
                    expected_head_sha=expected_sha,
                )
            )
        self.assertEqual(observed_statuses, list(dispatch.ACTIVE_STATUSES))
        self.assertNotIn(None, observed_statuses)

    def test_saturated_run_lookup_without_exact_title_fails_closed(self):
        runs = [
            {"displayTitle": f"other-{i}", "status": "completed", "createdAt": "2026-01-01T00:00:00Z"}
            for i in range(dispatch.RUN_LOOKUP_LIMIT)
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.exact_active_exists("owner/repo", "workflow.yml", "Expected title", "main")

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

    def test_same_title_on_different_branch_does_not_dedupe(self):
        runs = [
            {
                "displayTitle": "Expected title",
                "headBranch": "experiment",
                "status": "in_progress",
                "createdAt": "2026-08-14T12:00:00Z",
            }
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            self.assertFalse(
                dispatch.exact_active_exists("owner/repo", "workflow.yml", "Expected title", "main")
            )

    def test_same_title_on_same_branch_dedupes(self):
        runs = [
            {
                "displayTitle": "Expected title",
                "headBranch": "main",
                "status": "in_progress",
                "createdAt": "2026-08-14T12:00:00Z",
            }
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            self.assertTrue(
                dispatch.exact_active_exists("owner/repo", "workflow.yml", "Expected title", "main")
            )

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

    def test_unconfirmed_dispatch_error_fails_without_write_retry_and_backs_off(self):
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False),              mock.patch.object(dispatch, "run_gh", side_effect=dispatch.DispatchError("timeout")) as run_gh,              mock.patch.object(dispatch, "exact_recent_exists", return_value=False) as confirm,              mock.patch.object(dispatch.time, "sleep") as sleep:
            with self.assertRaises(dispatch.DispatchError):
                dispatch.dispatch_once(
                    "owner/repo",
                    "workflow.yml",
                    "main",
                    "Expected title",
                    ["version=1.2.3.4"],
                    confirm_attempts=3,
                )
        self.assertEqual(run_gh.call_count, 1)
        self.assertEqual(confirm.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 3])

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

    def test_run_just_before_parent_start_does_not_block_new_lineage(self):
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        runs = [
            {
                "displayTitle": "Expected title",
                "headBranch": "main",
                "status": "completed",
                "createdAt": "2026-08-14T11:59:50Z",
            }
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            self.assertFalse(
                dispatch.exact_exists_since(
                    "owner/repo", "workflow.yml", "Expected title", "main", parent_started
                )
            )
            self.assertTrue(
                dispatch.exact_recent_exists(
                    "owner/repo", "workflow.yml", "Expected title", "main", parent_started
                )
            )

    def test_parent_scoped_dedupe_fails_closed_on_malformed_timestamp(self):
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        run = {
            "displayTitle": "Expected title",
            "headBranch": "main",
            "status": "completed",
            "createdAt": "not-a-timestamp",
        }
        with mock.patch.object(dispatch, "list_recent_runs", return_value=[run]):
            with self.assertRaisesRegex(dispatch.DispatchError, "invalid createdAt metadata"):
                dispatch.exact_exists_since(
                    "owner/repo", "workflow.yml", "Expected title", "main", parent_started
                )

    def test_parent_scoped_dedupe_fails_closed_on_naive_timestamp(self):
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        run = {
            "displayTitle": "Expected title",
            "headBranch": "main",
            "status": "completed",
            "createdAt": "2026-08-14T12:00:01",
        }
        with mock.patch.object(dispatch, "list_recent_runs", return_value=[run]):
            with self.assertRaisesRegex(dispatch.DispatchError, "timezone-naive createdAt"):
                dispatch.exact_exists_since(
                    "owner/repo", "workflow.yml", "Expected title", "main", parent_started
                )

    def test_historical_same_title_does_not_block_fresh_build_lineage(self):
        parent_started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        old_run = {
            "displayTitle": "Expected title",
            "headBranch": "main",
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

    def test_lineage_sha_filters_same_title_same_branch_runs(self):
        expected_sha = "a" * 40
        runs = [
            {
                "displayTitle": "Expected title",
                "headBranch": "main",
                "headSha": "b" * 40,
                "status": "in_progress",
                "createdAt": "2026-08-14T12:00:00Z",
            }
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            self.assertFalse(
                dispatch.exact_active_exists(
                    "owner/repo", "workflow.yml", "Expected title", "main", expected_head_sha=expected_sha
                )
            )
            self.assertTrue(
                dispatch.exact_active_exists(
                    "owner/repo", "workflow.yml", "Expected title", "main", expected_head_sha="b" * 40
                )
            )

    def test_moved_ref_is_rejected_before_dispatch_write(self):
        expected_sha = "a" * 40
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False), \
             mock.patch.object(dispatch, "resolve_ref_sha", return_value="b" * 40), \
             mock.patch.object(dispatch, "run_gh") as run_gh:
            with self.assertRaisesRegex(dispatch.DispatchError, "refusing workflow dispatch"):
                dispatch.dispatch_once(
                    "owner/repo",
                    "workflow.yml",
                    "main",
                    "Expected title",
                    ["stage=2"],
                    expected_head_sha=expected_sha,
                )
        run_gh.assert_not_called()

    def test_successful_pinned_dispatch_confirms_materialized_head(self):
        expected_sha = "a" * 40
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False), \
             mock.patch.object(dispatch, "resolve_ref_sha", return_value=expected_sha), \
             mock.patch.object(
                 dispatch,
                 "run_gh",
                 return_value=subprocess.CompletedProcess(["gh"], 0, "", ""),
             ) as run_gh, \
             mock.patch.object(dispatch, "confirm_expected_dispatch_head", return_value=True) as confirm:
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["stage=2"],
                expected_head_sha=expected_sha.upper(),
            )
        self.assertEqual(result, "accepted-confirmed")
        self.assertEqual(run_gh.call_count, 1)
        self.assertEqual(confirm.call_args.args[4], expected_sha)

    def test_post_dispatch_matching_run_with_malformed_timestamp_fails_closed(self):
        expected_sha = "a" * 40
        runs = [
            {
                "displayTitle": "Expected title",
                "headBranch": "main",
                "headSha": expected_sha,
                "status": "queued",
                "createdAt": "not-a-timestamp",
            }
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            with self.assertRaisesRegex(dispatch.DispatchError, "invalid createdAt metadata"):
                dispatch.recent_dispatch_head_state(
                    "owner/repo",
                    "workflow.yml",
                    "Expected title",
                    "main",
                    expected_sha,
                    datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
                )

    def test_post_dispatch_matching_head_wins_over_other_same_window_run(self):
        expected_sha = "a" * 40
        started = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        runs = [
            {
                "displayTitle": "Expected title",
                "headBranch": "main",
                "headSha": "b" * 40,
                "status": "queued",
                "createdAt": "2026-08-14T11:59:45Z",
            },
            {
                "displayTitle": "Expected title",
                "headBranch": "main",
                "headSha": expected_sha,
                "status": "queued",
                "createdAt": "2026-08-14T12:00:01Z",
            },
        ]
        with mock.patch.object(dispatch, "list_recent_runs", return_value=runs):
            self.assertEqual(
                dispatch.recent_dispatch_head_state(
                    "owner/repo", "workflow.yml", "Expected title", "main", expected_sha, started
                ),
                "matched",
            )

    def test_post_dispatch_head_mismatch_fails_without_second_write(self):
        expected_sha = "a" * 40
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False), \
             mock.patch.object(dispatch, "resolve_ref_sha", return_value=expected_sha), \
             mock.patch.object(
                 dispatch,
                 "run_gh",
                 return_value=subprocess.CompletedProcess(["gh"], 0, "", ""),
             ) as run_gh, \
             mock.patch.object(dispatch, "recent_dispatch_head_state", return_value="mismatch"), \
             mock.patch.object(dispatch.time, "sleep"):
            with self.assertRaisesRegex(dispatch.DispatchError, "different head SHA"):
                dispatch.dispatch_once(
                    "owner/repo",
                    "workflow.yml",
                    "main",
                    "Expected title",
                    ["stage=2"],
                    expected_head_sha=expected_sha,
                )
        self.assertEqual(run_gh.call_count, 1)

    def test_uncertain_pinned_dispatch_confirms_without_write_retry(self):
        expected_sha = "a" * 40
        with mock.patch.object(dispatch, "exact_active_exists", return_value=False), \
             mock.patch.object(dispatch, "resolve_ref_sha", return_value=expected_sha), \
             mock.patch.object(dispatch, "run_gh", side_effect=dispatch.DispatchError("timeout")) as run_gh, \
             mock.patch.object(dispatch, "confirm_expected_dispatch_head", return_value=True):
            result = dispatch.dispatch_once(
                "owner/repo",
                "workflow.yml",
                "main",
                "Expected title",
                ["stage=2"],
                expected_head_sha=expected_sha,
            )
        self.assertEqual(result, "accepted-after-client-error")
        self.assertEqual(run_gh.call_count, 1)

    def test_invalid_expected_head_sha_fails_before_network(self):
        with mock.patch.object(dispatch, "run_gh") as run_gh:
            with self.assertRaises(ValueError):
                dispatch.dispatch_once(
                    "owner/repo",
                    "workflow.yml",
                    "main",
                    "Expected title",
                    ["stage=2"],
                    expected_head_sha="not-a-sha",
                )
        run_gh.assert_not_called()



if __name__ == "__main__":
    unittest.main()
