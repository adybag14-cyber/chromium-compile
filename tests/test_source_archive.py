import importlib.util
import io
import json
import pathlib
import subprocess
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

    def test_rejects_archive_missing_expected_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            with tarfile.open(path, "w:xz") as archive:
                info, data = self._file("foreign/DEPS")
                archive.addfile(info, data)
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

    def test_reports_streaming_source_archive_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._file(self.ROOT + "/DEPS"), self._file(self.ROOT + "/BUILD.gn")])
            stats = validator.validate_source_archive(path, self.VERSION)
            self.assertEqual(stats["member_count"], 3)
            self.assertEqual(stats["unpacked_bytes"], 2)
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('mode="r|xz"', source)
        self.assertNotIn('mode="r:xz"', source)

    def test_rejects_source_member_count_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._file(self.ROOT + "/DEPS")])
            with self.assertRaisesRegex(ValueError, "member limit"):
                validator.validate_source_archive(path, self.VERSION, max_members=1)

    def test_rejects_source_unpacked_size_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._file(self.ROOT + "/DEPS"), self._file(self.ROOT + "/BUILD.gn")])
            with self.assertRaisesRegex(ValueError, "unpacked-byte limit"):
                validator.validate_source_archive(path, self.VERSION, max_unpacked_bytes=1)

    def test_source_validator_policy_overrides_have_hard_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            self._write(path, [self._file(self.ROOT + "/DEPS")])
            with self.assertRaisesRegex(ValueError, "hard maximum"):
                validator.validate_source_archive(path, self.VERSION, max_members=validator.HARD_MAX_MEMBERS + 1)
            with self.assertRaisesRegex(ValueError, "hard maximum"):
                validator.validate_source_archive(
                    path, self.VERSION, max_unpacked_bytes=validator.HARD_MAX_UNPACKED_GIB * 1024**3 + 1
                )

    def test_source_validator_cli_writes_sha_bound_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "source.tar.xz"
            stats_path = root / "stats.json"
            self._write(path, [self._file(self.ROOT + "/DEPS")])
            sha = "a" * 64
            result = subprocess.run(
                [
                    sys.executable, str(MODULE_PATH), str(path), "--version", self.VERSION,
                    "--source-sha256", sha, "--stats-file", str(stats_path),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_sha256"], sha)
            self.assertEqual(payload["version"], self.VERSION)
            self.assertEqual(payload["member_count"], 2)

    def test_source_validator_stats_require_sha_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "source.tar.xz"
            stats_path = root / "stats.json"
            self._write(path, [self._file(self.ROOT + "/DEPS")])
            result = subprocess.run(
                [
                    sys.executable, str(MODULE_PATH), str(path), "--version", self.VERSION,
                    "--stats-file", str(stats_path),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires --source-sha256", result.stderr)
            self.assertFalse(stats_path.exists())


if __name__ == "__main__":
    unittest.main()
