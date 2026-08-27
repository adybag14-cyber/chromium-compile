import importlib.util
import io
import pathlib
import struct
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "chromium_windows_runtime.py"
SPEC = importlib.util.spec_from_file_location("chromium_windows_runtime", PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)

VERSION = "153.0.8010.12"


def pe_bytes(*, machine=runtime.PE_MACHINE_I386, magic=runtime.PE32_OPTIONAL_MAGIC):
    data = bytearray(0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    struct.pack_into("<H", data, 0x98, magic)
    return bytes(data)


class WindowsRuntimeTests(unittest.TestCase):
    def _write_release(self, path: pathlib.Path, *, overrides=None, extra=None):
        root = runtime.release_root(VERSION)
        members = {
            f"{root}/mini_installer.exe": pe_bytes(),
            f"{root}/Chrome-bin/{VERSION}/chrome.exe": pe_bytes(),
            f"{root}/Chrome-bin/{VERSION}/chrome.dll": pe_bytes(),
            f"{root}/Chrome-bin/{VERSION}/chrome_elf.dll": pe_bytes(),
            f"{root}/Chrome-bin/{VERSION}/icudtl.dat": b"icu",
            f"{root}/Chrome-bin/{VERSION}/resources.pak": b"pak",
            f"{root}/Chrome-bin/{VERSION}/locales/en-US.pak": b"locale",
        }
        if overrides:
            members.update(overrides)
        if extra:
            members.update(extra)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                if data is not None:
                    archive.writestr(name, data)

    def test_pe32_i386_is_accepted(self):
        runtime.validate_pe32_stream(io.BytesIO(pe_bytes()), "fixture.exe")

    def test_pe32_rejects_x64_and_pe32_plus(self):
        with self.assertRaises(runtime.WindowsRuntimeError):
            runtime.validate_pe32_stream(io.BytesIO(pe_bytes(machine=0x8664)), "x64.exe")
        with self.assertRaises(runtime.WindowsRuntimeError):
            runtime.validate_pe32_stream(io.BytesIO(pe_bytes(magic=0x20B)), "plus.exe")

    def test_release_zip_requires_complete_pe32_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / "release.zip"
            self._write_release(archive)
            stats = runtime.validate_release_zip(archive, VERSION)
            self.assertEqual(stats["pe32_count"], 4)
            self.assertGreaterEqual(stats["member_count"], 7)

    def test_release_zip_rejects_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / "release.zip"
            root = runtime.release_root(VERSION)
            self._write_release(
                archive,
                overrides={f"{root}/Chrome-bin/{VERSION}/chrome.dll": pe_bytes(machine=0x8664)},
            )
            with self.assertRaisesRegex(runtime.WindowsRuntimeError, "not Intel i386"):
                runtime.validate_release_zip(archive, VERSION)

    def test_release_zip_rejects_traversal_and_wrong_root(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / "release.zip"
            self._write_release(archive, extra={"../escape.exe": pe_bytes()})
            with self.assertRaises(runtime.WindowsRuntimeError):
                runtime.validate_release_zip(archive, VERSION)

    def test_release_zip_rejects_missing_locale(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / "release.zip"
            root = runtime.release_root(VERSION)
            self._write_release(
                archive,
                overrides={f"{root}/Chrome-bin/{VERSION}/locales/en-US.pak": None},
            )
            with self.assertRaisesRegex(runtime.WindowsRuntimeError, "locales/en-US"):
                runtime.validate_release_zip(archive, VERSION)

    def test_7z_listing_parser_preserves_equals_in_paths(self):
        records = runtime._parse_7z_records(
            "Path = Chrome-bin/a=b.pak\nSize = 7\nAttributes = A\n\n"
        )
        self.assertEqual(records[0]["Path"], "Chrome-bin/a=b.pak")
        self.assertEqual(records[0]["Size"], "7")

    def test_safe_member_rejects_windows_drive_and_backslash(self):
        for value in ("C:/escape.exe", "folder\\escape.exe", "/escape.exe"):
            with self.subTest(value=value), self.assertRaises(runtime.WindowsRuntimeError):
                runtime._safe_member_name(value)


if __name__ == "__main__":
    unittest.main()
