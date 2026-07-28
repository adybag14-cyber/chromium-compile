import importlib.util
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

    def test_active_versions_include_paginated_publisher_title(self):
        payload = [
            {"workflow_runs": [{"status": "completed", "display_title": "old"}]},
            {
                "workflow_runs": [
                    {
                        "status": "in_progress",
                        "display_title": "Publish Chromium i686 155.0.1.2 · stage 7 · attempt 0",
                    }
                ]
            },
        ]
        with mock.patch.object(watcher, "gh_json", return_value=payload):
            self.assertEqual(
                watcher.list_active_versions("owner/repository"),
                {"155.0.1.2"},
            )

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


if __name__ == "__main__":
    unittest.main()
