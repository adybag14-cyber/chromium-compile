import io
import importlib.util
import pathlib
import sys
import tarfile
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "validate_checkpoint_archive.py"
SPEC = importlib.util.spec_from_file_location("validate_checkpoint_archive", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, data)
    return buf.getvalue()


def regular(name, data=b"x"):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, io.BytesIO(data)


def directory(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


def symlink(name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def fifo(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.FIFOTYPE
    return info, None


class FakeProcess:
    def __init__(self, data, rc=0, stderr=b""):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(stderr)
        self.rc = rc
        self.killed = False
    def wait(self, timeout=None): return self.rc
    def kill(self): self.killed = True


class CheckpointArchiveTests(unittest.TestCase):
    def test_accepts_explicit_windows_output_root_without_weakening_default(self):
        def members():
            return [
                directory("Release_x86_win"),
                regular("Release_x86_win/build.ninja"),
                regular("Release_x86_win/args.gn"),
            ]

        with mock.patch.object(
            validator.subprocess, "Popen", return_value=FakeProcess(tar_bytes(members()))
        ):
            stats = validator.validate_checkpoint(
                pathlib.Path("checkpoint.tar.zst"), root="Release_x86_win"
            )
        self.assertEqual(stats["member_count"], 3)
        with mock.patch.object(
            validator.subprocess, "Popen", return_value=FakeProcess(tar_bytes(members()))
        ):
            with self.assertRaises(ValueError):
                validator.validate_checkpoint(pathlib.Path("checkpoint.tar.zst"))

    def _validate(self, members, rc=0):
        fake = FakeProcess(tar_bytes(members), rc=rc)
        with mock.patch.object(validator.subprocess, "Popen", return_value=fake):
            validator.validate_checkpoint(pathlib.Path("checkpoint.tar.zst"))
        return fake

    def test_accepts_contained_checkpoint_and_internal_symlink(self):
        self._validate([
            directory("Release_x86"),
            regular("Release_x86/build.ninja"),
            regular("Release_x86/args.gn"),
            regular("Release_x86/libfoo.so"),
            symlink("Release_x86/libfoo-current.so", "libfoo.so"),
        ])

    def test_rejects_path_traversal_and_absolute_member(self):
        for name in ("../evil", "/tmp/evil", "other/build.ninja"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self._validate([
                    regular("Release_x86/build.ninja"),
                    regular("Release_x86/args.gn"),
                    regular(name),
                ])

    def test_rejects_escaping_absolute_and_broken_links(self):
        for target in ("../../outside", "/etc/passwd", "missing.so"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self._validate([
                    regular("Release_x86/build.ninja"),
                    regular("Release_x86/args.gn"),
                    symlink("Release_x86/sub/link", target),
                ])

    def test_rejects_special_file(self):
        with self.assertRaises(ValueError):
            self._validate([
                regular("Release_x86/build.ninja"),
                regular("Release_x86/args.gn"),
                fifo("Release_x86/pipe"),
            ])

    def test_rejects_missing_required_graph_files(self):
        with self.assertRaises(ValueError):
            self._validate([regular("Release_x86/build.ninja")])

    def test_rejects_duplicate_member_names(self):
        with self.assertRaises(ValueError):
            self._validate([
                regular("Release_x86/build.ninja"),
                regular("Release_x86/args.gn"),
                regular("Release_x86/args.gn"),
            ])

    def test_rejects_zstd_failure(self):
        with self.assertRaises(ValueError):
            self._validate([
                regular("Release_x86/build.ninja"),
                regular("Release_x86/args.gn"),
            ], rc=1)


    def test_rejects_declared_unpacked_size_over_limit(self):
        fake = FakeProcess(tar_bytes([
            regular("Release_x86/build.ninja", b"abc"),
            regular("Release_x86/args.gn", b"def"),
        ]))
        with mock.patch.dict(validator.os.environ, {"CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_BYTES": "5"}), \
             mock.patch.object(validator.subprocess, "Popen", return_value=fake):
            with self.assertRaises(ValueError):
                validator.validate_checkpoint(pathlib.Path("checkpoint.tar.zst"))

    def test_rejects_member_count_over_limit(self):
        members = [
            regular("Release_x86/build.ninja"),
            regular("Release_x86/args.gn"),
            regular("Release_x86/extra"),
        ]
        fake = FakeProcess(tar_bytes(members))
        with mock.patch.dict(validator.os.environ, {"CHROMIUM_I686_MAX_CHECKPOINT_MEMBERS": "2"}), \
             mock.patch.object(validator.subprocess, "Popen", return_value=fake):
            with self.assertRaises(ValueError):
                validator.validate_checkpoint(pathlib.Path("checkpoint.tar.zst"))



    def test_checkpoint_policy_overrides_have_hard_caps(self):
        for env in (
            {"CHROMIUM_I686_MAX_CHECKPOINT_MEMBERS": str(validator.HARD_MAX_MEMBERS + 1)},
            {"CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_GIB": str(validator.HARD_MAX_UNPACKED_GIB + 1)},
            {"CHROMIUM_I686_MAX_CHECKPOINT_UNPACKED_BYTES": str(validator.HARD_MAX_UNPACKED_GIB * 1024**3 + 1)},
        ):
            members = [regular("Release_x86/build.ninja"), regular("Release_x86/args.gn")]
            fake = FakeProcess(tar_bytes(members))
            with self.subTest(env=env), mock.patch.dict(validator.os.environ, env), \
                 mock.patch.object(validator.subprocess, "Popen", return_value=fake), \
                 self.assertRaisesRegex(ValueError, "hard maximum"):
                validator.validate_checkpoint(pathlib.Path("checkpoint.tar.zst"))

    def test_cli_entrypoint_validates_archive_path(self):
        fake = FakeProcess(tar_bytes([
            regular("Release_x86/build.ninja"),
            regular("Release_x86/args.gn"),
        ]))
        with mock.patch.object(validator.subprocess, "Popen", return_value=fake), \
             mock.patch.object(sys, "argv", ["validate_checkpoint_archive.py", "checkpoint.tar.zst"]):
            self.assertEqual(validator.main(), 0)

    def test_validator_invokes_zstd_without_shell(self):
        fake = FakeProcess(tar_bytes([
            regular("Release_x86/build.ninja"),
            regular("Release_x86/args.gn"),
        ]))
        with mock.patch.object(validator.subprocess, "Popen", return_value=fake) as popen:
            validator.validate_checkpoint(pathlib.Path("checkpoint.tar.zst"))
        self.assertEqual(popen.call_args.args[0], ["zstd", "-q", "-d", "-c", "checkpoint.tar.zst"])
        self.assertNotIn("shell", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
