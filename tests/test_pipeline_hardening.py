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
        self.assertIn("scripts/github_maintenance_issue.py", preflight)
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
        self.assertIn('bounded_rm_rf "${restore_root}"', resume)
        self.assertIn('bounded_rm_rf "${backup_out}"', resume)
        self.assertNotIn('bounded_rm_rf "${OUT_DIR}"', resume)
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


    def test_native_build_dependency_failures_are_classified(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("NATIVE_BUILD_PACKAGES", common)
        self.assertIn("SYSTEM_DEPENDENCY_FAILURE_CLASS", common)
        self.assertIn("bounded_apt_get_simulate install -y", common)
        self.assertIn("Native Chromium build prerequisites are not solvable", common)
        self.assertIn("id: system_dependencies", action)
        self.assertIn("steps.system_dependencies.outputs.failure_class", action)
        self.assertIn("bash tests/test_system_dependencies.sh", validation)

    def test_configurable_runner_package_installs_are_bounded(self):
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("bounded_sudo_apt_get install -y file binutils", preflight)
        self.assertIn("bounded_sudo_apt_get install -y --no-install-recommends gcc-multilib file binutils", validation)
        self.assertIn('ldd_output="$(ldd "${RUNNER_TEMP}/lts-i386-canary")"', validation)

    def test_post_compile_artifact_boundaries_are_fail_closed(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("RELEASE_ARCHIVE_FAILURE_CLASS", common)
        self.assertIn("RELEASE_ARCHIVE_EXTRACT_FAILURE_CLASS", common)
        self.assertIn("Could not clear stale Chromium release outputs", common)
        self.assertIn("Could not hash packaged Chromium archive", common)
        self.assertIn("Could not write Chromium release provenance manifest", common)
        package = common[common.index("package_chromium_i686()"):]
        self.assertLess(package.index("CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure"), package.index("local package_sha source_sha"))
        self.assertLess(package.index("local package_sha source_sha"), package.index("CHROMIUM_PACKAGE_FAILURE_CLASS=deterministic_build", package.index("local release_stats")))
        self.assertIn('tar -cJf "${package}" -- "${files[@]}"', common)
        self.assertIn('ls -lh "${package}" "${checksum}" "${manifest}" || true', common)
        self.assertIn("CHECKPOINT_CREATE_FAILURE_CLASS", resume)
        self.assertIn("Checkpoint archive creation failed with status", resume)
        self.assertIn("Produced checkpoint archive failed structural/resource validation", resume)
        self.assertIn("Could not create/verify checkpoint SHA-256 sidecar", resume)
        self.assertIn("Could not write checkpoint manifest", resume)
        create = resume[resume.index("create_out_checkpoint()"):]
        self.assertLess(create.index("CHECKPOINT_CREATE_FAILURE_CLASS=infrastructure"), create.index("ensure_build_disk_space 10"))
        self.assertIn('failure_class=${CHECKPOINT_CREATE_FAILURE_CLASS:-infrastructure}', action)
        self.assertIn("id: checkpoint_artifact_failure", action)
        self.assertIn("id: final_recovery_artifact", action)
        self.assertIn("id: recovery_artifact_failure", action)
        output_expr = action[action.index("outputs:"):action.index("runs:")]
        self.assertLess(output_expr.index("steps.recovery_artifact_failure.outputs.failure_class"), output_expr.index("steps.package.outputs.failure_class"))
        self.assertLess(output_expr.index("steps.checkpoint_artifact_failure.outputs.failure_class"), output_expr.index("steps.checkpoint.outputs.failure_class"))
        self.assertIn('log_runner_storage "stage-${{ inputs.stage }}-${{ inputs.attempt }} after compile" || true', action)
        self.assertIn("bash tests/test_post_compile_artifact_integrity.sh", validation)

    def test_prepare_pipeline_classifies_setup_and_restore_failures(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        port = (ROOT / ".github" / "scripts" / "chromium_i686_port.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_PREPARE_FAILURE_CLASS", common)
        self.assertIn("CHROMIUM_I686_HARD_MAX_RESERVE_GIB=64", common)
        self.assertIn("validate_bounded_reserve_gib()", common)
        self.assertIn("CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB", common)
        self.assertIn("CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB", common)
        self.assertIn("CHROMIUM_I686_CHECKPOINT_RESTORE_RESERVE_GIB", resume)
        self.assertIn("validate_bounded_reserve_gib", resume)
        self.assertIn("classify_prepare_command_status()", common)
        self.assertIn("Chromium-pinned depot_tools", common)
        self.assertIn("Chromium-pinned Clang installation failed", common)
        self.assertIn("sysroot installer succeeded without an i386 sysroot", common)
        self.assertIn("Chromium-pinned GN CIPD install failed", common)
        self.assertIn("Chromium i686 GN graph generation failed", common)
        self.assertIn('bounded_external "${CHROMIUM_I686_TOOLCHAIN_TIMEOUT_SECONDS}"', common)
        self.assertIn("Common semantic Linux i686 patch no longer applies", port)
        self.assertIn("CHECKPOINT_BUNDLE_FAILURE_CLASS", resume)
        self.assertIn("CHECKPOINT_RESTORE_FAILURE_CLASS", resume)
        self.assertIn("classify_prepare_command_status", resume)
        self.assertIn("Could not normalize Chromium source file mtimes", resume)
        self.assertIn("fail_prepare_step()", action)
        self.assertIn("steps.runner_setup.outputs.failure_class", action)
        self.assertIn("steps.job_start.outputs.failure_class", action)
        self.assertIn("CHECKPOINT_RESTORE_FAILURE_CLASS:-infrastructure", action)
        self.assertIn("CHECKPOINT_BUNDLE_FAILURE_CLASS:-deterministic_build", action)
        self.assertIn("Preferred checkpoint validation failed due to runner/tool infrastructure", action)
        self.assertIn("Preferred checkpoint download failed after provenance succeeded", action)
        self.assertIn("Fallback checkpoint validation failed due to runner/tool infrastructure", action)
        self.assertIn("CHROMIUM_PREPARE_FAILURE_CLASS:-deterministic_build", action)
        self.assertGreaterEqual(action.count("CHROMIUM_PREPARE_FAILURE_CLASS"), 8)
        self.assertIn('log_runner_storage "stage-${{ inputs.stage }}-${{ inputs.attempt }} initial" || true', action)
        self.assertIn("bash tests/test_prepare_failure_classification.sh", validation)

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
        self.assertNotIn("- name: Try Preferred Checkpoint", action)
        self.assertIn("Preferred checkpoint provenance accepted; downloading trusted artifact on demand.", action)
        self.assertIn("Preferred checkpoint unavailable or invalid; validating fallback provenance before download.", action)
        self.assertIn("Fallback provenance accepted; downloading checkpoint on demand.", action)
        self.assertLess(action.index("validate_checkpoint_source_run"), action.index("bounded_gh run download"))
        self.assertIn("fallback_rc", action)
        self.assertIn("preferred_rc", action)
        self.assertIn("resume_producer_sha", action)
        self.assertIn("resume_producer_run_attempt", action)
        self.assertIn('"${resume_producer_run_id}" "${resume_producer_run_attempt}"', action)
        self.assertIn("Fallback checkpoint download failed after provenance succeeded; retrying on a fresh runner", action)

    def test_checkpoint_integrity_failures_return_immediately(self):
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn('bounded_external "${CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS}" zstd -q -t "${archive}" || compression_status=$?', resume)
        self.assertIn('CHECKPOINT_BUNDLE_FAILURE_CLASS="$(classify_prepare_command_status "${compression_status}" deterministic_build)"', resume)
        self.assertIn('Checkpoint archive SHA-256 verification failed.', resume)
        self.assertIn('Checkpoint manifest compatibility/provenance validation failed.', resume)
        self.assertIn('Checkpoint archive member/link/resource safety validation failed with status ${archive_validation_status}:', resume)

    def test_static_pin_policy_rejects_mutable_refs(self):
        workflow = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("@[0-9a-f]{40}", workflow)
        self.assertNotIn("@v[0-9]", workflow)


    def test_chromium_tooling_is_pinned_from_source_deps(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        preflight = (ROOT / ".github" / "workflows" / "chromium-i686-preflight.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("chromium_tool_pins.py", common)
        self.assertIn("depot_tools_revision", common)
        self.assertIn("chromium_gn_version", common)
        self.assertNotIn("cipd install gn/gn/linux-amd64 latest", common)
        self.assertIn("DEPOT_TOOLS_UPDATE=0", common)
        self.assertIn("verify_depot_tools_bootstrap()", common)
        self.assertIn('"${DEPOT_TOOLS}/ensure_bootstrap"', common)
        self.assertIn('"${DEPOT_TOOLS}/python-bin/python3" -c', common)
        self.assertIn("python3_bin_reldir.txt", common)
        self.assertIn('test -s "${DEPOT_TOOLS}/python3_bin_reldir.txt"', validation)
        self.assertLess(action.index('prepare_chromium_source'), action.index('install_depot_tools'))
        self.assertLess(preflight.index('prepare_chromium_source'), preflight.index('install_depot_tools'))

    def test_source_archive_stats_are_sha_bound_and_resource_bounded(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        validator = (ROOT / "scripts" / "validate_chromium_source_archive.py").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn('mode="r|xz"', validator)
        self.assertNotIn('mode="r:xz"', validator)
        self.assertIn("DEFAULT_MAX_MEMBERS", validator)
        self.assertIn("DEFAULT_MAX_UNPACKED_GIB", validator)
        self.assertIn("--source-sha256", validator)
        self.assertIn("--stats-file", validator)
        self.assertIn("CHROMIUM_I686_MAX_SOURCE_UNPACKED_GIB", common)
        self.assertIn("CHROMIUM_I686_MAX_SOURCE_MEMBERS", common)
        self.assertIn("CHROMIUM_I686_SOURCE_EXTRACT_RESERVE_GIB", common)
        self.assertIn("source_archive_stats_are_usable()", common)
        self.assertIn('member_count > max_members', common)
        self.assertIn('unpacked_bytes > max_unpacked_bytes', common)
        self.assertIn("ensure_source_archive_extract_space()", common)
        self.assertIn("source-archive-stats.json", common)
        self.assertIn("regenerating bounded stats once", common)
        self.assertIn("CHROMIUM_SOURCE_FAILURE_CLASS", common)
        self.assertIn('fail_prepare_step "${CHROMIUM_SOURCE_FAILURE_CLASS:-infrastructure}"', action)
        self.assertIn("bash tests/test_source_archive_space.sh", validation)
        self.assertLess(common.index("ensure_source_archive_extract_space"), common.index('echo "Extracting Chromium'))

    def test_source_archive_and_extracted_version_are_validated(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("validate_chromium_source_tarball()", common)
        self.assertIn("validate_extracted_chromium_version()", common)
        self.assertIn("--connect-timeout 30", common)
        self.assertIn("--max-time", common)
        self.assertIn("Authoritative GCS source bytes are structurally unsafe", common)
        self.assertIn("Discarding cached Chromium source bytes that do not match the authoritative GCS object", common)

    def test_packaged_runtime_is_executed_before_artifact_upload(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("smoke_test_i686_runtime_bundle()", common)
        self.assertIn("CHROMIUM_I686_RUNTIME_SMOKE_TIMEOUT_SECONDS", common)
        self.assertIn('"${launcher}" --version', common)
        self.assertIn("--headless", common)
        self.assertIn("--no-sandbox", common)
        self.assertIn("--disable-background-networking", common)
        self.assertIn('--dump-dom "file://${smoke_html}"', common)
        package = common[common.index("package_chromium_i686()"):]
        self.assertLess(package.index("validate_i686_runtime_bundle"), package.index("smoke_test_i686_runtime_bundle"))
        self.assertLess(package.index("smoke_test_i686_runtime_bundle"), package.rindex('CHROMIUM_PACKAGE_FAILURE_CLASS=""'))
        self.assertLess(action.index("Package Chromium i686 Build"), action.index("Upload Build Artifact"))
        self.assertIn("release_smoke_version", validation)
        self.assertIn("Validate real published i686 runtime", validation)
        self.assertIn("install_i386_runtime_libraries", validation)
        self.assertIn("gh release download", validation)
        self.assertIn("remote_digest", validation)
        self.assertIn("remote_checksum_digest", validation)
        self.assertIn("listed_sha", validation)
        release_drill = validation[validation.index("validate_real_release_runtime:"):validation.index("validate_full_source_preflight:")]
        self.assertNotIn("sha256sum -c", release_drill)
        self.assertIn("smoke_test_i686_runtime_bundle", validation)
        self.assertIn("bash tests/test_release_runtime_smoke.sh", validation)

    def test_release_archive_validation_is_streaming_and_resource_bounded(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        validator = (ROOT / "scripts" / "validate_release_archive.py").read_text(encoding="utf-8")
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn('mode="r|xz"', validator)
        self.assertNotIn("getmembers()", validator)
        self.assertIn("DEFAULT_MAX_MEMBERS", validator)
        self.assertIn("DEFAULT_MAX_UNPACKED_GIB", validator)
        self.assertIn("--stats-file", validator)
        self.assertIn("CHROMIUM_I686_MAX_RELEASE_UNPACKED_GIB", common)
        self.assertIn("CHROMIUM_I686_MAX_RELEASE_MEMBERS", common)
        self.assertIn("CHROMIUM_I686_RELEASE_EXTRACT_RESERVE_GIB", common)
        self.assertIn("validate_release_archive_with_stats()", common)
        self.assertIn("ensure_release_archive_extract_space()", common)
        self.assertIn('CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure', common)
        self.assertIn("validate_release_archive_with_stats", publish)
        self.assertLess(publish.index("validate_release_archive_with_stats"), publish.index("tar -xJf"))
        self.assertIn("ensure_release_archive_extract_space", publish)
        release_drill = validation[validation.index("validate_real_release_runtime:"):validation.index("validate_full_source_preflight:")]
        self.assertIn("validate_release_archive_with_stats", release_drill)
        self.assertIn("ensure_release_archive_extract_space", release_drill)
        self.assertIn("bash tests/test_release_archive_space.sh", validation)

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
        self.assertNotIn('--target "${BUILD_SHA}"', publish)
        self.assertIn("scripts/github_release_tag.py", publish)
        self.assertIn("scripts/github_release_state.py", publish)
        release_tag = (ROOT / "scripts" / "github_release_tag.py").read_text(encoding="utf-8")
        release_state = (ROOT / "scripts" / "github_release_state.py").read_text(encoding="utf-8")
        self.assertNotIn("urllib.request", release_tag)
        self.assertNotIn("urllib.request", release_state)
        self.assertNotIn("https://api.github.com", release_tag)
        self.assertNotIn("https://api.github.com", release_state)
        self.assertIn('["gh", *args]', release_tag)
        self.assertIn('["gh", *args]', release_state)
        self.assertIn("Verified exact release tag", publish)
        self.assertIn("Pre-publication tag verification", publish)
        self.assertGreaterEqual(publish.count("scripts/github_release_tag.py"), 2)
        self.assertNotIn('existing_tag_commit="$(bounded_gh api', publish)
        self.assertIn("refusing to rewrite release history", publish)
        self.assertIn("immutable releases are never mutated in place", publish)
        self.assertIn("--draft", publish)
        self.assertIn("Uploading missing draft asset", publish)
        self.assertIn("--draft=false", publish)
        self.assertNotIn("--clobber", publish)
        self.assertIn('"$(basename "${package}")"', common)
        self.assertIn("package_sha256=", common)

    def test_checkpoint_has_reserve_and_explicit_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "chromium-i686.yml").read_text(encoding="utf-8")
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        self.assertIn("CHROMIUM_I686_CHECKPOINT_MINUTES || '340'", workflow)
        self.assertIn("CHECKPOINT_CONTRACT_VERSION", common)
        self.assertIn("checkpoint_contract_version", resume)
        self.assertIn("CHECKPOINT_GN_VERSION", resume)
        self.assertIn("CHECKPOINT_DEPOT_REVISION", resume)
        self.assertIn("CHROMIUM_I686_ARCHIVE_TIMEOUT_SECONDS", resume)
        self.assertIn("CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS", common)
        self.assertIn("CHROMIUM_I686_CHECKPOINT_ARCHIVE_TIMEOUT_SECONDS", resume)


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
        self.assertIn("CHROMIUM_PACKAGE_FAILURE_CLASS=infrastructure", common)
        self.assertIn('id: package', action)
        self.assertIn("steps.package.outputs.failure_class", action)
        self.assertIn("if-no-files-found: error", action)


    def test_checkpoint_artifacts_precede_optional_cache_and_preserve_final_output(self):
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertNotIn("Restore ccache", action)
        self.assertNotIn("Save ccache", action)
        self.assertNotIn("chromium-i686-ccache-", action)
        self.assertIn("key: chromium-src-v3-${{ inputs.version }}", action)
        self.assertIn("chromium-src-v2-${{ inputs.version }}", action)
        self.assertIn("chromium-src-${{ inputs.version }}", action)
        self.assertIn("steps.source_cache.outputs.cache-hit != 'true'", action)
        self.assertNotIn("key: chromium-src-${{ inputs.version }}-${{ github.run_id }}", action)
        self.assertIn("Preserve completed output after packaging or artifact failure", action)
        self.assertIn("Upload Final Output Recovery Checkpoint", action)
        self.assertIn("steps.build_artifact.outcome == 'failure'", action)
        self.assertIn("steps.final_recovery.outputs.failure_class", action)

    def test_release_workflow_supports_trusted_manual_republish(self):
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", publish)
        self.assertIn("github.event.workflow_run.head_branch == github.event.repository.default_branch", publish)
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", publish)
        self.assertIn("build_run_id:", publish)
        self.assertIn("version:", publish)
        self.assertIn("Chromium i686 {0} from build run {1}", publish)
        self.assertIn("REQUESTED_VERSION", publish)
        self.assertIn("Manual republish requested Chromium", publish)
        self.assertIn("Resolve and verify trusted build source", publish)
        self.assertIn('workflow_path}" = ".github/workflows/chromium-i686.yml"', publish)
        self.assertIn('head_branch}" = "${DEFAULT_BRANCH}"', publish)
        self.assertIn('head_repo}" = "${GITHUB_REPOSITORY}"', publish)
        self.assertIn("has no retained final Chromium runtime artifact", publish)

    def test_host_optional_probes_cannot_hang_or_require_swap(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("continuing without swap", common)
        self.assertIn("capture_ldd_output()", common)
        self.assertIn("bounded_ldd()", common)
        self.assertIn("CHROMIUM_I686_LDD_TIMEOUT_SECONDS", common)
        self.assertIn('timeout -k 3s "${CHROMIUM_I686_LDD_TIMEOUT_SECONDS}s" ldd', common)
        self.assertIn("timeout -k 10s 120s ccache --cleanup", common)

    def test_standalone_runtime_requires_rendered_wrapper(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts" / "chromium_linux_runtime.py").read_text(encoding="utf-8")
        self.assertIn("chrome-wrapper", common)
        self.assertIn("render_standalone_wrapper", runtime)
        self.assertIn("@@PROGNAME", runtime)
        self.assertIn("@@channel", runtime)
        self.assertIn("--render-wrapper", common)


    def test_publisher_is_transactional_and_does_not_depend_on_target_i386_runtime(self):
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        self.assertIn("Create or resume transactional immutable release", publish)
        self.assertIn("--draft", publish)
        self.assertIn("Uploading missing draft asset", publish)
        self.assertIn("verifying whether GitHub stored", publish)
        self.assertIn("All draft assets are byte-identical", publish)
        self.assertIn("exact provenance is enforced by verified Git ref", publish)
        self.assertIn("--draft=false", publish)
        self.assertIn("group: chromium-i686-release", publish)
        self.assertNotIn("github.event.workflow_run.id || inputs.build_run_id", publish[publish.index("concurrency:"):publish.index("jobs:")])
        self.assertIn("required release-digest capability is unavailable", publish)
        self.assertNotIn("install_i386_runtime_libraries", publish)

    def test_build_hosts_always_install_release_validation_tools(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        deps = common[common.index("NATIVE_BUILD_PACKAGES=(") : common.index("I386_BASELINE_SONAMES=(")]
        self.assertIn("file binutils", deps)
        self.assertIn('bounded_sudo_apt_get install -y "${NATIVE_BUILD_PACKAGES[@]}"', deps)

    def test_runtime_bundle_validates_symlink_containment(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        self.assertIn("broken symlink", common)
        self.assertIn("symlink escapes package root", common)
        self.assertIn("readlink -f", common)


    def test_pinless_checkpoint_migrates_graph_before_claiming_tool_provenance(self):
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("CHECKPOINT_REQUIRES_GN_REFRESH=false", resume)
        self.assertIn("predates exact GN/depot_tools provenance", resume)
        self.assertIn("legacy checkpoint has no tool-pin manifest", resume)
        self.assertIn('CHECKPOINT_REQUIRES_GN_REFRESH}" = "true"', action)
        self.assertIn("Migrating restored checkpoint graph", action)
        self.assertIn("configure_gn", action)

    def test_publisher_installs_json_and_validation_prerequisites_before_use(self):
        publish = (ROOT / ".github" / "workflows" / "publish-i686-release.yml").read_text(encoding="utf-8")
        install_pos = publish.index("Install release validation tools")
        resolve_pos = publish.index("Resolve and verify trusted build source")
        self.assertLess(install_pos, resolve_pos)
        install = publish[install_pos:resolve_pos]
        self.assertIn("bounded_sudo_apt_get update", install)
        self.assertIn("jq python3 file binutils xz-utils", install)
        self.assertIn("display_title<<%s", publish)
        self.assertIn("if: ${{ failure() }}", publish)
        self.assertIn("steps.artifact.outputs.version || 'unknown'", publish)
        self.assertIn("scripts/github_maintenance_issue.py", publish)
        self.assertIn("Release succeeded but maintenance issue cleanup failed", publish)
        self.assertIn("if ! python3 scripts/github_maintenance_issue.py", publish)


    def test_source_identity_is_cross_checked_against_authoritative_tag(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("validate_chromium_critical_source_identity()", common)
        self.assertIn("chromium.googlesource.com/chromium/src/+/refs/tags/${version}", common)
        self.assertIn("chrome/installer/linux/BUILD.gn", common)
        self.assertIn("critical source files against the authoritative Gitiles tag", common)
        self.assertIn("validate_effective_https_host()", common)
        self.assertIn("--proto '=https' --proto-redir '=https'", common)
        self.assertIn("chromium.googlesource.com", common)
        self.assertIn("commondatastorage.googleapis.com", common)
        self.assertIn("response.geturl()", common)
        self.assertIn("version-history request escaped trusted host", common)
        self.assertIn('validate_effective_https_host "${effective_url}" chromium.googlesource.com', validation)
        self.assertIn("bash tests/test_source_trust_hosts.sh", validation)

    def test_gn_pin_is_reasserted_even_if_binary_already_exists(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        gn_func = common[common.index("install_gn_from_cipd()") : common.index("configure_gn()")]
        self.assertIn("will be re-asserted", gn_func)
        self.assertIn("cipd install", gn_func)
        self.assertNotIn("return 0", gn_func)


    def test_source_archive_paths_are_validated_before_extraction(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        validator = (ROOT / "scripts" / "validate_chromium_source_archive.py").read_text(encoding="utf-8")
        self.assertIn("validate_chromium_source_archive.py", common)
        self.assertLess(common.index("validate_chromium_source_tarball"), common.index("tar -xJf"))
        self.assertIn("Unsafe source archive member path", validator)
        self.assertIn("Source archive link escapes expected root", validator)
        self.assertIn("Unsupported special source archive member", validator)


    def test_manual_full_source_preflight_is_available_without_build_dispatch(self):
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("full_version:", validation)
        self.assertIn("validate_full_source_preflight:", validation)
        self.assertIn("Full Chromium i686 compatibility proof", validation)
        self.assertIn('prepare_chromium_source "${CHROMIUM_VERSION}"', validation)
        self.assertIn("run_extended_i686_preflight", validation)
        full_job = validation[validation.index("validate_full_source_preflight:") : validation.index("validate_i386_runtime:")]
        self.assertNotIn("gh workflow run chromium-i686.yml", full_job)
        self.assertNotIn("concurrency:", full_job)


    def test_source_cache_fast_path_is_bound_to_authoritative_object_identity(self):
        common = (ROOT / ".github" / "scripts" / "chromium_i686_common.sh").read_text(encoding="utf-8")
        verifier = (ROOT / "scripts" / "chromium_source_object.py").read_text(encoding="utf-8")
        self.assertIn("chromium_source_object.py", common)
        self.assertIn("SOURCE_METADATA_TEMPLATE", verifier)
        self.assertIn("md5Hash", verifier)
        self.assertIn("generation", verifier)
        self.assertIn("md5_base64", verifier)
        self.assertIn("sha256", verifier)
        self.assertIn("safe_archive", verifier)
        self.assertIn("gitiles_identity", verifier)
        self.assertIn("VERSION_RE", verifier)
        self.assertNotIn("urllib.request", verifier)
        self.assertNotIn('add_argument("--url")', verifier)
        self.assertIn('--version "${version}" --file', common)
        self.assertIn("--safe-archive-verified --gitiles-identity-verified", common)
        self.assertIn("skipping redundant decompression scan", common)
        self.assertIn("validate_chromium_critical_source_identity", common)
        self.assertNotIn('xz -t "${tarball}"', common)


    def test_source_cache_contract_can_migrate_legacy_cache_to_v3_stats(self):
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        restore_pos = action.index("Restore Chromium source tarball cache")
        save_pos = action.index("Save Chromium source tarball cache")
        restore = action[restore_pos:save_pos]
        save = action[save_pos:]
        self.assertIn("key: chromium-src-v3-${{ inputs.version }}", restore)
        self.assertIn("chromium-src-v2-${{ inputs.version }}", restore)
        self.assertIn("chromium-src-${{ inputs.version }}", restore)
        self.assertIn("key: chromium-src-v3-${{ inputs.version }}", save)
        self.assertIn("cache-hit != 'true'", save)


    def test_checkpoint_restore_has_archive_and_run_provenance_boundaries(self):
        resume = (ROOT / ".github" / "scripts" / "chromium_i686_resume.sh").read_text(encoding="utf-8")
        action = (ROOT / ".github" / "actions" / "chromium-i686-stage" / "action.yml").read_text(encoding="utf-8")
        validation = (ROOT / ".github" / "workflows" / "validate-port-infrastructure.yml").read_text(encoding="utf-8")
        self.assertIn("validate_checkpoint_source_run()", resume)
        self.assertIn("CHECKPOINT_PRODUCER_SHA", resume)
        self.assertIn("CHECKPOINT_PRODUCER_RUN_ATTEMPT", resume)
        self.assertIn("producer_run_id", resume)
        self.assertIn("producer_run_attempt", resume)
        self.assertIn("workflow_dispatch", resume)
        self.assertIn("checkpoint_validation_state_matches", resume)
        self.assertIn("Metadata-bearing checkpoints require trusted producer SHA/stage/run/attempt context", resume)
        self.assertIn("skip validation without matching in-process validation state", resume)
        self.assertIn("refusing a metadata-bearing checkpoint without source identity", resume)
        self.assertIn("scripts/validate_checkpoint_archive.py", resume)
        self.assertLess(
            resume.index("scripts/validate_checkpoint_archive.py"),
            resume.index("tar -I 'zstd -T0 -d' -xf"),
        )
        self.assertGreaterEqual(resume.count("scripts/validate_checkpoint_archive.py"), 2)
        self.assertIn(".checkpoint-restore-", resume)
        self.assertIn("active output remains untouched", resume)
        self.assertGreaterEqual(action.count("validate_checkpoint_source_run"), 2)
        self.assertIn("preferred_run_attempt", action)
        self.assertIn("fallback_run_attempt", action)
        self.assertIn("preferred-checkpoint-run-id", action)
        self.assertIn("fallback-checkpoint-run-id", action)
        self.assertIn("CHECKPOINT_PROVENANCE_FAILURE_CLASS", action)
        self.assertIn("bash tests/test_checkpoint_provenance.sh", validation)
        self.assertIn("bash tests/test_checkpoint_restore_atomic.sh", validation)
        self.assertIn("Validate real checkpoint recovery artifact", validation)
        self.assertIn("checkpoint_run_id", validation)
        self.assertIn("CHECKPOINT_EXPECTED_REF", resume)
        self.assertIn("validate_checkpoint_source_run", validation)
        self.assertIn("validate_checkpoint_archive.py", validation)



if __name__ == "__main__":
    unittest.main()
