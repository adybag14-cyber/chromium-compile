import importlib.util
import io
import pathlib
import sys
import tarfile
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "validate_chromium_source_archive.py"
SPEC = importlib.util.spec_from_file_location("validate_chromium_source_archive", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


class ChromiumSourceArchiveTests(unittest.TestCase):
    VERSION = "151.0.7922.108"
    ROOT = "chromium-151.0.7922.108"

    def _write(self, path: pathlib.Path, members):
        with tarfile.open(path, "w:xz") as archive:
            root = tarfile.TarInfo(self.ROOT + "/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            archive.addfile(root)
            for member in members:
                archive.addfile(*member)

    def _file(self, name: str):
        data = b"x"
        info = tarfile.TarInfo(name)
        info.size = len(data)
        return info, io.BytesIO(data)

    def _symlink(self, name: str, target: str):
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        return info, None

    def test_accepts_normal_tree_and_internal_relative_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(
                path,
                [
                    self._file(self.ROOT + "/DEPS"),
                    self._file(self.ROOT + "/third_party/tool/file"),
                    self._symlink(self.ROOT + "/link", "third_party/tool/file"),
                    self._symlink(self.ROOT + "/third_party/tool/up", "../../DEPS"),
                ],
            )
            validator.validate_source_archive(path, self.VERSION)

    def test_rejects_path_traversal_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._file(self.ROOT + "/../escape")])
            with self.assertRaises(ValueError):
                validator.validate_source_archive(path, self.VERSION)

    def test_rejects_wrong_top_level_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._file("chromium-other/DEPS")])
            with self.assertRaises(ValueError):
                validator.validate_source_archive(path, self.VERSION)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._symlink(self.ROOT + "/link", "../../escape")])
            with self.assertRaises(ValueError):
                validator.validate_source_archive(path, self.VERSION)

    def test_rejects_special_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            fifo = tarfile.TarInfo(self.ROOT + "/fifo")
            fifo.type = tarfile.FIFOTYPE
            self._write(path, [(fifo, None)])
            with self.assertRaises(ValueError):
                validator.validate_source_archive(path, self.VERSION)


if __name__ == "__main__":
    unittest.main()
