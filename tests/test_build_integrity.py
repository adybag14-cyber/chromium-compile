import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[1]
MODULE_RELEASE_ARCHIVE = ROOT / "scripts" / "validate_release_archive.py"


def load_module(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tool_pins = load_module("chromium_tool_pins", "scripts/chromium_tool_pins.py")
runtime = load_module("chromium_linux_runtime", "scripts/chromium_linux_runtime.py")
archive_validator = load_module("validate_release_archive", "scripts/validate_release_archive.py")


class ToolPinTests(unittest.TestCase):
    def test_resolves_chromium_declared_gn_and_depot_tools_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = pathlib.Path(tmp) / "DEPS"
            deps.write_text(
                """
vars = {
  'gn_version': 'git_revision:0123456789abcdef0123456789abcdef01234567',
}

deps = {
  'src/unrelated': 'https://example.invalid/repo.git@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'src/third_party/depot_tools':
    Var('chromium_git') + '/chromium/tools/depot_tools.git' + '@' + '0123456789abcdef0123456789abcdef01234567',
}
""",
                encoding="utf-8",
            )
            self.assertEqual(
                tool_pins.resolve_pins(deps),
                {
                    "gn_version": "git_revision:0123456789abcdef0123456789abcdef01234567",
                    "depot_tools_revision": "0123456789abcdef0123456789abcdef01234567",
                },
            )

    def test_missing_pin_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = pathlib.Path(tmp) / "DEPS"
            deps.write_text("vars = {}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                tool_pins.resolve_pins(deps)

    def test_mutable_gn_tag_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = pathlib.Path(tmp) / "DEPS"
            deps.write_text(
                """
vars = {'gn_version': 'latest'}
deps = {
  'src/third_party/depot_tools':
    Var('chromium_git') + '/chromium/tools/depot_tools.git' + '@' + '0123456789abcdef0123456789abcdef01234567',
}
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                tool_pins.resolve_pins(deps)



class RuntimeCollectorTests(unittest.TestCase):
    def _fixture(self, root: pathlib.Path):
        source = root / "src"
        out = root / "out"
        build = source / "chrome" / "installer" / "linux" / "BUILD.gn"
        build.parent.mkdir(parents=True)
        build.write_text(
            """
packaging_files_executables = [
  "$root_out_dir/chrome",
  "$root_out_dir/chrome_crashpad_handler",
  "$root_out_dir/chrome_management_service",
  "$root_out_dir/chrome_sandbox",
]
packaging_files_shlibs = [
  "$root_out_dir/libEGL.so",
  "$root_out_dir/libGLESv2.so",
]
if (enable_swiftshader) {
  packaging_files_shlibs += [ "$root_out_dir/libvk_swiftshader.so" ]
}
packaging_files = packaging_files_executables + packaging_files_shlibs + [
  "$root_out_dir/locales/en-US.pak",
  "$root_out_dir/MEIPreload/manifest.json",
  "$root_out_dir/MEIPreload/preloaded_data.pb",
]
if (enable_swiftshader) {
  packaging_files += [ "$root_out_dir/vk_swiftshader_icd.json" ]
}
copy("common_packaging_files") {
  sources = [ "common/wrapper" ]
}
action_foreach("calculate_deb_dependencies") {}
""",
            encoding="utf-8",
        )
        for rel in runtime.REQUIRED_RUNTIME - {"locales"}:
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        (out / "locales").mkdir(parents=True)
        (out / "locales" / "en-US.pak").write_bytes(b"locale")
        (out / "libvk_swiftshader.so").write_bytes(b"swift")
        (out / "vk_swiftshader_icd.json").write_text("{}", encoding="utf-8")
        (out / "future_resource.pak").write_bytes(b"pak")
        (out / "MEIPreload").mkdir(parents=True)
        (out / "MEIPreload" / "manifest.json").write_text("{}", encoding="utf-8")
        (out / "MEIPreload" / "preloaded_data.pb").write_bytes(b"pb")
        wrapper = out / "installer" / "common" / "wrapper"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/bin/bash\nexport CHROME_VERSION_EXTRA=\"@@channel\"\nexec -a \"$0\" \"$HERE/@@PROGNAME\" \"$@\"\n", encoding="utf-8")
        return source, out

    def test_collects_upstream_runtime_and_future_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = self._fixture(pathlib.Path(tmp))
            files = runtime.collect_runtime(source, out)
            self.assertTrue(runtime.REQUIRED_RUNTIME.issubset(files))
            self.assertIn("libvk_swiftshader.so", files)
            self.assertIn("vk_swiftshader_icd.json", files)
            self.assertIn("future_resource.pak", files)
            self.assertIn("MEIPreload", files)
            self.assertNotIn("MEIPreload/manifest.json", files)
            self.assertNotIn("MEIPreload/preloaded_data.pb", files)

    def test_renders_standalone_wrapper_from_upstream_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            _source, out = self._fixture(pathlib.Path(tmp))
            target = runtime.render_standalone_wrapper(out)
            text = target.read_text(encoding="utf-8")
            self.assertIn("chrome", text)
            self.assertNotIn("@@PROGNAME", text)
            self.assertNotIn("@@channel", text)
            if os.name != "nt":
                self.assertTrue(target.stat().st_mode & 0o111)

    def test_missing_required_output_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = self._fixture(pathlib.Path(tmp))
            (out / "libEGL.so").unlink()
            with self.assertRaises(ValueError):
                runtime.collect_runtime(source, out)

    def test_missing_installer_definition_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "source"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "installer definition is unavailable"):
                runtime.installer_runtime_candidates(source)


class ReleaseArchiveTests(unittest.TestCase):
    def _archive(self, path: pathlib.Path, names: list[str]):
        with tarfile.open(path, "w:xz") as archive:
            for name in names:
                data = b"runtime"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if name in {"chrome", "chrome_crashpad_handler", "chrome_management_service", "chrome_sandbox"} else 0o644
                archive.addfile(info, io.BytesIO(data))

    def test_accepts_complete_safe_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            self._archive(path, names)
            archive_validator.validate_archive(path)

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak", "../escape"]
            self._archive(path, names)
            with self.assertRaises(ValueError):
                archive_validator.validate_archive(path)

    def test_rejects_duplicate_member_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            with tarfile.open(path, "w:xz") as archive:
                for name in names + ["chrome"]:
                    data = b"runtime"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            with self.assertRaises(ValueError):
                archive_validator.validate_archive(path)

    def test_rejects_fifo_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            with tarfile.open(path, "w:xz") as archive:
                for name in names:
                    data = b"runtime"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                fifo = tarfile.TarInfo("runtime.fifo")
                fifo.type = tarfile.FIFOTYPE
                archive.addfile(fifo)
            with self.assertRaises(ValueError):
                archive_validator.validate_archive(path)

    def test_rejects_unsafe_link_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            with tarfile.open(path, "w:xz") as archive:
                for name in names:
                    data = b"runtime"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                link = tarfile.TarInfo("escape-link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside"
                archive.addfile(link)
            with self.assertRaises(ValueError):
                archive_validator.validate_archive(path)

    def test_rejects_missing_required_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales", "chrome_sandbox"}) + ["locales/en-US.pak"]
            self._archive(path, names)
            with self.assertRaises(ValueError):
                archive_validator.validate_archive(path)

    def test_rejects_required_runtime_stored_as_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales", "chrome"}) + ["locales/en-US.pak"]
            with tarfile.open(path, "w:xz") as archive:
                for name in names:
                    data = b"runtime"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                link = tarfile.TarInfo("chrome")
                link.type = tarfile.SYMTYPE
                link.linkname = "resources.pak"
                archive.addfile(link)
            with self.assertRaises(ValueError):
                archive_validator.validate_archive(path)

    def test_reports_streaming_archive_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            self._archive(path, names)
            stats = archive_validator.validate_archive(path)
            self.assertEqual(stats["member_count"], len(names))
            self.assertEqual(stats["unpacked_bytes"], len(names) * len(b"runtime"))
        source = MODULE_RELEASE_ARCHIVE.read_text(encoding="utf-8")
        self.assertIn('mode="r|xz"', source)
        self.assertNotIn("getmembers()", source)

    def test_rejects_release_member_count_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            self._archive(path, names)
            with self.assertRaisesRegex(ValueError, "member limit"):
                archive_validator.validate_archive(path, max_members=len(names) - 1)

    def test_rejects_release_unpacked_size_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundle.tar.xz"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            self._archive(path, names)
            with self.assertRaisesRegex(ValueError, "unpacked-byte limit"):
                archive_validator.validate_archive(path, max_unpacked_bytes=1)

    def test_release_validator_cli_writes_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "bundle.tar.xz"
            stats_path = root / "stats.json"
            names = sorted(runtime.REQUIRED_RUNTIME - {"locales"}) + ["locales/en-US.pak"]
            self._archive(path, names)
            result = subprocess.run(
                [sys.executable, str(MODULE_RELEASE_ARCHIVE), str(path), "--stats-file", str(stats_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["member_count"], len(names))
            self.assertGreater(payload["unpacked_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
