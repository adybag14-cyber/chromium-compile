import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).parents[1]


class PipelineHardeningTests(unittest.TestCase):
    def test_first_party_actions_are_immutable_pins(self):
        refs = []
        paths = sorted(
            path
            for pattern in ("*.yml", "*.yaml")
            for path in (ROOT / ".github").rglob(pattern)
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            refs.extend(re.findall(r"uses:\s+(actions/[^@\s]+)@([^\s#]+)", text))
        self.assertTrue(refs)
        for action, ref in refs:
            self.assertRegex(ref, r"^[0-9a-f]{40}$", f"{action} is not pinned to a commit SHA")

    def test_embedded_python_heredocs_parse(self):
        for rel in (
            ".github/scripts/chromium_i686_common.sh",
            ".github/scripts/chromium_i686_resume.sh",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            blocks = re.findall(r"python3[^\n]*<<'PY'\n(.*?)\nPY(?:\n|$)", text, re.DOTALL)
            self.assertTrue(blocks, f"no embedded Python blocks found in {rel}")
            for block in blocks:
                ast.parse(block)

    def test_checkpoint_bundle_has_integrity_metadata(self):
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("out-Release_x86.tar.zst.sha256", action)
        self.assertIn("checkpoint-manifest.json", action)
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn("source_tar_sha256", resume)
        self.assertIn("port_config_sha256", resume)
        self.assertIn("build_ninja_sha256", resume)

    def test_deterministic_build_failures_do_not_consume_runner_retry(self):
        workflow = (ROOT / ".github" / "workflows" / "chromium-i686.yml").read_text(encoding="utf-8")
        self.assertIn("needs.build.outputs.failure_class != 'deterministic_build'", workflow)

    def test_runtime_repair_covers_the_observed_stage3_sonames(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        for soname in (
            "libnspr4.so",
            "libnss3.so",
            "libnssutil3.so",
            "libsmime3.so",
            "libdbus-1.so.3",
            "libX11.so.6",
            "libXext.so.6",
            "libgbm.so.1",
            "libxcb.so.1",
            "libxkbcommon.so.0",
            "libudev.so.1",
            "libasound.so.2",
            "libQt5Core.so.5",
            "libQt5Gui.so.5",
            "libQt5Widgets.so.5",
        ):
            self.assertIn(f"[{soname}]", common)


    def test_runtime_repair_can_discover_future_i386_sonames(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("resolve_i386_package_for_soname()", common)
        self.assertIn("ensure_apt_file_i386_metadata()", common)
        self.assertIn("apt-file --filter-origins Ubuntu -a i386", common)
        self.assertIn("APT::Architecture=i386", common)
        self.assertIn("APT::Architectures::=i386", common)
        self.assertIn("Multiple Ubuntu i386 packages provide", common)
        self.assertIn("No installable Ubuntu i386 package provides", common)
        self.assertIn("for round in 1 2 3", common)
        self.assertIn("I386_BASELINE_SONAMES=(", common)
        self.assertIn('resolve_i386_package_for_soname "${soname}"', common)

    def test_release_local_time64_package_variants_are_supported(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("i386_package_variants()", common)
        self.assertIn('"${base}t64:i386"', common)
        self.assertIn("Release-local i386 runtime mapping", common)

    def test_runtime_scan_excludes_shared_target_objects(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("is_i386_host_executable()", common)
        self.assertIn("shared target objects are intentionally excluded", common)
        self.assertIn("(pie )?executable", common)

    def test_runtime_resolver_does_not_mutate_errexit(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resolver = common[common.index("resolve_i386_package_for_soname()") : common.index("install_i386_runtime_libraries()") ]
        self.assertNotIn("set +e", resolver)
        self.assertNotIn("set -e", resolver)

    def test_runtime_discovery_and_apt_operations_are_bounded(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_I686_APT_TIMEOUT_SECONDS", common)
        self.assertIn("CHROMIUM_I686_DISCOVERY_TIMEOUT_SECONDS", common)
        self.assertIn("CHROMIUM_I686_APT_FILE_SEARCH_TIMEOUT_SECONDS", common)
        self.assertIn("timeout -k 20s", common)
        self.assertIn("refusing to burn a fresh runner retry", common)
        self.assertIn("classify_apt_file_search_status()", common)
        self.assertIn("resolver syntax/tooling requires maintenance", common)
        self.assertIn("apt-file search failed or timed out", common)

    def test_non_build_orchestration_is_not_pinned_to_old_lts(self):
        for rel in (
            ".github/workflows/bootstrap-i686-live.yml",
            ".github/workflows/publish-i686-release.yml",
            ".github/workflows/report-i686-build-failure.yml",
            ".github/workflows/watch-chromium-stable.yml",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("runs-on: ubuntu-22.04", text, rel)
            self.assertIn("runs-on: ubuntu-latest", text, rel)

    def test_lts_matrix_and_configurable_production_runner_exist(self):
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        build = (ROOT / ".github" / "workflows" / "chromium-i686.yml").read_text(encoding="utf-8")
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        self.assertIn("ubuntu-22.04", validation)
        self.assertIn("ubuntu-24.04", validation)
        self.assertIn("ubuntu-latest", validation)
        self.assertIn("maximize_runner_disk_space", validation)
        self.assertIn("install_system_dependencies", validation)
        self.assertIn("schedule:", validation)
        self.assertIn("report_lts_drift:", validation)
        self.assertIn("Ubuntu LTS compatibility drift", validation)
        self.assertIn("CHROMIUM_I686_RUNNER", build)
        self.assertIn("CHROMIUM_I686_RUNNER", preflight)

    def test_runtime_failure_uses_exact_failed_tool_before_scanning(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("repair_i386_runtime_from_build_log()", common)
        self.assertIn("error while loading shared libraries", common)
        self.assertIn('repair_i386_runtime_from_build_log "${pass_log}"', common)
        self.assertIn("for pass in 1 2 3", common)
        self.assertIn('runtime_repairs}" -lt 2', common)

    def test_configurable_runner_package_installs_are_bounded(self):
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("bounded_sudo_apt_get install -y file binutils", preflight)
        self.assertIn("bounded_sudo_apt_get install -y --no-install-recommends gcc-multilib file binutils", validation)
        self.assertIn('ldd_output="$(ldd "${RUNNER_TEMP}/lts-i386-canary")"', validation)

    def test_prepare_propagates_runtime_repair_failure_class(self):
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("I386_RUNTIME_REPAIR_FAILURE_CLASS:-runtime_environment", action)
        self.assertIn("steps.runtime.outputs.failure_class", action)

    def test_linux_ci_exercises_generic_soname_discovery(self):
        workflow = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("resolve_i386_package_for_soname libQt5Core.so.5", workflow)
        self.assertIn("libqt5core5a:i386", workflow)
        self.assertIn("resolve_i386_package_for_soname libQt5Widgets.so.5", workflow)
        self.assertIn("libqt5widgets5:i386", workflow)
        self.assertIn("resolve_i386_package_for_soname libQt5Network.so.5", workflow)
        self.assertIn("libqt5network5:i386", workflow)

    def test_early_termination_is_not_mistaken_for_checkpoint_timeout(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn('status}" -eq 137', common)
        self.assertIn('now}" -ge "${cutoff}', common)
        self.assertIn('failure_class=infrastructure', common)

    def test_preflight_checks_i386_host_runtime(self):
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        self.assertIn("install_i386_runtime_libraries", preflight)

    def test_checkout_does_not_persist_credentials(self):
        for pattern in ("*.yml", "*.yaml"):
            for path in (ROOT / ".github").rglob(pattern):
                text = path.read_text(encoding="utf-8")
                if "uses: actions/checkout@" in text:
                    self.assertIn("persist-credentials: false", text, str(path))

    def test_legacy_checkpoint_requires_version_scoped_opt_in(self):
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn('ALLOW_LEGACY_CHECKPOINT_VERSION', resume)
        self.assertIn('Legacy checkpoint lacks integrity metadata', resume)

    def test_build_failure_classification_is_per_pass(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn('pass_log_start=', common)
        self.assertIn('classify_build_failure "${pass_log}"', common)

    def test_checkpoint_restore_is_not_duplicated(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertNotIn("restore_out_checkpoint()", common)
        self.assertEqual(resume.count("restore_out_checkpoint()"), 1)

    def test_fallback_checkpoint_is_downloaded_on_demand(self):
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertNotIn("- name: Try Fallback Checkpoint", action)
        self.assertIn("Preferred checkpoint unavailable or invalid; downloading fallback checkpoint on demand.", action)
        self.assertIn('restore_out_checkpoint "${resume_archive}" "${{ inputs.version }}" "${{ inputs.stage }}" true', action)
        self.assertIn("Fallback checkpoint download failed; continuing with fresh output/ccache", action)

    def test_checkpoint_integrity_failures_return_immediately(self):
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn('if ! zstd -q -t "${archive}"', resume)
        self.assertIn('Checkpoint archive SHA-256 verification failed.', resume)
        self.assertIn('Checkpoint manifest compatibility validation failed.', resume)

    def test_static_pin_policy_rejects_mutable_refs(self):
        workflow = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("@[0-9a-f]{40}", workflow)
        self.assertNotIn("@v[0-9]", workflow)


if __name__ == "__main__":
    unittest.main()
