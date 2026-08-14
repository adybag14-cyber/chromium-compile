import importlib.util
import io
import os
import pathlib
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[1]


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
  'gn_version': 'git_revision:0123456789abcdef',
}

deps = {
  'src/third_party/depot_tools':
    Var('chromium_git') + '/chromium/tools/depot_tools.git' + '@' + '0123456789abcdef0123456789abcdef01234567',
}
""",
                encoding="utf-8",
            )
            self.assertEqual(
                tool_pins.resolve_pins(deps),
                {
                    "gn_version": "git_revision:0123456789abcdef",
                    "depot_tools_revision": "0123456789abcdef0123456789abcdef01234567",
                },
            )

    def test_missing_pin_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = pathlib.Path(tmp) / "DEPS"
            deps.write_text("vars = {}\n", encoding="utf-8")
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

    def test_renders_standalone_wrapper_from_upstream_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = self._fixture(pathlib.Path(tmp))
            target = runtime.render_standalone_wrapper(out)
            text = target.read_text(encoding="utf-8")
            self.assertIn("chrome", text)
            self.assertNotIn("@@PROGNAME", text)
            self.assertNotIn("@@channel", text)
            if os.name != "nt":
                self.assertTrue(target.stat().st_mode & 0o111)
            self.assertIn("target.chmod(0o755)", (ROOT / "scripts" / "chromium_linux_runtime.py").read_text(encoding="utf-8"))

    def test_missing_required_output_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = self._fixture(pathlib.Path(tmp))
            (out / "libEGL.so").unlink()
            with self.assertRaises(ValueError):
                runtime.collect_runtime(source, out)


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



if __name__ == "__main__":
    unittest.main()
