import importlib.util
import io
import json
import pathlib
import sys
import tarfile
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
    @staticmethod
    def _write_xz_tar(path: pathlib.Path, members: list[tuple[str, bytes]]) -> None:
        with tarfile.open(path, "w:xz") as archive:
            for name, payload in members:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

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

    def test_command_wrapper_can_quietly_discard_large_dry_run_output(self):
        completed = pipeline.subprocess.CompletedProcess(["ninja.exe"], 0, None, "")
        with mock.patch.object(
            pipeline.subprocess, "run", return_value=completed
        ) as run:
            pipeline._run(["ninja.exe", "-n", "chrome"], discard_stdout=True)
        self.assertIs(run.call_args.kwargs["stdout"], pipeline.subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], pipeline.subprocess.PIPE)

    def test_dawn_generator_output_is_confined_to_generated_output_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            out = source / "out/Release_x86_win"
            out.mkdir(parents=True)
            resolved = pipeline._gn_generated_output_under_out(
                source,
                out,
                "//out/Release_x86_win/gen/src/tint/lang/core/enums.h",
            )
            self.assertEqual(
                resolved,
                out / "gen/src/tint/lang/core/enums.h",
            )
            for unsafe in (
                "//out/Other/gen/src/tint/enums.h",
                "gen/../escape.h",
                "obj/third_party/dawn/generator.stamp",
                "C:/escape.h",
            ):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaises(pipeline.WindowsPipelineError):
                        pipeline._gn_generated_output_under_out(source, out, unsafe)

    def test_dawn_generator_preflight_executes_real_ninja_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            out = source / "out/Release_x86_win"
            out.mkdir(parents=True)
            declared = (
                out / "gen/src/tint/lang/core/enums.h",
                out / "gen/src/tint/lang/wgsl/enums.cc",
            )

            def run(command, **_kwargs):
                if "desc" in command:
                    stdout = "\n".join(
                        "//out/Release_x86_win/"
                        + path.relative_to(out).as_posix()
                        for path in declared
                    )
                    return pipeline.subprocess.CompletedProcess(
                        command, 0, stdout + "\n", ""
                    )
                for path in declared:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("generated\n", encoding="utf-8")
                return pipeline.subprocess.CompletedProcess(command, 0, None, "")

            with mock.patch.object(pipeline, "_run", side_effect=run) as runner:
                stats = pipeline.validate_dawn_source_generator(
                    source,
                    out,
                    pathlib.Path("gn.exe"),
                    pathlib.Path("ninja.exe"),
                    {"GOTOOLCHAIN": "local"},
                )
            self.assertEqual(stats["label"], pipeline.DAWN_GENERATOR_LABEL)
            self.assertEqual(stats["output_count"], 2)
            self.assertEqual(
                runner.call_args_list[1].args[0][-1],
                "gen/src/tint/lang/core/enums.h",
            )

    def test_windows_resume_normalizes_inputs_without_touching_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            tool = source / "third_party/tool/bin/tool.exe"
            output = source / "out/Release_x86_win/obj/already-built.obj"
            tool.parent.mkdir(parents=True)
            output.parent.mkdir(parents=True)
            tool.write_bytes(b"tool")
            output.write_bytes(b"object")
            symlink = source / "tool-link.exe"
            try:
                symlink.symlink_to(tool)
            except OSError:
                symlink = None
            output_epoch = 1_700_000_000
            pipeline.os.utime(output, (output_epoch, output_epoch))

            stats = pipeline.normalize_windows_resume_inputs(
                source, epoch=pipeline.WINDOWS_RESUME_INPUT_EPOCH
            )

            self.assertEqual(int(tool.stat().st_mtime), pipeline.WINDOWS_RESUME_INPUT_EPOCH)
            self.assertEqual(
                int(tool.parent.stat().st_mtime), pipeline.WINDOWS_RESUME_INPUT_EPOCH
            )
            self.assertEqual(int(source.stat().st_mtime), pipeline.WINDOWS_RESUME_INPUT_EPOCH)
            self.assertEqual(int(output.stat().st_mtime), output_epoch)
            self.assertGreaterEqual(stats["files"], 1)
            self.assertGreaterEqual(stats["directories"], 3)
            if symlink is not None:
                self.assertEqual(
                    int(symlink.lstat().st_mtime), pipeline.WINDOWS_RESUME_INPUT_EPOCH
                )
                self.assertEqual(stats["symlinks"], 1)

    def test_reused_windows_graph_never_runs_gn_gen_and_requires_clean_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            out = source / "out" / pipeline.OUT_NAME
            out.mkdir(parents=True)
            (out / "args.gn").write_text(pipeline.WINDOWS_GN_ARGS, encoding="utf-8")
            (out / "build.ninja").write_text("rule noop\n", encoding="utf-8")
            clean = pipeline.subprocess.CompletedProcess(
                ["ninja.exe"], 0, "ninja: no work to do.\n", ""
            )
            with mock.patch.object(pipeline, "_run", return_value=clean) as run, mock.patch.object(
                pipeline,
                "_validate_configured_gn_graph",
                return_value=out,
            ) as validate, mock.patch.object(
                pipeline,
                "revalidate_restored_gn_manifest",
                return_value={"stamp_refreshed": True},
            ) as revalidate:
                result = pipeline.reuse_restored_gn_graph(
                    source,
                    pathlib.Path("gn.exe"),
                    pathlib.Path("ninja.exe"),
                    {},
                    visual_studio=pathlib.Path("vs"),
                    windows_build_timestamp=1_785_646_800,
                )
            self.assertEqual(result, out)
            self.assertEqual(run.call_args.args[0][-2:], ["-n", "build.ninja"])
            self.assertIn("explain", run.call_args.args[0])
            self.assertNotIn("gen", run.call_args.args[0])
            revalidate.assert_called_once()
            validate.assert_called_once()

            dirty = pipeline.subprocess.CompletedProcess(
                ["ninja.exe"], 0, "[1/1] ACTION //build:gn_run_binary\n", ""
            )
            with mock.patch.object(pipeline, "_run", return_value=dirty), mock.patch.object(
                pipeline,
                "revalidate_restored_gn_manifest",
                return_value={"stamp_refreshed": True},
            ):
                with self.assertRaisesRegex(
                    pipeline.WindowsPipelineError, "refusing silent graph regeneration"
                ):
                    pipeline.reuse_restored_gn_graph(
                        source,
                        pathlib.Path("gn.exe"),
                        pathlib.Path("ninja.exe"),
                        {},
                        visual_studio=pathlib.Path("vs"),
                        windows_build_timestamp=1_785_646_800,
                    )

    def test_restored_gn_manifest_revalidates_only_the_zero_byte_stamp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "src"
            out = source / "out" / pipeline.OUT_NAME
            visual_studio = root / "Program Files/Microsoft Visual Studio/18/Enterprise"
            windows_kits = root / "Program Files (x86)/Windows Kits/10"
            netfx_sdk = windows_kits.parent / "NETFXSDK"
            vs_dep = visual_studio / "VC/Tools/MSVC/14.51/include"
            kits_dep = windows_kits / "include/10.0.28000.0/shared"
            netfx_dep = netfx_sdk / "4.8.1/include/um"
            for directory in (out, vs_dep, kits_dep, netfx_dep):
                directory.mkdir(parents=True)
            source_dep = source / "BUILD.gn"
            source_dep.write_text("group(\"fixture\") {}\n", encoding="utf-8")
            args_gn = out / "args.gn"
            args_gn.write_text(pipeline.WINDOWS_GN_ARGS, encoding="utf-8")
            build_ninja = out / "build.ninja"
            build_ninja.write_text(
                "\n".join(
                    (
                        "ninja_required_version = 1.7.2",
                        "",
                        "rule gn",
                        "  command = ../../../gn/gn.exe --root=../.. -q --regeneration gen .",
                        "  pool = console",
                        "  description = Regenerating ninja files",
                        "",
                        "build build.ninja.stamp: gn",
                        "  generator = 1",
                        "  depfile = build.ninja.d",
                        "",
                        "build build.ninja: phony build.ninja.stamp",
                        "  generator = 1",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            def escaped(path):
                return path.as_posix().replace(" ", "\\ ")

            depfile = out / "build.ninja.d"
            depfile.write_text(
                "build.ninja.stamp: ../../BUILD.gn "
                + escaped(vs_dep)
                + " "
                + escaped(kits_dep)
                + " "
                + escaped(netfx_dep)
                + "\n",
                encoding="utf-8",
            )
            stamp = out / "build.ninja.stamp"
            stamp.touch()
            ninja_log = out / ".ninja_log"
            ninja_log.write_text("# ninja log v5\n1\t2\t0\tobj/a.obj\tdeadbeef\n", encoding="utf-8")
            ninja_deps = out / ".ninja_deps"
            ninja_deps.write_bytes(b"deps-fixture")
            completed_output = out / "obj/a.obj"
            completed_output.parent.mkdir()
            completed_output.write_bytes(b"compiled-output")

            fixture_now_ns = pipeline.time.time_ns()
            old_ns = fixture_now_ns - 20_000_000_000
            source_ns = 946_684_800_000_000_000
            external_ns = fixture_now_ns - 10_000_000_000
            pipeline.os.utime(source_dep, ns=(source_ns, source_ns))
            pipeline.os.utime(stamp, ns=(old_ns, old_ns))
            pipeline.os.utime(vs_dep, ns=(external_ns, external_ns))
            pipeline.os.utime(kits_dep, ns=(external_ns, external_ns))
            protected = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (
                    args_gn,
                    build_ninja,
                    depfile,
                    ninja_log,
                    ninja_deps,
                    completed_output,
                )
            }

            stats = pipeline.revalidate_restored_gn_manifest(
                source,
                out,
                visual_studio=visual_studio,
                windows_kits_root=windows_kits,
            )

            self.assertEqual(stats["dependency_count"], 4)
            self.assertEqual(stats["source_dependency_count"], 1)
            self.assertEqual(stats["external_directory_count"], 3)
            self.assertEqual(stats["trusted_external_root_count"], 3)
            self.assertTrue(stats["stamp_refreshed"])
            self.assertGreater(stamp.stat().st_mtime_ns, external_ns)
            for path, before in protected.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_restored_gn_manifest_rejects_untrusted_external_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "src"
            out = source / "out" / pipeline.OUT_NAME
            visual_studio = root / "vs"
            windows_kits = root / "kits"
            rogue = root / "rogue"
            for directory in (out, visual_studio, windows_kits, rogue):
                directory.mkdir(parents=True)
            (out / "args.gn").write_text(pipeline.WINDOWS_GN_ARGS, encoding="utf-8")
            (out / "build.ninja").write_text(
                "rule gn\n"
                "  command = ../../../gn/gn.exe --root=../.. -q --regeneration gen .\n"
                "build build.ninja.stamp: gn\n"
                "  depfile = build.ninja.d\n"
                "build build.ninja: phony build.ninja.stamp\n",
                encoding="utf-8",
            )
            (out / "build.ninja.d").write_text(
                "build.ninja.stamp: " + rogue.as_posix() + "\n",
                encoding="utf-8",
            )
            (out / "build.ninja.stamp").touch()
            with self.assertRaisesRegex(
                pipeline.WindowsPipelineError, "untrusted absolute input"
            ):
                pipeline.revalidate_restored_gn_manifest(
                    source,
                    out,
                    visual_studio=visual_studio,
                    windows_kits_root=windows_kits,
                )

    @unittest.skipUnless(sys.platform == "win32", "Windows reparse-point API test")
    def test_windows_resume_reparse_timestamp_api_does_not_follow_target(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            forced_reparse = source / "forced-reparse"
            source.mkdir()
            forced_reparse.write_bytes(b"fixture")
            concrete_path_type = type(forced_reparse)
            original_is_symlink = concrete_path_type.is_symlink

            def is_symlink(path):
                return path.name == forced_reparse.name or original_is_symlink(path)

            with mock.patch.object(concrete_path_type, "is_symlink", is_symlink):
                stats = pipeline.normalize_windows_resume_inputs(source)

            self.assertEqual(
                int(forced_reparse.stat().st_mtime),
                pipeline.WINDOWS_RESUME_INPUT_EPOCH,
            )
            self.assertEqual(stats["symlinks"], 1)

    def test_windows_reparse_timestamp_always_uses_directory_handle_semantics(self):
        source = (ROOT / "scripts" / "chromium_windows_pipeline.py").read_text(
            encoding="utf-8"
        )
        block = source[
            source.index("def normalize_windows_resume_inputs(") : source.index(
                "def _version_tuple("
            )
        ]
        self.assertIn(
            "flags = file_flag_open_reparse_point | file_flag_backup_semantics",
            block,
        )
        self.assertNotIn("if path.is_dir() or lstat_attributes", block)

    @unittest.skipUnless(sys.platform == "win32", "Windows bsdtar precision test")
    def test_windows_pax_checkpoint_preserves_subsecond_output_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            output = root / "out" / pipeline.OUT_NAME / "obj/probe.obj"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"probe")
            wanted_ns = 1_787_689_253_123_456_700
            pipeline.os.utime(output, ns=(wanted_ns, wanted_ns))
            before_ns = output.stat().st_mtime_ns
            archive = root / "checkpoint.tar.zst"
            tar = pipeline.shutil.which("tar.exe") or pipeline.shutil.which("tar")
            self.assertIsNotNone(tar)
            pipeline.subprocess.run(
                [
                    tar,
                    "--zstd",
                    "-cf",
                    str(archive),
                    "--format=pax",
                    "-C",
                    str(root / "out"),
                    pipeline.OUT_NAME,
                ],
                check=True,
            )
            restored = root / "restored"
            restored.mkdir()
            pipeline.subprocess.run(
                [tar, "-xf", str(archive), "-C", str(restored)],
                check=True,
            )
            after_ns = (restored / pipeline.OUT_NAME / "obj/probe.obj").stat().st_mtime_ns
            self.assertEqual(after_ns, before_ns)

    def test_ninja_log_progress_counts_unique_outputs_not_rebuild_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            log = pathlib.Path(temp) / ".ninja_log"
            log.write_text(
                "# ninja log v5\n"
                "0\t1\t1\tobj/a.obj\taaaa\n"
                "1\t2\t2\tobj/b.obj\tbbbb\n"
                "2\t3\t3\tobj/a.obj\taaaa\n",
                encoding="utf-8",
            )
            self.assertEqual(pipeline._ninja_log_stats(log), (3, 2))
            self.assertEqual(pipeline._ninja_log_count(log), 3)

    def test_windows_compiler_slice_reserves_checkpoint_termination_margin(self):
        minimum = (
            pipeline.MIN_WINDOWS_COMPILER_SLICE_SECONDS
            + pipeline.WINDOWS_CHECKPOINT_TERMINATION_RESERVE_SECONDS
        )
        self.assertEqual(pipeline.compiler_slice_timeout_seconds(minimum), 0)
        self.assertEqual(
            pipeline.compiler_slice_timeout_seconds(minimum + 1),
            pipeline.MIN_WINDOWS_COMPILER_SLICE_SECONDS + 1,
        )
        self.assertEqual(
            pipeline.compiler_slice_timeout_seconds(18_043),
            17_743,
        )
        for invalid in (-1, True, 1.5, "900"):
            with self.subTest(invalid=invalid), self.assertRaises(
                pipeline.WindowsPipelineError
            ):
                pipeline.compiler_slice_timeout_seconds(invalid)

    def test_empty_ninja_exit_is_controlled_only_at_checkpoint_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            log = pathlib.Path(temp) / "build.log"
            log.write_text(
                "[10267/13791] CXX obj/chrome/example.obj\n"
                "ninja: error: \n"
                "ninja: build stopped: .\n",
                encoding="utf-8",
            )
            self.assertTrue(pipeline.is_empty_ninja_controller_exit(log))
            self.assertEqual(
                pipeline.normalize_checkpoint_boundary_status(
                    1,
                    durable_progress=True,
                    seconds_until_cutoff=62,
                    build_log=log,
                ),
                pipeline.TIMEOUT_EXIT_CODE,
            )
            for status, progress, seconds in (
                (2, True, 62),
                (1, False, 62),
                (1, True, pipeline.WINDOWS_CHECKPOINT_TERMINATION_RESERVE_SECONDS + 1),
                (1, True, -pipeline.WINDOWS_CHECKPOINT_BOUNDARY_LATE_SECONDS - 1),
            ):
                with self.subTest(status=status, progress=progress, seconds=seconds):
                    self.assertEqual(
                        pipeline.normalize_checkpoint_boundary_status(
                            status,
                            durable_progress=progress,
                            seconds_until_cutoff=seconds,
                            build_log=log,
                        ),
                        status,
                    )

            log.write_text(
                "FAILED: obj/chrome/example.obj\n"
                "clang-cl: error: deterministic source failure\n"
                "ninja: error: \n"
                "ninja: build stopped: .\n",
                encoding="utf-8",
            )
            self.assertFalse(pipeline.is_empty_ninja_controller_exit(log))
            self.assertEqual(
                pipeline.normalize_checkpoint_boundary_status(
                    1,
                    durable_progress=True,
                    seconds_until_cutoff=62,
                    build_log=log,
                ),
                1,
            )

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

    def test_gitiles_tag_identity_is_exact_and_utc_timestamped(self):
        payload = {
            "commit": "971a7443b0c9b0a9b2860529b33331b76077ec62",
            "committer": {"time": "Tue Aug 25 20:20:53 2026"},
            "message": (
                "Incrementing VERSION\n\n"
                "Cr-Commit-Position: refs/branch-heads/8010@{#343}\n"
            ),
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return (
                    "https://chromium.googlesource.com/chromium/src/+/refs/tags/"
                    "153.0.8010.12?format=JSON"
                )

            def read(self, _limit):
                return b")]}'\n" + json.dumps(payload).encode("utf-8")

        with mock.patch.object(
            pipeline.urllib.request, "urlopen", return_value=Response()
        ) as opener, mock.patch.object(pipeline.time, "time", return_value=1_800_000_000):
            identity = pipeline.fetch_gitiles_tag_identity("153.0.8010.12")
        self.assertEqual(
            identity.commit, "971a7443b0c9b0a9b2860529b33331b76077ec62"
        )
        self.assertEqual(identity.commit_position, "refs/branch-heads/8010@{#343}")
        self.assertEqual(identity.timestamp, 1_787_689_253)
        self.assertIn("/+/refs/tags/153.0.8010.12?format=JSON", opener.call_args.args[0].full_url)

    def test_gitiles_tag_identity_rejects_noncanonical_commit_position(self):
        payload = {
            "commit": "a" * 40,
            "committer": {"time": "Tue Aug 25 20:20:53 2026"},
            "message": "Cr-Commit-Position: refs/tags/8010@{#343}\n",
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return (
                    "https://chromium.googlesource.com/chromium/src/+/refs/tags/"
                    "153.0.8010.12?format=JSON"
                )

            def read(self, _limit):
                return b")]}'\n" + json.dumps(payload).encode("utf-8")

        with mock.patch.object(
            pipeline.urllib.request, "urlopen", return_value=Response()
        ), mock.patch.object(pipeline.time, "time", return_value=1_800_000_000):
            with self.assertRaisesRegex(
                pipeline.WindowsPipelineError, "canonical Chromium commit position"
            ):
                pipeline.fetch_gitiles_tag_identity("153.0.8010.12")

    def test_revision_metadata_uses_tag_time_and_preflights_linker_timestamp(self):
        identity = pipeline.ChromiumTagIdentity(
            commit="971a7443b0c9b0a9b2860529b33331b76077ec62",
            commit_position="refs/branch-heads/8010@{#343}",
            committer_time="Tue Aug 25 20:20:53 2026",
            timestamp=1_787_689_253,
        )
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp)
            script = source / "build/compute_build_timestamp.py"
            script.parent.mkdir(parents=True)
            script.write_text("print(1785646800)\n", encoding="utf-8")
            completed = pipeline.subprocess.CompletedProcess(
                ["python3.exe"], 0, "1785646800\n", ""
            )
            with mock.patch.object(pipeline, "_run", return_value=completed) as run:
                timestamp = pipeline.materialize_chromium_revision_metadata(
                    source,
                    pathlib.Path("python3.exe"),
                    {},
                    identity,
                )
            self.assertEqual(timestamp, 1_785_646_800)
            self.assertEqual(
                (source / "build/util/LASTCHANGE").read_text(encoding="utf-8"),
                "LASTCHANGE=971a7443b0c9b0a9b2860529b33331b76077ec62-"
                "refs/branch-heads/8010@{#343}\n",
            )
            self.assertEqual(
                (source / "build/util/LASTCHANGE.committime").read_text(
                    encoding="utf-8"
                ),
                "1787689253\n",
            )
            self.assertEqual(run.call_args.args[0][-1], "default")
            self.assertTrue(run.call_args.kwargs["capture"])

    def test_revision_metadata_rejects_the_negative_linker_timestamp_regression(self):
        identity = pipeline.ChromiumTagIdentity(
            commit="a" * 40,
            commit_position="refs/heads/main@{#1681091}",
            committer_time="Tue Aug 25 20:20:53 2026",
            timestamp=1_787_689_253,
        )
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp)
            script = source / "build/compute_build_timestamp.py"
            script.parent.mkdir(parents=True)
            script.write_text("print(-2142000)\n", encoding="utf-8")
            completed = pipeline.subprocess.CompletedProcess(
                ["python3.exe"], 0, "-2142000\n", ""
            )
            with mock.patch.object(pipeline, "_run", return_value=completed):
                with self.assertRaisesRegex(
                    pipeline.WindowsPipelineError,
                    "Chromium Windows build timestamp must be an integer",
                ):
                    pipeline.materialize_chromium_revision_metadata(
                        source,
                        pathlib.Path("python3.exe"),
                        {},
                        identity,
                    )

    def test_generated_chrome_linker_timestamp_resolves_renamed_executable(self):
        timestamp = "1785646800"

        def run(command, **_kwargs):
            if "deps" in command:
                stdout = (
                    "//chrome:renamed_browser_executable\n"
                    "//chrome:setup_helper\n"
                )
            elif command[-1] == "outputs" and command[-2].endswith(
                ":renamed_browser_executable"
            ):
                stdout = "//out/Release_x86_win/initialexe/chrome.exe\n"
            elif command[-1] == "outputs":
                stdout = "//out/Release_x86_win/setup_helper.exe\n"
            elif command[-1] == "ldflags":
                stdout = f"/MACHINE:X86\n/TIMESTAMP:{timestamp}\n"
            else:
                self.fail(f"Unexpected GN command: {command}")
            return pipeline.subprocess.CompletedProcess(command, 0, stdout, "")

        with mock.patch.object(pipeline, "_run", side_effect=run):
            stats = pipeline.validate_generated_chrome_linker_timestamp(
                pathlib.Path("src"),
                pathlib.Path("out"),
                pathlib.Path("gn.exe"),
                {},
                1_785_646_800,
            )
        self.assertEqual(stats["chrome_executable_dependency_count"], 2)
        self.assertEqual(
            stats["chrome_executable_label"],
            "//chrome:renamed_browser_executable",
        )
        self.assertEqual(stats["timestamp_occurrences"], 1)

        timestamp = "-2142000"
        with mock.patch.object(pipeline, "_run", side_effect=run):
            with self.assertRaisesRegex(
                pipeline.WindowsPipelineError, "observed=.*-2142000"
            ):
                pipeline.validate_generated_chrome_linker_timestamp(
                    pathlib.Path("src"),
                    pathlib.Path("out"),
                    pathlib.Path("gn.exe"),
                    {},
                    1_785_646_800,
                )

    def test_prepared_state_and_checkpoint_bind_tag_and_linker_timestamps(self):
        state = pipeline.PreparedState(
            schema=pipeline.PREPARED_STATE_SCHEMA,
            version="153.0.8010.12",
            source_sha256="a" * 64,
            chromium_commit="b" * 40,
            chromium_commit_position="refs/branch-heads/8010@{#343}",
            chromium_commit_timestamp=1_787_689_253,
            windows_build_timestamp=1_785_646_800,
            depot_tools_revision="c" * 40,
            gn_version="git_revision:" + "d" * 40,
            ninja_package="infra/3pp/tools/ninja/",
            ninja_version="version:3@1.13.2.chromium.1",
            cpython3_version="version:2@3.11.8.chromium.35",
            windows_cipd_tools_sha256="e" * 64,
            windows_gcs_tools_sha256="f" * 64,
            windows_git_tools_sha256="1" * 64,
            clang_revision="llvmorg-23-init-1234-gabcdef",
            sdk_family="10.0.28000.0",
            sdk_servicing="10.0.28000.2270",
            visual_studio_year="2026",
            visual_studio_version="18.0.0",
            port_config_hash_schema=pipeline.PORT_CONFIG_HASH_SCHEMA,
            port_config_sha256="2" * 64,
            checkpoint_no_progress_streak=0,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            pipeline.write_prepared_state(root, state)
            self.assertEqual(pipeline.read_prepared_state(root), state)
            payload = dict(state.__dict__)
            payload["windows_build_timestamp"] = state.chromium_commit_timestamp + 1
            (root / "prepared-state.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                pipeline.WindowsPipelineError, "newer than the tag commit"
            ):
                pipeline.read_prepared_state(root)

        proof = {
            "producer_sha": "3" * 40,
            "run_id": "123",
            "run_attempt": 1,
            "producer_stage": 1,
        }
        manifest = {
            "schema": pipeline.CHECKPOINT_MANIFEST_SCHEMA,
            "checkpoint_contract_version": pipeline.CHECKPOINT_CONTRACT_VERSION,
            "target_os": "win",
            "target_cpu": "x86",
            "output_root": pipeline.OUT_NAME,
            "version": state.version,
            "source_sha256": state.source_sha256,
            "chromium_commit": state.chromium_commit,
            "chromium_commit_position": state.chromium_commit_position,
            "chromium_commit_timestamp": state.chromium_commit_timestamp,
            "windows_build_timestamp": state.windows_build_timestamp,
            "depot_tools_revision": state.depot_tools_revision,
            "gn_version": state.gn_version,
            "ninja_package": state.ninja_package,
            "ninja_version": state.ninja_version,
            "cpython3_version": state.cpython3_version,
            "windows_cipd_tools_sha256": state.windows_cipd_tools_sha256,
            "windows_gcs_tools_sha256": state.windows_gcs_tools_sha256,
            "windows_git_tools_sha256": state.windows_git_tools_sha256,
            "clang_revision": state.clang_revision,
            "sdk_family": state.sdk_family,
            "sdk_servicing": state.sdk_servicing,
            "visual_studio_year": state.visual_studio_year,
            "visual_studio_version": state.visual_studio_version,
            "runner_image": pipeline.os.environ.get("ImageOS", "unknown"),
            "runner_image_version": pipeline.os.environ.get(
                "ImageVersion", "unknown"
            ),
            "resume_input_epoch": pipeline.WINDOWS_RESUME_INPUT_EPOCH,
            "port_config_hash_schema": state.port_config_hash_schema,
            "port_config_sha256": state.port_config_sha256,
            "github_sha": proof["producer_sha"],
            "github_run_id": proof["run_id"],
            "github_run_attempt": proof["run_attempt"],
            "stage": proof["producer_stage"],
            "no_progress_streak": 0,
        }
        compatibility = pipeline._checkpoint_manifest_matches_state(
            manifest, state, proof
        )
        self.assertEqual(compatibility.no_progress_streak, 0)
        self.assertFalse(compatibility.requires_gn_refresh)
        with mock.patch.dict(
            pipeline.os.environ,
            {"RUNNER_OS": "Linux", "ImageOS": "ubuntu24"},
            clear=False,
        ):
            compatibility = pipeline._checkpoint_manifest_matches_state(
                manifest, state, proof
            )
            self.assertFalse(compatibility.requires_gn_refresh)
        manifest["runner_image"] = "win25-vs2026"
        manifest["runner_image_version"] = "20260824.214.3"
        with mock.patch.dict(
            pipeline.os.environ,
            {
                "RUNNER_OS": "Windows",
                "ImageOS": "win25-vs2026",
                "ImageVersion": "20260824.214.3",
            },
            clear=False,
        ):
            compatibility = pipeline._checkpoint_manifest_matches_state(
                manifest, state, proof
            )
            self.assertFalse(compatibility.requires_gn_refresh)
            manifest["runner_image_version"] = "20260825.1"
            compatibility = pipeline._checkpoint_manifest_matches_state(
                manifest, state, proof
            )
            self.assertTrue(compatibility.requires_gn_refresh)
            self.assertEqual(
                compatibility.gn_refresh_fields, ("runner_image_version",)
            )
        manifest["runner_image_version"] = "20260824.214.3"
        manifest["sdk_servicing"] = "10.0.28000.9999"
        compatibility = pipeline._checkpoint_manifest_matches_state(
            manifest, state, proof
        )
        self.assertTrue(compatibility.requires_gn_refresh)
        self.assertEqual(compatibility.gn_refresh_fields, ("sdk_servicing",))
        manifest["sdk_servicing"] = state.sdk_servicing
        manifest["windows_build_timestamp"] = state.windows_build_timestamp + 1
        with self.assertRaisesRegex(
            pipeline.WindowsPipelineError, "windows_build_timestamp"
        ):
            pipeline._checkpoint_manifest_matches_state(manifest, state, proof)
        manifest["windows_build_timestamp"] = state.windows_build_timestamp

        migration = pipeline.CheckpointMigration(
            run_id=proof["run_id"],
            version=state.version,
            stage=proof["producer_stage"],
            producer_sha=proof["producer_sha"],
            port_config_sha256="4" * 64,
            archive_sha256="5" * 64,
        )
        manifest["port_config_sha256"] = migration.port_config_sha256
        compatibility = pipeline._checkpoint_manifest_matches_state(
            manifest,
            state,
            proof,
            migration=migration,
        )
        self.assertEqual(compatibility.migration_run_id, proof["run_id"])
        manifest["visual_studio_version"] = "19.0.0"
        with self.assertRaisesRegex(
            pipeline.WindowsPipelineError,
            "cannot cross runner toolchain drift",
        ):
            pipeline._checkpoint_manifest_matches_state(
                manifest,
                state,
                proof,
                migration=migration,
            )

    def test_approved_checkpoint_migration_is_exactly_scoped(self):
        migration = pipeline._resolve_checkpoint_migration(
            "33274094424",
            version="153.0.8010.12",
            stage=1,
        )
        self.assertIsNotNone(migration)
        self.assertEqual(
            migration.archive_sha256,
            "6f124bee59ed5693db7a0477f91ad57330f990cc0f32aa43fe0e9cabb426e058",
        )
        with self.assertRaisesRegex(
            pipeline.WindowsPipelineError, "exact approved migration scope"
        ):
            pipeline._resolve_checkpoint_migration(
                "33274094424",
                version="153.0.8010.12",
                stage=2,
            )
        self.assertIsNone(
            pipeline._resolve_checkpoint_migration(
                "99999999999",
                version="153.0.8010.12",
                stage=1,
            )
        )

        stage_four = pipeline._resolve_checkpoint_migration(
            "33357533082",
            version="153.0.8010.12",
            stage=4,
        )
        self.assertIsNotNone(stage_four)
        self.assertEqual(
            stage_four.producer_sha,
            "4bc44f0ba432ca2b3eaeac40811e2533daeb2e98",
        )
        self.assertEqual(
            stage_four.archive_sha256,
            "c81f702c589404b3f4ffa6bade8db08650f6e8cade30a45b274d8e0740497ce7",
        )
        with self.assertRaisesRegex(
            pipeline.WindowsPipelineError, "exact approved migration scope"
        ):
            pipeline._resolve_checkpoint_migration(
                "33357533082",
                version="153.0.8010.12",
                stage=5,
            )

        stage_five = pipeline._resolve_checkpoint_migration(
            "33390506701",
            version="153.0.8010.12",
            stage=5,
        )
        self.assertIsNotNone(stage_five)
        self.assertEqual(
            stage_five.producer_sha,
            "cbef7e08f1abc62d05715978ee4f96a02c13163b",
        )
        self.assertEqual(
            stage_five.port_config_sha256,
            "8b1d3f5e50c730efac4325089f1afbb68ead1490335fcf4689d4ec373b06d317",
        )
        self.assertEqual(
            stage_five.archive_sha256,
            "ace1e90426e6973d8ec4dabef9f73b5e106a9652003bfd2dfcde91723429f392",
        )
        with self.assertRaisesRegex(
            pipeline.WindowsPipelineError, "exact approved migration scope"
        ):
            pipeline._resolve_checkpoint_migration(
                "33390506701",
                version="153.0.8010.12",
                stage=6,
            )

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
        pipeline_source = (ROOT / "scripts/chromium_windows_pipeline.py").read_text(
            encoding="utf-8"
        )
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
        self.assertIn('("manifest_schema", "5")', pipeline_source)
        self.assertIn('("chromium_commit", state.chromium_commit)', pipeline_source)
        self.assertIn(
            '("windows_build_timestamp", str(state.windows_build_timestamp))',
            pipeline_source,
        )

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
        self.assertIn('ninja_root = work_root / "ninja"', tool_block)
        self.assertNotIn('ninja_root = source / "third_party/ninja"', tool_block)
        self.assertIn('"infra/3pp/tools/cpython3/windows-amd64"', tool_block)
        self.assertIn('pins["cpython3_version"]', tool_block)
        self.assertIn('source / "third_party/cpython3/host"', tool_block)
        self.assertIn('target_python = cpython_target / "bin/python3"', tool_block)
        self.assertIn("shutil.copy2(target_python_exe, target_python)", tool_block)
        self.assertIn("sha256_file(target_python)", tool_block)
        self.assertIn("install_windows_gcs_tools(", tool_block)
        self.assertIn('source / "buildtools/win-format/clang-format.exe"', source)
        self.assertIn('source / "third_party/node/win/node.exe"', source)
        self.assertIn('source / "third_party/node/node_modules"', source)
        self.assertIn("install_windows_cipd_tools(", tool_block)
        self.assertIn(
            'source / "third_party/typescript/windows-amd64/src/lib/tsc.exe"',
            source,
        )
        self.assertIn("devtools-frontend/src/third_party/esbuild/esbuild.exe", source)
        self.assertIn("devtools-frontend/src/third_party/rollup_libs", source)
        self.assertIn("scripts/deps/sync_rollup_libs.py", source)
        self.assertIn("rollup-win32-x64-msvc/package.json", source)
        self.assertIn('source / "third_party/dawn/DEPS"', source)
        self.assertIn("dawn/tools/golang/windows-amd64/bin/go.exe", source)
        self.assertIn("validate_dawn_source_generator(", source)
        self.assertIn('"GOTOOLCHAIN": "local"', source)
        self.assertIn('"--version"],', source)
        self.assertIn("install_windows_git_tools(", tool_block)
        self.assertIn('source / "third_party/gperf/bin/gperf.exe"', source)
        self.assertIn('source / "third_party/perl/perl/bin/perl.exe"', source)
        self.assertIn('rust_root / "bin/bindgen.exe"', source)
        self.assertIn('source / "third_party/llvm-libclang"', source)
        self.assertIn('"--print-revision",', source)
        self.assertIn('"validate",', source)

    def test_windows_gcs_tool_archive_extracts_regular_members_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "src"
            destination = source / "third_party/rust-toolchain"
            destination.parent.mkdir(parents=True)
            archive = root / "tool.tar.xz"
            self._write_xz_tar(
                archive,
                [
                    ("VERSION", b"rustc fixture\n"),
                    ("bin/bindgen.exe", b"MZfixture"),
                ],
            )
            stats = pipeline._extract_gcs_tool_archive(
                archive, destination, source_root=source
            )
            self.assertEqual(stats["member_count"], 2)
            self.assertEqual((destination / "VERSION").read_bytes(), b"rustc fixture\n")
            self.assertEqual((destination / "bin/bindgen.exe").read_bytes(), b"MZfixture")

    def test_windows_gcs_tool_archive_rejects_traversal_and_case_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "src"
            destination = source / "third_party/rust-toolchain"
            destination.parent.mkdir(parents=True)
            traversal = root / "traversal.tar.xz"
            self._write_xz_tar(traversal, [("../escape", b"bad")])
            with self.assertRaisesRegex(pipeline.WindowsPipelineError, "Unsafe"):
                pipeline._extract_gcs_tool_archive(
                    traversal, destination, source_root=source
                )
            self.assertFalse((root / "escape").exists())

            alias = root / "alias.tar.xz"
            self._write_xz_tar(alias, [("bin/Tool.exe", b"one"), ("bin/tool.exe", b"two")])
            with self.assertRaisesRegex(pipeline.WindowsPipelineError, "case-aliasing"):
                pipeline._extract_gcs_tool_archive(
                    alias, destination, source_root=source
                )

    def test_gn_args_use_trusted_file_not_multiline_command_argument(self):
        source = (ROOT / "scripts/chromium_windows_pipeline.py").read_text(
            encoding="utf-8"
        )
        block = source[
            source.index("def _validate_configured_gn_graph(") : source.index(
                "def _read_json_object(")
        ]
        self.assertIn('args_gn.write_text(WINDOWS_GN_ARGS', block)
        self.assertIn('[str(gn), "gen", str(out)]', block)
        self.assertNotIn("--args=", block)
        self.assertIn("validate_generated_chrome_linker_timestamp(", block)
        self.assertIn("source, out, gn, env, windows_build_timestamp", block)
        self.assertIn("def reuse_restored_gn_graph(", block)
        self.assertIn('"-n", "build.ninja"', block)
        self.assertIn("refusing silent graph regeneration", block)

    def test_prepare_traverses_full_ninja_input_graph_before_dispatch(self):
        source = (ROOT / "scripts" / "chromium_windows_pipeline.py").read_text(
            encoding="utf-8"
        )
        output_parent_pos = source.index('(source / "out").mkdir')
        normalize_pos = source.index("resume_input_stats =")
        restore_pos = source.index("restore_checkpoint(checkpoint_archive")
        self.assertLess(output_parent_pos, normalize_pos)
        self.assertLess(normalize_pos, restore_pos)
        block = source[
            normalize_pos : source.index("exports = {")
        ]
        self.assertIn("normalize_windows_resume_inputs(source)", block)
        self.assertIn("reuse_restored_gn_graph(", block)
        self.assertIn("requires_gn_refresh", block)
        self.assertIn("validate_ninja_input_closure(", block)
        self.assertNotIn("discard_stdout=True", block)
        self.assertIn("ninja-input-closure.json", block)

        closure = source[
            source.index("def validate_ninja_input_closure(") : source.index(
                "def prepare_pipeline("
            )
        ]
        self.assertIn('"-n",', closure)
        self.assertIn('"-t",', closure)
        self.assertIn('"inputs",', closure)
        self.assertIn('"missingdeps",', closure)
        self.assertIn('targets = ["chrome", "mini_installer"]', closure)
        self.assertIn("progress_databases_unchanged=true", closure)

    def test_read_only_ninja_closure_preserves_progress_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            out = source / "out" / pipeline.OUT_NAME
            out.mkdir(parents=True)
            ninja_log = out / ".ninja_log"
            ninja_deps = out / ".ninja_deps"
            ninja_log.write_text(
                "# ninja log v6\n1\t2\t3\tobj/a.obj\tdeadbeef\n",
                encoding="utf-8",
            )
            ninja_deps.write_bytes(b"exact dependency database")

            def run(command, **_kwargs):
                if "inputs" in command:
                    return pipeline.subprocess.CompletedProcess(
                        command,
                        0,
                        "../../a.cc\nobj/generated.h\n",
                        "",
                    )
                if "missingdeps" in command:
                    return pipeline.subprocess.CompletedProcess(
                        command,
                        0,
                        "Processed 156605 nodes.\n"
                        "No missing dependencies on generated files found.\n",
                        "",
                    )
                self.fail(f"Unexpected Ninja closure command: {command}")

            log_before = ninja_log.read_bytes()
            deps_before = ninja_deps.read_bytes()
            with mock.patch.object(pipeline, "_run", side_effect=run) as runner:
                stats = pipeline.validate_ninja_input_closure(
                    source,
                    out,
                    pathlib.Path("ninja.exe"),
                    {},
                )

            self.assertEqual(runner.call_count, 2)
            self.assertEqual(stats["manifest_input_count"], 2)
            self.assertEqual(stats["dependency_nodes_processed"], 156605)
            self.assertFalse(stats["build_simulation"])
            self.assertTrue(stats["state_unchanged"])
            self.assertEqual(ninja_log.read_bytes(), log_before)
            self.assertEqual(ninja_deps.read_bytes(), deps_before)

    def test_read_only_ninja_closure_rejects_progress_database_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "src"
            out = source / "out" / pipeline.OUT_NAME
            out.mkdir(parents=True)
            ninja_log = out / ".ninja_log"
            ninja_log.write_bytes(b"before")

            def run(command, **_kwargs):
                ninja_log.write_bytes(b"after")
                return pipeline.subprocess.CompletedProcess(
                    command,
                    0,
                    (
                        "../../a.cc\n"
                        if "inputs" in command
                        else "Processed 1 nodes.\n"
                        "No missing dependencies on generated files found.\n"
                    ),
                    "",
                )

            with mock.patch.object(pipeline, "_run", side_effect=run):
                with self.assertRaisesRegex(
                    pipeline.WindowsPipelineError,
                    "changed a progress database",
                ):
                    pipeline.validate_ninja_input_closure(
                        source,
                        out,
                        pathlib.Path("ninja.exe"),
                        {},
                    )


if __name__ == "__main__":
    unittest.main()
