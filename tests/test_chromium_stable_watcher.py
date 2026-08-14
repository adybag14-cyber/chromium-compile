import importlib.util
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "chromium_stable_watcher.py"
SPEC = importlib.util.spec_from_file_location("chromium_stable_watcher", MODULE_PATH)
assert SPEC and SPEC.loader
watcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watcher
SPEC.loader.exec_module(watcher)


class StableWatcherTests(unittest.TestCase):
    def test_version_key_is_numeric(self):
        self.assertGreater(watcher.version_key("150.0.0.10"), watcher.version_key("150.0.0.9"))

    def test_invalid_version_is_rejected(self):
        with self.assertRaises(ValueError):
            watcher.version_key("latest")

    def test_active_build_pauses_the_global_queue(self):
        state = watcher.PortState(set(), set(), set(), {"154.0.0.1"})
        self.assertEqual(
            watcher.select_candidates(["155.0.0.1"], "150.0.0.0", state, 1),
            [],
        )

    def test_blocked_version_does_not_hide_later_versions(self):
        state = watcher.PortState(
            known={"151.0.0.1"},
            released={"152.0.0.1"},
            blocked={"153.0.0.1"},
            active=set(),
        )
        versions = ["155.0.0.2", "151.0.0.1", "154.0.0.1", "153.0.0.1", "152.0.0.1"]
        self.assertEqual(
            watcher.select_candidates(versions, "150.0.0.0", state, 1),
            ["154.0.0.1"],
        )

    def test_candidates_are_oldest_first(self):
        empty = watcher.PortState(set(), set(), set(), set())
        self.assertEqual(
            watcher.select_candidates(["155.0.0.2", "155.0.0.1"], "150.0.0.0", empty, 1),
            ["155.0.0.1"],
        )

    def test_active_versions_include_publisher_title(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "publish-i686-release.yml":
                return [{"status": "in_progress", "conclusion": "", "display_title": "Publish Chromium i686 155.0.1.2 - stage 7 - attempt 0"}]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            self.assertEqual(watcher.list_active_versions("owner/repository"), {"155.0.1.2"})

    def test_failed_run_history_quarantines_preflight_build_and_publisher(self):
        def fake_runs(_repository, workflow, **_kwargs):
            mapping = {
                "chromium-i686-preflight.yml": [
                    {"status": "completed", "conclusion": "failure", "display_title": "Chromium i686 preflight 151.0.7922.108"}
                ],
                "chromium-i686.yml": [
                    {"status": "completed", "conclusion": "cancelled", "display_title": "Chromium i686 152.0.0.1 - stage 2 - attempt 0"}
                ],
                "publish-i686-release.yml": [
                    {"status": "completed", "conclusion": "failure", "display_title": "Publish Chromium i686 153.0.0.1 - stage 4 - attempt 0"}
                ],
            }
            return mapping.get(workflow, [])

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            self.assertEqual(
                watcher.list_quarantined_run_versions("owner/repository"),
                {"151.0.7922.108", "152.0.0.1", "153.0.0.1"},
            )

    def test_workflow_history_saturation_fails_closed(self):
        full_page = {"workflow_runs": [{"display_title": "old"}] * 100}
        with mock.patch.object(watcher, "gh_json", return_value=full_page):
            with self.assertRaises(watcher.WatcherError):
                watcher.list_workflow_runs(
                    "owner/repository",
                    "chromium-i686.yml",
                    created_after=watcher.datetime.now(watcher.timezone.utc),
                    max_pages=2,
                )

    def test_gh_timeout_becomes_watcher_error(self):
        with mock.patch.object(
            watcher.subprocess,
            "run",
            side_effect=watcher.subprocess.TimeoutExpired(["gh"], 1),
        ):
            with self.assertRaises(watcher.WatcherError):
                watcher.run_gh(["api", "repos/owner/repository"], timeout=1)

    def test_uncertain_dispatch_confirms_server_side_acceptance_without_retry(self):
        with mock.patch.object(watcher, "run_gh", side_effect=watcher.WatcherError("timeout")) as run_call, \
             mock.patch.object(watcher, "_recent_exact_run_exists", return_value=True), \
             mock.patch.object(watcher.time, "sleep"):
            watcher.dispatch_preflight("owner/repository", "main", "155.0.1.2", False)
        self.assertEqual(run_call.call_count, 1)

    def test_baseline_and_older_versions_are_ignored(self):
        empty = watcher.PortState(set(), set(), set(), set())
        self.assertEqual(
            watcher.select_candidates(
                ["149.9.9.9", "150.0.0.0", "150.0.0.1"],
                "150.0.0.0",
                empty,
                5,
            ),
            ["150.0.0.1"],
        )


    def test_version_history_repeated_token_fails_closed(self):
        responses = [
            io.BytesIO(json.dumps({"versions": [], "nextPageToken": "repeat"}).encode()),
            io.BytesIO(json.dumps({"versions": [], "nextPageToken": "repeat"}).encode()),
        ]
        with mock.patch.object(watcher.urllib.request, "urlopen", side_effect=responses):
            with self.assertRaises(watcher.WatcherError):
                watcher.fetch_stable_versions("https://example.invalid", "150.0.0.0")

    def test_version_history_page_horizon_fails_closed(self):
        responses = [
            io.BytesIO(json.dumps({"versions": [], "nextPageToken": "a"}).encode()),
            io.BytesIO(json.dumps({"versions": [], "nextPageToken": "b"}).encode()),
        ]
        with mock.patch.object(watcher, "VERSION_API_MAX_PAGES", 2),              mock.patch.object(watcher.urllib.request, "urlopen", side_effect=responses):
            with self.assertRaises(watcher.WatcherError):
                watcher.fetch_stable_versions("https://example.invalid", "150.0.0.0")

    def test_rest_collection_horizon_fails_closed(self):
        with mock.patch.object(watcher, "gh_json", return_value=[{}] * 100):
            with self.assertRaises(watcher.WatcherError):
                watcher.list_rest_items("owner/repo", "releases", max_pages=2)


    def test_force_version_does_not_bypass_active_port_ownership(self):
        with mock.patch.object(watcher, "list_port_run_state", return_value=({"154.0.0.1"}, set())),              mock.patch.object(watcher, "dispatch_preflight") as dispatch_call,              mock.patch.object(watcher, "append_summary"):
            rc = watcher.main([
                "--repository", "owner/repo",
                "--force-version", "155.0.0.1",
                "--dry-run",
                "--baseline", str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json"),
            ])
        self.assertEqual(rc, 0)
        dispatch_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
