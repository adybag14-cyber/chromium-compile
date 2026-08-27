import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "chromium_windows_pipeline.py"
if str(PATH.parent) not in sys.path:
    sys.path.insert(0, str(PATH.parent))
SPEC = importlib.util.spec_from_file_location("chromium_windows_pipeline", PATH)
assert SPEC and SPEC.loader
pipeline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


class WindowsI686PipelineTests(unittest.TestCase):
    def test_runner_command_files_are_scoped_to_runner_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            command_dir = root / "_runner_file_commands"
            command_dir.mkdir()
            expected = command_dir / "set_output_abcdefgh"
            with mock.patch.dict(
                pipeline.os.environ,
                {"RUNNER_TEMP": str(root), "GITHUB_OUTPUT": str(expected)},
                clear=False,
            ):
                self.assertEqual(
                    pipeline._runner_command_file("GITHUB_OUTPUT", "set_output_"),
                    expected.resolve(),
                )
            with mock.patch.dict(
                pipeline.os.environ,
                {
                    "RUNNER_TEMP": str(root),
                    "GITHUB_OUTPUT": str(root / "set_output_abcdefgh"),
                },
                clear=False,
            ):
                with self.assertRaises(pipeline.WindowsPipelineError):
                    pipeline._runner_command_file("GITHUB_OUTPUT", "set_output_")

    def test_command_wrapper_rejects_unlisted_executable_before_spawn(self):
        with mock.patch.object(pipeline.subprocess, "run") as run:
            with self.assertRaisesRegex(
                pipeline.WindowsPipelineError, "outside the Windows pipeline allowlist"
            ):
                pipeline._run(["attacker-controlled.exe", "argument"])
        run.assert_not_called()

    def test_gitiles_critical_file_fetch_retries_transient_http_and_uses_show_endpoint(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "https://chromium.googlesource.com/chromium/src/+show/refs/tags/153.0.8010.12/docs/windows_build_instructions.md?format=TEXT"

            def read(self, _limit):
                import base64

                return base64.b64encode(b"proof")

        transient = urllib.error.HTTPError(
            "https://chromium.googlesource.com/", 400, "transient", {}, None
        )
        with mock.patch.object(
            pipeline.urllib.request, "urlopen", side_effect=[transient, Response()]
        ) as opener, mock.patch.object(pipeline.time, "sleep") as sleep:
            self.assertEqual(
                pipeline._fetch_gitiles_bytes(
                    "153.0.8010.12", "docs/windows_build_instructions.md"
                ),
                b"proof",
            )
        self.assertIn("/+show/refs/tags/", opener.call_args.args[0].full_url)
        sleep.assert_called_once_with(1)

    def test_source_declared_sdk_and_visual_studio_are_derived_not_hardcoded(self):
        vs_toolchain = """
TOOLCHAIN_HASH = 'abc'
SDK_VERSION = '10.0.28000.0'
MSVS_VERSIONS = collections.OrderedDict([
    ('2026', '18.0'),
    ('2022', '17.0'),
])
"""
        docs = "Chromium requires Visual Studio 2026 (>=18.0.0). Required SDK version 10.0.28000.2270."
        result = pipeline.parse_windows_requirements(vs_toolchain, docs)
        self.assertEqual(result.sdk_family, "10.0.28000.0")
        self.assertEqual(result.sdk_min_servicing, "10.0.28000.2270")
        self.assertEqual(result.visual_studio_year, "2026")
        self.assertEqual(result.visual_studio_min_version, "18.0")

    def test_windows_x86_source_guard_is_semantic_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp)
            files = {
                "BUILD.gn": """
is_valid_x86_target = target_os != "ios" && target_os != "mac" &&
  (target_os != "linux" || use_libfuzzer)
assert(is_valid_x86_target || target_cpu != "x86" || v8_target_cpu == "arm")
group("next") {}
""",
                "build/toolchain/win/BUILD.gn": """
if (target_cpu == "x86" || target_cpu == "x64") {
  win_toolchains("x86") { toolchain_arch = "x86" }
}
""",
                "build/vs_toolchain.py": """
SDK_VERSION = '10.0.28000.0'
MSVS_VERSIONS = collections.OrderedDict([('2026', '18.0')])
""",
                "docs/windows_build_instructions.md": "SDK version 10.0.28000.2270",
            }
            for relative, text in files.items():
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            result = pipeline.verify_windows_x86_source_contract(source)
            self.assertEqual(result.sdk_family, "10.0.28000.0")
            (source / "BUILD.gn").write_text(
                files["BUILD.gn"].replace(
                    'target_os != "ios"', 'target_os != "win" && target_os != "ios"'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(pipeline.WindowsPipelineError, "no longer declares"):
                pipeline.verify_windows_x86_source_contract(source)

    def test_build_failure_classification_separates_runner_from_source(self):
        with tempfile.TemporaryDirectory() as temp:
            log = pathlib.Path(temp) / "build.log"
            log.write_text("fatal error: no member named changed_upstream", encoding="utf-8")
            self.assertEqual(pipeline.classify_build_log(log), "deterministic_build")
            log.write_text("LINK : fatal error LNK1102: out of memory", encoding="utf-8")
            self.assertEqual(pipeline.classify_build_log(log), "infrastructure")
            log.write_text("The code execution cannot proceed because foo.dll was not found", encoding="utf-8")
            self.assertEqual(pipeline.classify_build_log(log), "runtime_environment")

    def test_windows_control_plane_is_independent_and_bounded(self):
        build = (ROOT / ".github/workflows/chromium-windows-i686.yml").read_text(encoding="utf-8")
        preflight = (ROOT / ".github/workflows/chromium-windows-i686-preflight.yml").read_text(encoding="utf-8")
        action = (ROOT / ".github/actions/chromium-windows-i686-stage/action.yml").read_text(encoding="utf-8")
        resolver = (ROOT / ".github/workflows/resolve-windows-i686-production-runner.yml").read_text(encoding="utf-8")
        self.assertIn("chromium-windows-i686-port-queue", build)
        self.assertNotIn("'chromium-i686-port-queue'", build)
        self.assertIn("workflow lineage SHA drift", build)
        self.assertIn("CHROMIUM_WINDOWS_I686_MAX_STAGES", build)
        self.assertIn("CHROMIUM_WINDOWS_RUNNER_RETRIES", build)
        self.assertIn("scripts/github_workflow_dispatch.py", build)
        self.assertIn("--dedupe-completed", build)
        self.assertIn("--expected-head-sha", build)
        self.assertIn("needs.build.outputs.failure_class != 'deterministic_build'", build)
        self.assertIn("--lane windows", build)
        self.assertIn("source publication", preflight)
        self.assertIn("--evidence-dir", preflight)
        self.assertIn("actions/cache/restore@55cc834", action)
        self.assertIn("prepared-source-cache-key", action)
        self.assertIn("if: ${{ always() }}", action)
        self.assertIn("out-Release_x86_win.tar.zst", action)
        self.assertIn("Preserve completed output after packaging or artifact failure", action)
        self.assertIn("windows-2025-vs2026", resolver)
        self.assertNotIn("windows-latest", resolver)

    def test_windows_release_is_independently_pe32_smoked_and_immutable(self):
        publish = (ROOT / ".github/workflows/publish-windows-i686-release.yml").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts/chromium_windows_runtime.py").read_text(encoding="utf-8")
        helper = (ROOT / "scripts/github_immutable_release.py").read_text(encoding="utf-8")
        self.assertIn("validate-release-bundle", publish)
        self.assertIn("chromium_windows_runtime.py smoke", publish)
        self.assertIn("runs-on: ${{ needs.resolve_runner.outputs.label }}", publish)
        self.assertIn("github_immutable_release.py", publish)
        self.assertIn("isImmutable", helper)
        self.assertIn("refusing overwrite", helper)
        self.assertIn("PE_MACHINE_I386 = 0x014C", runtime)
        self.assertIn("PE32_OPTIONAL_MAGIC = 0x010B", runtime)
        self.assertIn("taskkill.exe", runtime)

    def test_windows_watcher_has_separate_platform_state(self):
        baseline = json.loads((ROOT / "support/baseline.json").read_text(encoding="utf-8"))
        watcher = (ROOT / ".github/workflows/watch-chromium-windows-stable.yml").read_text(encoding="utf-8")
        script = (ROOT / "scripts/chromium_stable_watcher.py").read_text(encoding="utf-8")
        self.assertEqual(baseline["windows_minimum_version"], "153.0.8010.12")
        self.assertEqual(baseline["windows_verified_builds"], [])
        self.assertIn("--lane windows", watcher)
        self.assertIn("version_platform=\"win\"", script)
        self.assertIn("chromium-windows-i686-preflight.yml", script)
        self.assertIn("windows-i686-port", script)

    def test_source_pinned_sdk_installer_includes_debugger_and_x86_features(self):
        source = (ROOT / "scripts/chromium_windows_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("Microsoft.WindowsSDK.", source)
        self.assertIn("OptionId.DesktopCPPx86", source)
        self.assertIn("OptionId.WindowsDesktopDebuggers", source)
        self.assertIn("Debuggers/x86/dbghelp.dll", source)
        self.assertIn("Lib/{sdk_family}/um/x86/kernel32.lib", source)

    def test_windows_batch_tools_use_tokenized_cmd_call_without_s_requoting(self):
        source = (ROOT / "scripts/chromium_windows_pipeline.py").read_text(
            encoding="utf-8"
        )
        depot_block = source[
            source.index("def install_depot_tools(") : source.index(
                "def _depot_python(")
        ]
        tool_block = source[
            source.index("def install_source_declared_tools(") : source.index(
                "PORT_CONFIG_FILES =")
        ]
        for block in (depot_block, tool_block):
            self.assertIn('"/c",', block)
            self.assertIn('"call",', block)
            self.assertNotIn('"/s",', block)
            self.assertNotIn("f'call", block)


if __name__ == "__main__":
    unittest.main()
