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

    def test_feature_branch_failure_does_not_quarantine_production(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "chromium-i686.yml":
                return [
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "display_title": "Chromium i686 155.0.1.2 - stage 2 - attempt 0",
                        "head_branch": "experiment",
                    }
                ]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            active, quarantined = watcher.list_port_run_state("owner/repository", "main")
        self.assertEqual(active, set())
        self.assertEqual(quarantined, set())

    def test_feature_branch_active_run_still_owns_global_queue(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "chromium-i686.yml":
                return [
                    {
                        "status": "in_progress",
                        "conclusion": "",
                        "display_title": "Chromium i686 155.0.1.2 - stage 2 - attempt 0",
                        "head_branch": "experiment",
                    }
                ]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            active, quarantined = watcher.list_port_run_state("owner/repository", "main")
        self.assertEqual(active, {"155.0.1.2"})
        self.assertEqual(quarantined, set())

    def test_terminal_run_without_branch_fails_closed_for_production_quarantine(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "chromium-i686-preflight.yml":
                return [
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "display_title": "Chromium i686 preflight 155.0.1.2",
                    }
                ]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            with self.assertRaises(watcher.WatcherError):
                watcher.list_port_run_state("owner/repository", "main")

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

    def test_uncertain_dispatch_uses_central_exact_once_confirmation(self):
        with mock.patch.object(watcher, "dispatch_once", return_value="accepted-after-client-error") as dispatch_call:
            watcher.dispatch_preflight("owner/repository", "main", "155.0.1.2", False)
        dispatch_call.assert_called_once_with(
            "owner/repository",
            "chromium-i686-preflight.yml",
            "main",
            "Chromium i686 preflight 155.0.1.2",
            ["version=155.0.1.2", "dispatch_build=true"],
        )

    def test_racing_manual_preflight_is_deduped_by_central_dispatcher(self):
        with mock.patch.object(watcher, "dispatch_once", return_value="already-active") as dispatch_call:
            watcher.dispatch_preflight("owner/repository", "main", "155.0.1.2", False)
        self.assertEqual(dispatch_call.call_count, 1)

    def test_dispatcher_failure_becomes_watcher_error(self):
        with mock.patch.object(watcher, "dispatch_once", side_effect=watcher.DispatchError("timeout")):
            with self.assertRaises(watcher.WatcherError):
                watcher.dispatch_preflight("owner/repository", "main", "155.0.1.2", False)

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

    @staticmethod
    def _healthy_release(version: str, **overrides):
        release = {
            "tag_name": f"chromium-{version}-linux-i686",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": f"chromium-{version}-linux-i686.tar.xz",
                    "state": "uploaded",
                    "size": 100,
                    "digest": "sha256:" + "a" * 64,
                },
                {
                    "name": f"chromium-{version}-linux-i686.tar.xz.sha256",
                    "state": "uploaded",
                    "size": 100,
                    "digest": "sha256:" + "b" * 64,
                },
                {
                    "name": f"chromium-{version}-linux-i686-manifest.txt",
                    "state": "uploaded",
                    "size": 100,
                    "digest": "sha256:" + "c" * 64,
                },
            ],
        }
        release.update(overrides)
        return release

    def test_draft_release_does_not_mark_version_released(self):
        releases = [
            self._healthy_release("151.0.0.1", draft=True),
            self._healthy_release("152.0.0.1"),
        ]
        with mock.patch.object(watcher, "list_rest_items", return_value=releases):
            healthy, broken = watcher.list_release_health("owner/repo")
            self.assertEqual(healthy, {"152.0.0.1"})
            self.assertEqual(broken, {"151.0.0.1"})
            self.assertEqual(watcher.list_release_versions("owner/repo"), {"152.0.0.1"})

    def test_incomplete_release_assets_are_broken(self):
        release = self._healthy_release("153.0.0.1")
        release["assets"] = release["assets"][:-1]
        with mock.patch.object(watcher, "list_rest_items", return_value=[release]):
            healthy, broken = watcher.list_release_health("owner/repo")
        self.assertEqual(healthy, set())
        self.assertEqual(broken, {"153.0.0.1"})

    def test_unverifiable_release_asset_is_broken(self):
        for field, value in (("state", "new"), ("size", 0), ("digest", None)):
            with self.subTest(field=field):
                release = self._healthy_release("154.0.0.1")
                release["assets"][0][field] = value
                with mock.patch.object(watcher, "list_rest_items", return_value=[release]):
                    healthy, broken = watcher.list_release_health("owner/repo")
                self.assertEqual(healthy, set())
                self.assertEqual(broken, {"154.0.0.1"})

    def test_prerelease_and_duplicate_assets_are_broken(self):
        prerelease = self._healthy_release("155.0.0.1", prerelease=True)
        duplicate = self._healthy_release("156.0.0.1")
        duplicate["assets"].append(dict(duplicate["assets"][0]))
        with mock.patch.object(watcher, "list_rest_items", return_value=[prerelease, duplicate]):
            healthy, broken = watcher.list_release_health("owner/repo")
        self.assertEqual(healthy, set())
        self.assertEqual(broken, {"155.0.0.1", "156.0.0.1"})


    def test_force_version_does_not_bypass_active_port_ownership(self):
        with mock.patch.object(watcher, "list_port_run_state", return_value=({"154.0.0.1"}, set())),              mock.patch.object(watcher, "dispatch_preflight") as dispatch_call:
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
