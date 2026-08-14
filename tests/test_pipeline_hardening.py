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

    def test_platform_detection_isolated_from_workflow_variables(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_I686_OS_RELEASE_FILE", common)
        self.assertIn("only in a subshell so platform probing can never overwrite caller/workflow state", common)
        self.assertIn("CHROMIUM_VERSION: ${{ inputs.version }}", preflight)
        self.assertNotIn("\n          VERSION: ${{ inputs.version }}", preflight)

    def test_preflight_failure_quarantine_has_independent_loop_prevention(self):
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        watcher = (ROOT / "scripts" / "chromium_stable_watcher.py").read_text(encoding="utf-8")
        self.assertIn("issues: write", preflight)
        self.assertIn("Quarantine failed preflight", preflight)
        self.assertIn("gh issue list --repo \"${GITHUB_REPOSITORY}\" --state open --limit 1000", preflight)
        self.assertIn("Failed workflow history is itself treated as quarantine state", preflight)
        self.assertIn("list_quarantined_run_versions", watcher)
        self.assertIn("issue_blocked | run_quarantined", watcher)
        self.assertIn("QUARANTINE_RUN_CONCLUSIONS", watcher)

    def test_runtime_resolver_does_not_mutate_errexit(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resolver = common[common.index("resolve_i386_package_for_soname()") : common.index("install_i386_runtime_libraries()") ]
        self.assertNotIn("set +e", resolver)
        self.assertNotIn("set -e", resolver)

    def test_large_cleanup_and_swap_operations_are_bounded(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_I686_REMOVE_TIMEOUT_SECONDS", common)
        self.assertIn("CHROMIUM_I686_SYSTEM_CLEANUP_TIMEOUT_SECONDS", common)
        self.assertIn("CHROMIUM_I686_SWAP_TIMEOUT_SECONDS", common)
        self.assertIn("bounded_sudo_rm_rf /usr/local/lib/android", common)
        self.assertIn('bounded_rm_rf "${CHROMIUM_SRC}"', common)
        self.assertIn('bounded_rm_rf "${OUT_DIR}"', resume)
        self.assertNotIn("sudo rm -rf /usr/local/lib/android", common)

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
        self.assertIn('VERSION="151.0.7922.108"', validation)
        self.assertIn('test "${VERSION}" = "${version_sentinel}"', validation)

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
        self.assertIn('if ! bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" zstd -q -t "${archive}"', resume)
        self.assertIn('Checkpoint archive SHA-256 verification failed.', resume)
        self.assertIn('Checkpoint manifest compatibility validation failed.', resume)

    def test_static_pin_policy_rejects_mutable_refs(self):
        workflow = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("@[0-9a-f]{40}", workflow)
        self.assertNotIn("@v[0-9]", workflow)


    def test_chromium_tooling_is_pinned_from_source_deps(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        self.assertIn("chromium_tool_pins.py", common)
        self.assertIn("depot_tools_revision", common)
        self.assertIn("chromium_gn_version", common)
        self.assertNotIn("cipd install gn/gn/linux-amd64 latest", common)
        self.assertIn("DEPOT_TOOLS_UPDATE=0", common)
        self.assertLess(action.index('prepare_chromium_source'), action.index('install_depot_tools'))
        self.assertLess(preflight.index('prepare_chromium_source'), preflight.index('install_depot_tools'))

    def test_source_archive_and_extracted_version_are_validated(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("validate_chromium_source_tarball()", common)
        self.assertIn("validate_extracted_chromium_version()", common)
        self.assertIn("--connect-timeout 30", common)
        self.assertIn("--max-time", common)
        self.assertIn("Discarding corrupt cached Chromium source archive", common)

    def test_build_tracks_upstream_linux_installer_runtime(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        port = (ROOT / ".github" / "scripts" / "chromium_i686_port.sh").read_text(encoding="utf-8")
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        self.assertIn('chrome/installer/linux:installer_deps', common)
        self.assertIn("chromium_linux_runtime.py", common)
        self.assertIn("validate_i686_runtime_bundle", common)
        self.assertIn("run_extended_i686_preflight", common)
        self.assertIn("run_extended_i686_preflight", preflight)
        self.assertIn("chrome_crashpad_handler", common)
        self.assertIn("libEGL.so", common)
        self.assertIn("libGLESv2.so", common)
        self.assertNotIn("run_extended_i686_preflight", port)

    def test_release_provenance_is_exact_and_immutable(self):
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn('grep -qx "github_sha=${BUILD_SHA}"', publish)
        self.assertIn('grep -qx "github_run_id=${BUILD_RUN_ID}"', publish)
        self.assertIn('--target "${BUILD_SHA}"', publish)
        self.assertIn("refusing to rewrite release history", publish)
        self.assertIn("refusing --clobber", publish)
        self.assertNotIn("gh release upload", publish)
        self.assertIn('"$(basename "${package}")"', common)
        self.assertIn("package_sha256=", common)

    def test_checkpoint_has_reserve_and_explicit_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "chromium-i686.yml").read_text(encoding="utf-8")
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_I686_CHECKPOINT_MINUTES || '330'", workflow)
        self.assertIn("CHECKPOINT_CONTRACT_VERSION", common)
        self.assertIn("checkpoint_contract_version", resume)
        self.assertIn("CHECKPOINT_GN_VERSION", resume)
        self.assertIn("CHECKPOINT_DEPOT_REVISION", resume)
        self.assertIn("CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS", resume)


    def test_latest_upstream_contract_is_probed_in_ci(self):
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("validate_upstream_contract:", validation)
        self.assertIn("resolve_latest_version", validation)
        self.assertIn("chromium_tool_pins.py", validation)
        self.assertIn("chromium_linux_runtime.py", validation)
        self.assertIn("install_depot_tools", validation)
        self.assertIn("install_gn_from_cipd", validation)
        self.assertIn("report_upstream_contract_drift:", validation)

    def test_publisher_avoids_generic_version_environment_name(self):
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_VERSION", publish)
        self.assertNotIn("\n          VERSION: ${{ steps.artifact.outputs.version }}", publish)


    def test_release_has_single_trusted_publication_boundary(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertNotIn("publish_chromium_release()", common)
        self.assertNotIn("Publish GitHub Release", action)
        self.assertNotIn("publish-release:", action)
        self.assertNotIn("gh release upload", common)
        self.assertNotIn("create_out_checkpoint()", common)


    def test_packaging_failures_are_classified_before_runner_retry(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_PACKAGE_FAILURE_CLASS", common)
        self.assertIn('CHROMIUM_PACKAGE_FAILURE_CLASS="infrastructure"', common)
        self.assertIn('id: package', action)
        self.assertIn("steps.package.outputs.failure_class", action)
        self.assertIn("if-no-files-found: error", action)


    def test_checkpoint_artifacts_precede_optional_cache_and_preserve_final_output(self):
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertNotIn("Restore ccache", action)
        self.assertNotIn("Save ccache", action)
        self.assertNotIn("chromium-i686-ccache-", action)
        self.assertIn("key: chromium-src-${{ inputs.version }}", action)
        self.assertIn("steps.source_cache.outputs.cache-hit != 'true'", action)
        self.assertNotIn("key: chromium-src-${{ inputs.version }}-${{ github.run_id }}", action)
        self.assertIn("Preserve completed output after packaging or artifact failure", action)
        self.assertIn("Upload Final Output Recovery Checkpoint", action)
        self.assertIn("steps.build_artifact.outcome == 'failure'", action)
        self.assertIn("steps.final_recovery.outputs.failure_class", action)

    def test_release_workflow_supports_trusted_manual_republish(self):
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", publish)
        self.assertIn("build_run_id:", publish)
        self.assertIn("Resolve and verify trusted build source", publish)
        self.assertIn('workflow_path}" = ".github/workflows/chromium-i686.yml"', publish)
        self.assertIn('head_branch}" = "${DEFAULT_BRANCH}"', publish)
        self.assertIn('head_repo}" = "${GITHUB_REPOSITORY}"', publish)
        self.assertIn("has no retained final Chromium runtime artifact", publish)

    def test_host_optional_probes_cannot_hang_or_require_swap(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("continuing without swap", common)
        self.assertIn("capture_ldd_output()", common)
        self.assertIn("timeout -k 3s 15s ldd", common)
        self.assertIn("timeout -k 10s 120s ccache --cleanup", common)

    def test_standalone_runtime_requires_rendered_wrapper(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts" / "chromium_linux_runtime.py").read_text(encoding="utf-8")
        self.assertIn("chrome-wrapper", common)
        self.assertIn("render_standalone_wrapper", runtime)
        self.assertIn("@@PROGNAME", runtime)
        self.assertIn("@@channel", runtime)
        self.assertIn("--render-wrapper", common)


if __name__ == "__main__":
    unittest.main()
