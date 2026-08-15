import base64
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "chromium_source_object.py"
SPEC = importlib.util.spec_from_file_location("chromium_source_object", MODULE_PATH)
assert SPEC and SPEC.loader
source_object = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source_object
SPEC.loader.exec_module(source_object)

VERSION = "151.0.7922.108"

class ChromiumSourceObjectTests(unittest.TestCase):
    def _metadata(self, data: bytes):
        md5=hashlib.md5(data,usedforsecurity=False).digest()
        return {"url":source_object.source_download_url(VERSION),"generation":"12345","content_length":len(data),"md5_base64":base64.b64encode(md5).decode("ascii"),"etag":"etag"}

    def _gcs_payload(self, data: bytes):
        md5=hashlib.md5(data,usedforsecurity=False).digest()
        return {"bucket":source_object.SOURCE_BUCKET,"name":f"chromium-{VERSION}.tar.xz","generation":"12345","size":str(len(data)),"md5Hash":base64.b64encode(md5).decode("ascii"),"crc32c":"AAAAAA==","etag":"etag"}

    def test_fetch_metadata_uses_fixed_gcs_endpoint_and_strict_version(self):
        data=b"abc"
        result=subprocess.CompletedProcess(["curl"],0,json.dumps(self._gcs_payload(data))+"\n"+source_object.source_metadata_url(VERSION),"")
        with mock.patch.object(source_object.subprocess,"run",return_value=result) as run:
            metadata=source_object.fetch_metadata(VERSION)
        args=run.call_args.args[0]
        self.assertEqual(args[0],"curl")
        self.assertIn("https://storage.googleapis.com/storage/v1/b/chromium-browser-official/o/",args[-1])
        self.assertIn(f"chromium-{VERSION}.tar.xz",args[-1])
        self.assertIn("--proto", args)
        self.assertIn("--proto-redir", args)
        self.assertIn("\n%{url_effective}", args)
        self.assertEqual(metadata["content_length"],len(data))
        for bad in ("../etc/passwd","151","151.0.0.1?x=y","https://evil.invalid/"):
            with self.subTest(bad=bad), self.assertRaises(ValueError): source_object.fetch_metadata(bad)

    def test_fetch_metadata_rejects_wrong_bucket_object_or_malformed_hash(self):
        data=b"abc"
        for key,value in (("bucket","other"),("name","other.tar.xz"),("md5Hash","not-base64")):
            payload=self._gcs_payload(data); payload[key]=value
            result=subprocess.CompletedProcess(["curl"],0,json.dumps(payload)+"\n"+source_object.source_metadata_url(VERSION),"")
            with mock.patch.object(source_object.subprocess,"run",return_value=result), self.subTest(key=key), self.assertRaises(ValueError): source_object.fetch_metadata(VERSION)

    def test_fetch_metadata_rejects_untrusted_effective_host(self):
        data=b"abc"
        stdout=json.dumps(self._gcs_payload(data))+"\nhttps://evil.invalid/storage/v1/object"
        result=subprocess.CompletedProcess(["curl"],0,stdout,"")
        with mock.patch.object(source_object.subprocess,"run",return_value=result), self.assertRaises(ValueError):
            source_object.fetch_metadata(VERSION)

    def test_effective_host_validation_rejects_credentials_ports_and_http(self):
        for url in (
            "http://storage.googleapis.com/storage/v1/x",
            "https://evil.invalid/storage/v1/x",
            "https://user@storage.googleapis.com/storage/v1/x",
            "https://storage.googleapis.com:444/storage/v1/x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                source_object.validate_effective_https_host(url, source_object.GCS_METADATA_HOST)
        source_object.validate_effective_https_host(
            "https://storage.googleapis.com/storage/v1/x", source_object.GCS_METADATA_HOST
        )

    def test_fetch_metadata_curl_failure_is_closed(self):
        result=subprocess.CompletedProcess(["curl"],22,"","404")
        with mock.patch.object(source_object.subprocess,"run",return_value=result), self.assertRaises(ValueError): source_object.fetch_metadata(VERSION)

    def test_cache_key_is_bound_to_version_and_bounded_gcs_generation(self):
        metadata = self._metadata(b"abc")
        self.assertEqual(
            source_object.source_cache_key(VERSION, metadata),
            f"chromium-src-v4-{VERSION}-12345",
        )
        for generation in ("0", "-1", "abc", "1" * 41):
            changed = dict(metadata)
            changed["generation"] = generation
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                source_object.source_cache_key(VERSION, changed)
        changed = dict(metadata)
        changed["url"] = "https://evil.invalid/source.tar.xz"
        with self.assertRaises(ValueError):
            source_object.source_cache_key(VERSION, changed)

    def test_cache_key_only_cli_uses_metadata_generation(self):
        metadata = self._metadata(b"abc")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "metadata.json"
            path.write_text(json.dumps(metadata), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--version", VERSION, "--metadata-in", str(path), "--cache-key-only"],
                check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"chromium-src-v4-{VERSION}-12345")

    def test_verify_file_checks_gcs_length_md5_and_computes_sha256(self):
        data=b"chromium source bytes"; metadata=self._metadata(data)
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"source.tar.xz"; path.write_bytes(data); result=source_object.verify_file(path,metadata)
        self.assertEqual(result["md5_base64"],metadata["md5_base64"]); self.assertEqual(result["sha256"],hashlib.sha256(data).hexdigest())

    def test_verify_file_rejects_wrong_length_and_md5(self):
        data=b"abc"; metadata=self._metadata(data)
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"source.tar.xz"; path.write_bytes(data)
            wrong=dict(metadata); wrong["content_length"]=99
            with self.assertRaises(ValueError): source_object.verify_file(path,wrong)
            wrong=dict(metadata); wrong["md5_base64"]=base64.b64encode(b"x"*16).decode("ascii")
            with self.assertRaises(ValueError): source_object.verify_file(path,wrong)

    def test_marker_requires_generation_md5_length_sha_and_both_proofs(self):
        data=b"abc"; metadata=self._metadata(data); sha=hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            marker=pathlib.Path(tmp)/"marker.json"
            source_object.write_marker(marker,version=VERSION,metadata=metadata,sha256=sha,safe_archive=True,gitiles_identity=True)
            self.assertTrue(source_object.marker_matches(marker,version=VERSION,metadata=metadata,sha256=sha))
            for field,value in (("generation","x"),("content_length",999),("md5_base64","bad")):
                changed=dict(metadata); changed[field]=value
                self.assertFalse(source_object.marker_matches(marker,version=VERSION,metadata=changed,sha256=sha))
            with self.assertRaises(ValueError): source_object.write_marker(marker,version=VERSION,metadata=metadata,sha256=sha,safe_archive=False,gitiles_identity=True)

    def test_module_has_no_python_url_request_sink_or_url_cli(self):
        text=MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("urllib.request",text); self.assertNotIn('add_argument("--url")',text)
        self.assertIn("VERSION_RE",text)

if __name__ == "__main__": unittest.main()
