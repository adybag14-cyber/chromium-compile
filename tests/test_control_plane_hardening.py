import pathlib
import unittest

ROOT = pathlib.Path(__file__).parents[1]


class ControlPlaneHardeningTests(unittest.TestCase):
    def test_build_continuation_and_recovery_use_exactly_once_dispatcher(self):
        workflow = (ROOT / ".github" / "workflows" / "chromium-i686.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(workflow.count("scripts/github_workflow_dispatch.py"), 2)
        self.assertIn("Start next stage on a fresh runner exactly once", workflow)
        self.assertIn("Redispatch failed stage exactly once", workflow)
        self.assertIn("CHROMIUM_VERSION", workflow)

    def test_terminal_build_failure_has_same_run_issue_mirror(self):
        workflow = (ROOT / ".github" / "workflows" / "chromium-i686.yml").read_text(encoding="utf-8")
        self.assertIn("report_terminal_failure:", workflow)
        self.assertIn("needs.recover_bad_runner.result != 'success'", workflow)
        self.assertIn("scripts/github_maintenance_issue.py", workflow)
        self.assertIn("issues: write", workflow)

    def test_preflight_dispatch_and_issue_use_central_helpers(self):
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/github_workflow_dispatch.py", preflight)
        self.assertIn("scripts/github_maintenance_issue.py", preflight)
        self.assertNotIn("gh workflow run chromium-i686.yml", preflight)

    def test_bootstrap_is_manual_release_guarded_and_exactly_once(self):
        bootstrap = (ROOT / ".github" / "workflows" / "bootstrap-i686-live.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", bootstrap)
        self.assertNotIn("push:", bootstrap)
        self.assertIn("gh release view", bootstrap)
        self.assertIn("bootstrap will never redispatch a released baseline", bootstrap)
        self.assertIn("scripts/github_workflow_dispatch.py", bootstrap)
        self.assertIn("CHROMIUM_VERSION", bootstrap)
        self.assertNotIn("\n          VERSION:", bootstrap)

    def test_secondary_reporter_covers_publisher_and_uses_central_issue_helper(self):
        reporter = (ROOT / ".github" / "workflows" / "report-i686-build-failure.yml").read_text(encoding="utf-8")
        self.assertIn("Publish Chromium i686 Release", reporter)
        self.assertIn("scripts/github_maintenance_issue.py", reporter)
        self.assertIn("timeout -k 10s 90s gh run view", reporter)

    def test_watcher_state_queries_are_bounded_and_include_publisher(self):
        watcher = (ROOT / "scripts" / "chromium_stable_watcher.py").read_text(encoding="utf-8")
        self.assertIn('"publish-i686-release.yml"', watcher)
        self.assertIn("RUN_HISTORY_DAYS", watcher)
        self.assertIn("RUN_HISTORY_MAX_PAGES", watcher)
        self.assertIn("REST_MAX_PAGES", watcher)
        self.assertIn("VERSION_API_MAX_PAGES", watcher)
        self.assertIn("Workflow history horizon saturated", watcher)
        self.assertIn("VersionHistory repeated a page token", watcher)
        self.assertIn("refusing to silently truncate", watcher)


if __name__ == "__main__":
    unittest.main()
