import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

MODULE_PATH=pathlib.Path(__file__).parents[1]/"scripts"/"github_release_tag.py"
SPEC=importlib.util.spec_from_file_location("github_release_tag",MODULE_PATH); assert SPEC and SPEC.loader
tag_state=importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name]=tag_state; SPEC.loader.exec_module(tag_state)
SHA="a"*40; OTHER_SHA="b"*40; TAG="chromium-151.0.7922.108-linux-i686"

def cp(payload, rc=0, err=""):
    out=json.dumps(payload) if not isinstance(payload,str) else payload
    return subprocess.CompletedProcess(["gh"],rc,out,err)

class ReleaseTagTests(unittest.TestCase):
    def test_resolve_lightweight_tag_uses_exact_relative_tag_ref(self):
        with mock.patch.object(tag_state,"_run_gh",return_value=cp({"object":{"type":"commit","sha":SHA}})) as run:
            self.assertEqual(tag_state.resolve_tag_commit("owner/repo",TAG,"token"),SHA)
        self.assertEqual(run.call_args.args[0],["api",f"repos/owner/repo/git/ref/tags/{TAG}"])

    def test_resolve_annotated_tag_dereferences_to_commit(self):
        tag_sha="c"*40
        responses=[cp({"object":{"type":"tag","sha":tag_sha}}),cp({"object":{"type":"commit","sha":SHA}})]
        with mock.patch.object(tag_state,"_run_gh",side_effect=responses) as run:
            self.assertEqual(tag_state.resolve_tag_commit("owner/repo",TAG,"token"),SHA)
        self.assertEqual(run.call_count,2); self.assertEqual(run.call_args_list[1].args[0],["api",f"repos/owner/repo/git/tags/{tag_sha}"])

    def test_tag_lookup_404_is_missing_non404_fails(self):
        missing=subprocess.CompletedProcess(["gh"],1,"","gh: Not Found (HTTP 404)")
        with mock.patch.object(tag_state,"_run_gh",return_value=missing): self.assertIsNone(tag_state.resolve_tag_commit("owner/repo",TAG,"token"))
        failure=subprocess.CompletedProcess(["gh"],1,"","gh: unavailable (HTTP 503)")
        with mock.patch.object(tag_state,"_run_gh",return_value=failure), self.assertRaises(tag_state.TagStateError): tag_state.resolve_tag_commit("owner/repo",TAG,"token")

    def test_existing_exact_tag_is_accepted_without_write(self):
        with mock.patch.object(tag_state,"resolve_tag_commit",return_value=SHA), mock.patch.object(tag_state,"_run_gh") as run:
            self.assertEqual(tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"token"),"already-exact")
        run.assert_not_called()

    def test_existing_wrong_tag_fails_before_write(self):
        with mock.patch.object(tag_state,"resolve_tag_commit",return_value=OTHER_SHA), mock.patch.object(tag_state,"_run_gh") as run, self.assertRaises(tag_state.TagStateError): tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"token")
        run.assert_not_called()

    def test_missing_tag_is_created_once_then_confirmed(self):
        with mock.patch.object(tag_state,"resolve_tag_commit",side_effect=[None,SHA]), mock.patch.object(tag_state,"_run_gh",return_value=cp({"ref":f"refs/tags/{TAG}"})) as run, mock.patch.object(tag_state.time,"sleep"):
            self.assertEqual(tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"token"),"created")
        self.assertEqual(run.call_count,1)
        args=run.call_args.args[0]; self.assertEqual(args[:4],["api","--method","POST","repos/owner/repo/git/refs"]); self.assertIn(f"ref=refs/tags/{TAG}",args); self.assertIn(f"sha={SHA}",args)

    def test_uncertain_create_is_not_retried_and_is_read_confirmed(self):
        failure=subprocess.CompletedProcess(["gh"],1,"","transport failed")
        with mock.patch.object(tag_state,"resolve_tag_commit",side_effect=[None,SHA]), mock.patch.object(tag_state,"_run_gh",return_value=failure) as run, mock.patch.object(tag_state.time,"sleep"):
            self.assertEqual(tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"token"),"created-after-client-error")
        self.assertEqual(run.call_count,1)

    def test_timeout_exception_is_not_retried_and_is_read_confirmed(self):
        with mock.patch.object(tag_state,"resolve_tag_commit",side_effect=[None,SHA]), mock.patch.object(tag_state,"_run_gh",side_effect=tag_state.TagStateError("timeout")) as run, mock.patch.object(tag_state.time,"sleep"):
            self.assertEqual(tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"token"),"created-after-client-error")
        self.assertEqual(run.call_count,1)

    def test_uncertain_create_confirming_wrong_tag_fails(self):
        failure=subprocess.CompletedProcess(["gh"],1,"","transport failed")
        with mock.patch.object(tag_state,"resolve_tag_commit",side_effect=[None,OTHER_SHA]), mock.patch.object(tag_state,"_run_gh",return_value=failure), mock.patch.object(tag_state.time,"sleep"), self.assertRaises(tag_state.TagStateError): tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"token")

    def test_inputs_are_strict(self):
        with self.assertRaises(ValueError): tag_state.ensure_exact_tag("bad repo",TAG,SHA,"token")
        with self.assertRaises(ValueError): tag_state.ensure_exact_tag("owner/repo","v1",SHA,"token")
        with self.assertRaises(ValueError): tag_state.ensure_exact_tag("owner/repo",TAG,"short","token")
        with self.assertRaises(tag_state.TagStateError): tag_state.ensure_exact_tag("owner/repo",TAG,SHA,"")

    def test_helper_has_no_python_url_request_sink(self):
        text=MODULE_PATH.read_text(encoding="utf-8"); self.assertNotIn("urllib.request",text); self.assertNotIn("https://api.github.com",text)

if __name__ == "__main__": unittest.main()
