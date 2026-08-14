import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

MODULE_PATH=pathlib.Path(__file__).parents[1]/"scripts"/"github_release_state.py"
SPEC=importlib.util.spec_from_file_location("github_release_state",MODULE_PATH); assert SPEC and SPEC.loader
release_state=importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name]=release_state; SPEC.loader.exec_module(release_state)
TAG="chromium-151.0.7922.108-linux-i686"

class ReleaseStateTests(unittest.TestCase):
    def test_existing_release_is_true_and_uses_relative_gh_endpoint(self):
        cp=subprocess.CompletedProcess(["gh"],0,"{}","")
        with mock.patch.object(release_state,"_run_gh",return_value=cp) as run:
            self.assertTrue(release_state.release_exists("owner/repo",TAG,"token"))
        self.assertEqual(run.call_args.args[0],["api",f"repos/owner/repo/releases/tags/{TAG}"])

    def test_real_404_is_missing(self):
        cp=subprocess.CompletedProcess(["gh"],1,"","gh: Not Found (HTTP 404)")
        with mock.patch.object(release_state,"_run_gh",return_value=cp): self.assertFalse(release_state.release_exists("owner/repo",TAG,"token"))

    def test_non_404_failure_fails_closed(self):
        cp=subprocess.CompletedProcess(["gh"],1,"","gh: unavailable (HTTP 503)")
        with mock.patch.object(release_state,"_run_gh",return_value=cp), self.assertRaises(release_state.ReleaseStateError): release_state.release_exists("owner/repo",TAG,"token")

    def test_input_validation_and_invalid_json(self):
        with self.assertRaises(ValueError): release_state.release_exists("bad repo",TAG,"token")
        with self.assertRaises(ValueError): release_state.release_exists("owner/repo","bad-tag","token")
        with self.assertRaises(release_state.ReleaseStateError): release_state.release_exists("owner/repo",TAG,"")
        cp=subprocess.CompletedProcess(["gh"],0,"not-json","")
        with mock.patch.object(release_state,"_run_gh",return_value=cp), self.assertRaises(release_state.ReleaseStateError): release_state.release_exists("owner/repo",TAG,"token")

    def test_helper_has_no_python_url_request_sink(self):
        text=MODULE_PATH.read_text(encoding="utf-8"); self.assertNotIn("urllib.request",text); self.assertNotIn("https://api.github.com",text)

if __name__ == "__main__": unittest.main()
