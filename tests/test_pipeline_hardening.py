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
        ):
            self.assertIn(f"[{soname}]", common)

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
        self.assertIn('restore_out_checkpoint "${resume_archive}"', action)
        self.assertIn(" true", action)


if __name__ == "__main__":
    unittest.main()
