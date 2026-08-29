import hashlib
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

    def test_active_versions_include_release_handoff_title(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "publish-i686-release-handoff.yml":
                return [{
                    "status": "in_progress",
                    "conclusion": "",
                    "display_title": "Handoff Chromium i686 155.0.1.2 from build run 123456",
                }]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            self.assertEqual(watcher.list_active_versions("owner/repository"), {"155.0.1.2"})

    def test_failed_release_handoff_quarantines_production_version(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "publish-i686-release-handoff.yml":
                return [{
                    "status": "completed",
                    "conclusion": "failure",
                    "display_title": "Handoff Chromium i686 155.0.1.2 from build run 123456",
                    "head_branch": "main",
                }]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            active, quarantined = watcher.list_port_run_state("owner/repository", "main")
        self.assertEqual(active, set())
        self.assertEqual(quarantined, {"155.0.1.2"})

    def test_active_versions_include_manual_publisher_title(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "publish-i686-release.yml":
                return [{
                    "status": "in_progress",
                    "conclusion": "",
                    "display_title": "Publish Chromium i686 155.0.1.2 from build run 123456",
                }]
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

    def test_active_unparseable_control_run_fails_closed(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "chromium-i686.yml":
                return [{
                    "status": "in_progress",
                    "conclusion": "",
                    "display_title": "Chromium build without version",
                    "head_branch": "main",
                }]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            with self.assertRaises(watcher.WatcherError):
                watcher.list_port_run_state("owner/repository", "main")

    def test_windows_lane_parses_active_staged_build_title(self):
        def fake_runs(_repository, workflow, **_kwargs):
            if workflow == "chromium-windows-i686.yml":
                return [
                    {
                        "status": "in_progress",
                        "conclusion": "",
                        "display_title": (
                            "Chromium Windows i686 153.0.8010.12 - stage 6 - attempt 0"
                        ),
                        "head_branch": "main",
                    }
                ]
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            active, quarantined = watcher.list_port_run_state(
                "owner/repository", "main", watcher.WINDOWS_LANE
            )
        self.assertEqual(active, {"153.0.8010.12"})
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

    def test_filtered_workflow_history_rejects_ignored_status_filter(self):
        wrong = {
            "workflow_runs": [
                {
                    "status": "completed",
                    "conclusion": "success",
                    "display_title": "Chromium i686 155.0.1.2 - stage 2 - attempt 0",
                }
            ]
        }
        with mock.patch.object(watcher, "gh_json", return_value=wrong), self.assertRaises(watcher.WatcherError):
            watcher.list_workflow_runs(
                "owner/repository",
                "chromium-i686.yml",
                created_after=watcher.datetime.now(watcher.timezone.utc),
                status_filter="failure",
            )

    def test_filtered_workflow_history_uses_status_parameter(self):
        with mock.patch.object(watcher, "gh_json", return_value={"workflow_runs": []}) as gh_json:
            watcher.list_workflow_runs(
                "owner/repository",
                "chromium-i686.yml",
                created_after=watcher.datetime.now(watcher.timezone.utc),
                status_filter="cancelled",
            )
        endpoint = gh_json.call_args.args[0][-1]
        self.assertIn("status=cancelled", endpoint)

    def test_port_state_queries_only_active_and_terminal_filters(self):
        calls = []

        def fake_runs(_repository, workflow, **kwargs):
            calls.append((workflow, kwargs.get("status_filter")))
            return []

        with mock.patch.object(watcher, "list_workflow_runs", side_effect=fake_runs):
            watcher.list_port_run_state("owner/repository", "main")
        expected_filters = watcher.ACTIVE_RUN_STATES | watcher.QUARANTINE_RUN_CONCLUSIONS
        self.assertEqual(
            set(calls),
            {(workflow, state) for workflow in watcher.PORT_WORKFLOWS for state in expected_filters},
        )
        self.assertNotIn(("chromium-i686.yml", None), calls)
        self.assertNotIn(("chromium-i686.yml", "success"), calls)

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

    def test_exact_full_workflow_history_horizon_with_empty_sentinel_succeeds(self):
        full_page = {"workflow_runs": [{"display_title": "old"}] * 100}
        empty_page = {"workflow_runs": []}
        with mock.patch.object(watcher, "gh_json", side_effect=[full_page, full_page, empty_page]) as gh_json:
            runs = watcher.list_workflow_runs(
                "owner/repository",
                "chromium-i686.yml",
                created_after=watcher.datetime.now(watcher.timezone.utc),
                max_pages=2,
            )
        self.assertEqual(len(runs), 200)
        self.assertEqual(gh_json.call_count, 3)

    def test_gh_timeout_becomes_watcher_error(self):
        with mock.patch.object(
            watcher.subprocess,
            "run",
            side_effect=watcher.subprocess.TimeoutExpired(["gh"], 1),
        ):
            with self.assertRaises(watcher.WatcherError):
                watcher.run_gh(["api", "repos/owner/repository"], timeout=1)

    def test_healthy_release_supersedes_historical_run_quarantine(self):
        version = "155.0.1.2"
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        with mock.patch.object(watcher, "fetch_stable_versions", return_value=[version]), \
             mock.patch.object(watcher, "list_blocked_versions", return_value=set()), \
             mock.patch.object(watcher, "list_port_run_state", return_value=(set(), {version})), \
             mock.patch.object(watcher, "list_release_health", return_value=({version}, set())), \
             mock.patch.object(watcher, "source_object_is_ready") as source_ready, \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = watcher.main([
                "--repository", "owner/repo", "--ref", "main", "--dry-run", "--baseline", baseline
            ])
        self.assertEqual(rc, 0)
        source_ready.assert_not_called()
        dispatch_call.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Recent terminal-run quarantines: `0`", output)
        self.assertIn("Historical run quarantines superseded by healthy releases: `1`", output)
        self.assertIn("Total blocked versions: `0`", output)

    def test_unreleased_terminal_failure_remains_live_quarantine(self):
        version = "155.0.1.2"
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        with mock.patch.object(watcher, "fetch_stable_versions", return_value=[version]), \
             mock.patch.object(watcher, "list_blocked_versions", return_value=set()), \
             mock.patch.object(watcher, "list_port_run_state", return_value=(set(), {version})), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), set())), \
             mock.patch.object(watcher, "source_object_is_ready") as source_ready, \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = watcher.main([
                "--repository", "owner/repo", "--ref", "main", "--dry-run", "--baseline", baseline
            ])
        self.assertEqual(rc, 0)
        source_ready.assert_not_called()
        dispatch_call.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Recent terminal-run quarantines: `1`", output)
        self.assertIn("Historical run quarantines superseded by healthy releases: `0`", output)
        self.assertIn("Total blocked versions: `1`", output)

    def test_source_readiness_distinguishes_pending_from_api_failure(self):
        with mock.patch.object(
            watcher, "fetch_source_metadata", side_effect=watcher.SourceObjectNotFound("pending")
        ):
            self.assertFalse(watcher.source_object_is_ready("155.0.1.2"))
        with mock.patch.object(watcher, "fetch_source_metadata", side_effect=ValueError("503")), \
             self.assertRaises(watcher.WatcherError):
            watcher.source_object_is_ready("155.0.1.2")

    def test_source_pending_version_is_deferred_without_preflight_dispatch(self):
        version = "155.0.1.2"
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        with mock.patch.object(watcher, "fetch_stable_versions", return_value=[version]), \
             mock.patch.object(watcher, "list_blocked_versions", return_value=set()), \
             mock.patch.object(watcher, "list_port_run_state", return_value=(set(), set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), set())), \
             mock.patch.object(watcher, "source_object_is_ready", return_value=False), \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = watcher.main([
                "--repository", "owner/repo", "--ref", "main", "--dry-run", "--baseline", baseline
            ])
        self.assertEqual(rc, 0)
        dispatch_call.assert_not_called()
        self.assertIn("source object is not published yet", stdout.getvalue())
        self.assertIn("Stable versions waiting for source publication: `1`", stdout.getvalue())

    def test_pending_older_source_does_not_hide_later_ready_version(self):
        older, newer = "155.0.1.2", "155.0.1.3"
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        readiness = {older: False, newer: True}
        with mock.patch.object(watcher, "fetch_stable_versions", return_value=[newer, older]), \
             mock.patch.object(watcher, "list_blocked_versions", return_value=set()), \
             mock.patch.object(watcher, "list_port_run_state", return_value=(set(), set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), set())), \
             mock.patch.object(watcher, "source_object_is_ready", side_effect=lambda v: readiness[v]), \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call:
            rc = watcher.main([
                "--repository", "owner/repo", "--ref", "main", "--dry-run", "--baseline", baseline
            ])
        self.assertEqual(rc, 0)
        dispatch_call.assert_called_once()
        self.assertEqual(dispatch_call.call_args.args[2], newer)

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

    def test_preflight_dispatch_can_bind_to_watcher_workflow_sha(self):
        expected_sha = "a" * 40
        with mock.patch.object(watcher, "dispatch_once", return_value="accepted-confirmed") as dispatch_call:
            watcher.dispatch_preflight(
                "owner/repository", "main", "155.0.1.2", False, expected_sha
            )
        dispatch_call.assert_called_once_with(
            "owner/repository",
            "chromium-i686-preflight.yml",
            "main",
            "Chromium i686 preflight 155.0.1.2",
            ["version=155.0.1.2", "dispatch_build=true"],
            expected_head_sha=expected_sha,
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

    def test_exact_full_rest_horizon_with_empty_sentinel_succeeds(self):
        full = [{}] * 100
        with mock.patch.object(watcher, "gh_json", side_effect=[full, full, []]) as gh_json:
            items = watcher.list_rest_items("owner/repo", "releases", max_pages=2)
        self.assertEqual(len(items), 200)
        self.assertEqual(gh_json.call_count, 3)

    @staticmethod
    def _healthy_release(version: str, **overrides):
        release = {
            "tag_name": f"chromium-{version}-linux-i686",
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "assets": [
                {
                    "id": 101,
                    "name": f"chromium-{version}-linux-i686.tar.xz",
                    "state": "uploaded",
                    "size": 100,
                    "digest": "sha256:" + "a" * 64,
                },
                {
                    "id": 102,
                    "name": f"chromium-{version}-linux-i686.tar.xz.sha256",
                    "state": "uploaded",
                    "size": 100,
                    "digest": "sha256:" + "b" * 64,
                },
                {
                    "id": 103,
                    "name": f"chromium-{version}-linux-i686-manifest.txt",
                    "state": "uploaded",
                    "size": 100,
                    "digest": "sha256:" + "c" * 64,
                },
            ],
        }
        release.update(overrides)
        return release

    def test_release_health_requires_immutability_outside_legacy_allowlist(self):
        version = "155.0.0.1"
        mutable = self._healthy_release(version, immutable=False)
        with mock.patch.object(watcher, "list_rest_items", return_value=[mutable]):
            healthy, broken = watcher.list_release_health("owner/repo")
        self.assertEqual(healthy, set())
        self.assertEqual(broken, {version})

    def test_legacy_mutable_release_is_explicitly_grandfathered(self):
        version = "151.0.7922.108"
        mutable = self._healthy_release(version, immutable=False)
        with mock.patch.object(watcher, "list_rest_items", return_value=[mutable]), \
             mock.patch.object(watcher, "verify_release_provenance", return_value=None):
            healthy, broken = watcher.list_release_health("owner/repo", {version})
            versions = watcher.list_release_versions("owner/repo", {version})
        self.assertEqual(healthy, {version})
        self.assertEqual(broken, set())
        self.assertEqual(versions, {version})

    def test_baseline_legacy_mutable_release_policy_is_strict_and_explicit(self):
        baseline = pathlib.Path(__file__).parents[1] / "support" / "baseline.json"
        minimum, known, legacy = watcher.load_baseline(baseline)
        self.assertEqual(minimum, "150.0.7871.186")
        self.assertIn("150.0.7871.186", known)
        self.assertEqual(
            legacy,
            {"150.0.7871.186", "151.0.7922.71", "151.0.7922.75", "151.0.7922.108"},
        )

    @staticmethod
    def _release_manifest(version: str, build_sha: str) -> str:
        return (
            f"manifest_schema=2\nversion={version}\ntarget_cpu=x86\ntarget_os=linux\n"
            f"github_sha={build_sha}\n\npackaged_files:\nchrome\n"
        )

    def test_legacy_release_manifest_file_list_is_accepted_after_required_metadata(self):
        version = "151.0.7922.75"
        build_sha = "a" * 40
        manifest = (
            f"version={version}\ntarget_cpu=x86\ntarget_os=linux\ngithub_sha={build_sha}\n"
            ".gitignore\nchrome\n"
        )
        raw = manifest.encode("utf-8")
        asset = {
            "id": 103,
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        with mock.patch.object(watcher, "gh_resource_text", return_value=manifest):
            self.assertEqual(
                watcher.read_release_manifest_build_sha("owner/repo", version, asset), build_sha
            )

    def test_legacy_release_manifest_cannot_enter_file_list_before_required_metadata(self):
        version = "151.0.7922.75"
        manifest = f"version={version}\ntarget_cpu=x86\n.gitignore\ngithub_sha={'a' * 40}\n"
        raw = manifest.encode("utf-8")
        asset = {
            "id": 103,
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        with mock.patch.object(watcher, "gh_resource_text", return_value=manifest), \
             self.assertRaisesRegex(ValueError, "malformed metadata"):
            watcher.read_release_manifest_build_sha("owner/repo", version, asset)

    def test_release_provenance_cross_binds_manifest_digest_and_tag(self):
        version = "155.0.0.1"
        build_sha = "a" * 40
        manifest = self._release_manifest(version, build_sha)
        raw = manifest.encode("utf-8")
        asset = {
            "id": 103,
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        with mock.patch.object(
            watcher, "gh_resource_text", side_effect=[manifest, json.dumps({"sha": build_sha})]
        ) as read:
            watcher.verify_release_provenance("owner/repo", version, asset)
        self.assertEqual(read.call_count, 2)
        self.assertIn("Accept: application/octet-stream", read.call_args_list[0].args[0])
        self.assertIn(f"commits/chromium-{version}-linux-i686", read.call_args_list[1].args[0][-1])

    def test_release_health_rejects_tag_manifest_mismatch(self):
        version = "155.0.0.1"
        manifest_sha = "a" * 40
        tag_sha = "b" * 40
        manifest = self._release_manifest(version, manifest_sha)
        raw = manifest.encode("utf-8")
        release = self._healthy_release(version)
        manifest_asset = release["assets"][2]
        manifest_asset["size"] = len(raw)
        manifest_asset["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        with mock.patch.object(watcher, "list_rest_items", return_value=[release]), \
             mock.patch.object(
                 watcher, "gh_resource_text", side_effect=[manifest, json.dumps({"sha": tag_sha})]
             ):
            healthy, broken = watcher.list_release_health("owner/repo")
        self.assertEqual(healthy, set())
        self.assertEqual(broken, {version})

    def test_release_manifest_digest_mismatch_is_rejected_before_tag_lookup(self):
        version = "155.0.0.1"
        manifest = self._release_manifest(version, "a" * 40)
        raw = manifest.encode("utf-8")
        asset = {"id": 103, "size": len(raw), "digest": "sha256:" + "0" * 64}
        with mock.patch.object(watcher, "gh_resource_text", return_value=manifest) as read, \
             self.assertRaisesRegex(ValueError, "digest changed"):
            watcher.verify_release_provenance("owner/repo", version, asset)
        self.assertEqual(read.call_count, 1)

    def test_release_manifest_size_bound_precedes_download(self):
        asset = {
            "id": 103,
            "size": watcher.RELEASE_MANIFEST_MAX_BYTES + 1,
            "digest": "sha256:" + "a" * 64,
        }
        with mock.patch.object(watcher, "gh_resource_text") as read, \
             self.assertRaisesRegex(ValueError, "watcher limit"):
            watcher.read_release_manifest_build_sha("owner/repo", "155.0.0.1", asset)
        read.assert_not_called()

    def test_release_provenance_network_uncertainty_propagates(self):
        release = self._healthy_release("155.0.0.1")
        with mock.patch.object(watcher, "list_rest_items", return_value=[release]), \
             mock.patch.object(
                 watcher, "verify_release_provenance", side_effect=watcher.WatcherError("rate limit")
             ), self.assertRaisesRegex(watcher.WatcherError, "rate limit"):
            watcher.list_release_health("owner/repo")

    def test_draft_release_does_not_mark_version_released(self):
        releases = [
            self._healthy_release("151.0.0.1", draft=True),
            self._healthy_release("152.0.0.1"),
        ]
        with mock.patch.object(watcher, "list_rest_items", return_value=releases), \
             mock.patch.object(watcher, "verify_release_provenance", return_value=None):
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


    def test_active_publisher_draft_is_transient_not_broken(self):
        version = "155.0.1.2"
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        with mock.patch.object(watcher, "fetch_stable_versions", return_value=[version]), \
             mock.patch.object(watcher, "list_blocked_versions", return_value=set()), \
             mock.patch.object(watcher, "list_port_run_state", return_value=({version}, set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), {version})), \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call:
            rc = watcher.main([
                "--repository", "owner/repo",
                "--ref", "main",
                "--dry-run",
                "--baseline", baseline,
            ])
        self.assertEqual(rc, 0)
        dispatch_call.assert_not_called()

    def test_broken_other_release_still_fails_while_another_version_is_active(self):
        active_version = "155.0.1.2"
        broken_version = "156.0.1.2"
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        with mock.patch.object(watcher, "fetch_stable_versions", return_value=[active_version, broken_version]), \
             mock.patch.object(watcher, "list_blocked_versions", return_value=set()), \
             mock.patch.object(watcher, "list_port_run_state", return_value=({active_version}, set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), {broken_version})):
            with self.assertRaises(watcher.WatcherError):
                watcher.main([
                    "--repository", "owner/repo",
                    "--ref", "main",
                    "--dry-run",
                    "--baseline", baseline,
                ])

    def test_force_version_does_not_bypass_active_port_ownership(self):
        with mock.patch.object(watcher, "list_port_run_state", return_value=({"154.0.0.1"}, set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), set())), \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call:
            rc = watcher.main([
                "--repository", "owner/repo",
                "--force-version", "155.0.0.1",
                "--dry-run",
                "--baseline", str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json"),
            ])
        self.assertEqual(rc, 0)
        dispatch_call.assert_not_called()

    def test_force_version_cannot_replace_healthy_release(self):
        with mock.patch.object(watcher, "list_port_run_state", return_value=(set(), set())), \
             mock.patch.object(watcher, "list_release_health", return_value=({"155.0.0.1"}, set())), \
             mock.patch.object(watcher, "dispatch_preflight") as dispatch_call:
            rc = watcher.main([
                "--repository", "owner/repo",
                "--force-version", "155.0.0.1",
                "--dry-run",
                "--baseline", str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json"),
            ])
        self.assertEqual(rc, 0)
        dispatch_call.assert_not_called()

    def test_force_version_refuses_broken_release_state(self):
        with mock.patch.object(watcher, "list_port_run_state", return_value=(set(), set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), {"155.0.0.1"})):
            with self.assertRaises(watcher.WatcherError):
                watcher.main([
                    "--repository", "owner/repo",
                    "--force-version", "155.0.0.1",
                    "--dry-run",
                    "--baseline", str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json"),
                ])

    def test_force_version_refuses_baseline_or_older(self):
        baseline = str(pathlib.Path(__file__).parents[1] / "support" / "baseline.json")
        with mock.patch.object(watcher, "list_port_run_state", return_value=(set(), set())), \
             mock.patch.object(watcher, "list_release_health", return_value=(set(), set())):
            with self.assertRaises(watcher.WatcherError):
                watcher.main([
                    "--repository", "owner/repo",
                    "--force-version", "150.0.7871.186",
                    "--dry-run",
                    "--baseline", baseline,
                ])


if __name__ == "__main__":
    unittest.main()
