import base64
import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from email.message import Message
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "chromium_source_object.py"
SPEC = importlib.util.spec_from_file_location("chromium_source_object", MODULE_PATH)
assert SPEC and SPEC.loader
source_object = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source_object
SPEC.loader.exec_module(source_object)


class FakeResponse:
    def __init__(self, headers: Message):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ChromiumSourceObjectTests(unittest.TestCase):
    def _metadata(self, data: bytes):
        md5 = hashlib.md5(data, usedforsecurity=False).digest()
        return {
            "url": "https://example.invalid/source.tar.xz",
            "generation": "12345",
            "content_length": len(data),
            "md5_base64": base64.b64encode(md5).decode("ascii"),
            "etag": md5.hex(),
        }

    def test_verify_file_checks_gcs_length_md5_and_computes_sha256(self):
        data = b"chromium source bytes"
        metadata = self._metadata(data)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            path.write_bytes(data)
            result = source_object.verify_file(path, metadata)
        self.assertEqual(result["md5_base64"], metadata["md5_base64"])
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())

    def test_verify_file_rejects_wrong_length(self):
        data = b"abc"
        metadata = self._metadata(data)
        metadata["content_length"] = 99
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                source_object.verify_file(path, metadata)

    def test_verify_file_rejects_wrong_md5(self):
        data = b"abc"
        metadata = self._metadata(data)
        metadata["md5_base64"] = base64.b64encode(b"x" * 16).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "source.tar.xz"
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                source_object.verify_file(path, metadata)

    def test_marker_requires_generation_md5_length_sha_and_both_proofs(self):
        data = b"abc"
        metadata = self._metadata(data)
        sha = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            marker = pathlib.Path(tmp) / "marker.json"
            source_object.write_marker(marker, version="1.2.3.4", metadata=metadata, sha256=sha)
            self.assertTrue(source_object.marker_matches(marker, version="1.2.3.4", metadata=metadata, sha256=sha))
            changed = dict(metadata)
            changed["generation"] = "54321"
            self.assertFalse(source_object.marker_matches(marker, version="1.2.3.4", metadata=changed, sha256=sha))
            changed = dict(metadata)
            changed["md5_base64"] = base64.b64encode(b"z" * 16).decode("ascii")
            self.assertFalse(source_object.marker_matches(marker, version="1.2.3.4", metadata=changed, sha256=sha))
            self.assertFalse(source_object.marker_matches(marker, version="1.2.3.4", metadata=metadata, sha256="0" * 64))

    def test_fetch_metadata_requires_consistent_gcs_hash_headers(self):
        data = b"abc"
        md5 = hashlib.md5(data, usedforsecurity=False).digest()
        headers = Message()
        headers.add_header("x-goog-hash", "crc32c=AAAAAA==")
        headers.add_header("x-goog-hash", "md5=" + base64.b64encode(md5).decode("ascii"))
        headers["x-goog-generation"] = "123"
        headers["x-goog-stored-content-length"] = str(len(data))
        headers["ETag"] = '"' + md5.hex() + '"'
        with mock.patch.object(source_object.urllib.request, "urlopen", return_value=FakeResponse(headers)):
            metadata = source_object.fetch_metadata("https://example.invalid")
        self.assertEqual(metadata["generation"], "123")
        self.assertEqual(metadata["content_length"], len(data))

    def test_fetch_metadata_rejects_etag_md5_disagreement(self):
        data = b"abc"
        md5 = hashlib.md5(data, usedforsecurity=False).digest()
        headers = Message()
        headers.add_header("x-goog-hash", "md5=" + base64.b64encode(md5).decode("ascii"))
        headers["x-goog-generation"] = "123"
        headers["x-goog-stored-content-length"] = str(len(data))
        headers["ETag"] = '"' + ('0' * 32) + '"'
        with mock.patch.object(source_object.urllib.request, "urlopen", return_value=FakeResponse(headers)):
            with self.assertRaises(ValueError):
                source_object.fetch_metadata("https://example.invalid")


if __name__ == "__main__":
    unittest.main()
