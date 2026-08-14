import importlib.util
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "github_release_tag.py"
SPEC = importlib.util.spec_from_file_location("github_release_tag", MODULE_PATH)
assert SPEC and SPEC.loader
tag_state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tag_state
SPEC.loader.exec_module(tag_state)

SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG = "chromium-151.0.7922.108-linux-i686"


class FakeResponse:
    def __init__(self, status=201):
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class FakeJsonResponse(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *_args): self.close(); return False


class ReleaseTagTests(unittest.TestCase):
    def test_resolve_lightweight_tag_uses_exact_tag_ref(self):
        response = FakeJsonResponse({"object": {"type": "commit", "sha": SHA}})
        with mock.patch.object(tag_state, "_request", return_value=response) as request:
            self.assertEqual(tag_state.resolve_tag_commit("owner/repo", TAG, "token"), SHA)
        self.assertIn("/git/ref/tags/", request.call_args.args[0])
        self.assertNotIn("/commits/", request.call_args.args[0])

    def test_resolve_annotated_tag_dereferences_to_commit(self):
        tag_object_sha = "c" * 40
        responses = [
            FakeJsonResponse({"object": {"type": "tag", "sha": tag_object_sha}}),
            FakeJsonResponse({"object": {"type": "commit", "sha": SHA}}),
        ]
        with mock.patch.object(tag_state, "_request", side_effect=responses) as request:
            self.assertEqual(tag_state.resolve_tag_commit("owner/repo", TAG, "token"), SHA)
        self.assertEqual(request.call_count, 2)
        self.assertIn(f"/git/tags/{tag_object_sha}", request.call_args_list[1].args[0])

    def test_existing_exact_tag_is_accepted_without_write(self):
        with mock.patch.object(tag_state, "resolve_tag_commit", return_value=SHA), \
             mock.patch.object(tag_state, "_request") as request:
            self.assertEqual(
                tag_state.ensure_exact_tag("owner/repo", TAG, SHA, "token"),
                "already-exact",
            )
        request.assert_not_called()

    def test_existing_wrong_tag_fails_before_write(self):
        with mock.patch.object(tag_state, "resolve_tag_commit", return_value=OTHER_SHA), \
             mock.patch.object(tag_state, "_request") as request:
            with self.assertRaises(tag_state.TagStateError):
                tag_state.ensure_exact_tag("owner/repo", TAG, SHA, "token")
        request.assert_not_called()

    def test_missing_tag_is_created_once_then_confirmed(self):
        with mock.patch.object(tag_state, "resolve_tag_commit", side_effect=[None, SHA]), \
             mock.patch.object(tag_state, "_request", return_value=FakeResponse()) as request, \
             mock.patch.object(tag_state.time, "sleep"):
            self.assertEqual(
                tag_state.ensure_exact_tag("owner/repo", TAG, SHA, "token"),
                "created",
            )
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertEqual(request.call_args.kwargs["body"], {"ref": f"refs/tags/{TAG}", "sha": SHA})

    def test_uncertain_create_is_not_retried_and_is_read_confirmed(self):
        with mock.patch.object(tag_state, "resolve_tag_commit", side_effect=[None, SHA]), \
             mock.patch.object(tag_state, "_request", side_effect=urllib.error.URLError("timeout")) as request, \
             mock.patch.object(tag_state.time, "sleep"):
            self.assertEqual(
                tag_state.ensure_exact_tag("owner/repo", TAG, SHA, "token"),
                "created-after-client-error",
            )
        self.assertEqual(request.call_count, 1)

    def test_uncertain_create_confirming_wrong_tag_fails(self):
        with mock.patch.object(tag_state, "resolve_tag_commit", side_effect=[None, OTHER_SHA]), \
             mock.patch.object(tag_state, "_request", side_effect=urllib.error.URLError("timeout")), \
             mock.patch.object(tag_state.time, "sleep"):
            with self.assertRaises(tag_state.TagStateError):
                tag_state.ensure_exact_tag("owner/repo", TAG, SHA, "token")

    def test_inputs_are_strict(self):
        with self.assertRaises(ValueError):
            tag_state.ensure_exact_tag("bad repo", TAG, SHA, "token")
        with self.assertRaises(ValueError):
            tag_state.ensure_exact_tag("owner/repo", "v1", SHA, "token")
        with self.assertRaises(ValueError):
            tag_state.ensure_exact_tag("owner/repo", TAG, "short", "token")
        with self.assertRaises(tag_state.TagStateError):
            tag_state.ensure_exact_tag("owner/repo", TAG, SHA, "")


if __name__ == "__main__":
    unittest.main()
