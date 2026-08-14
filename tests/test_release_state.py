import importlib.util
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "github_release_state.py"
SPEC = importlib.util.spec_from_file_location("github_release_state", MODULE_PATH)
assert SPEC and SPEC.loader
release_state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_state
SPEC.loader.exec_module(release_state)


class FakeResponse:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class ReleaseStateTests(unittest.TestCase):
    def test_existing_release_is_true(self):
        with mock.patch.object(release_state.urllib.request, "urlopen", return_value=FakeResponse()):
            self.assertTrue(release_state.release_exists("owner/repo", "tag", "token"))

    def test_real_404_is_missing(self):
        error = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, None)
        with mock.patch.object(release_state.urllib.request, "urlopen", side_effect=error):
            self.assertFalse(release_state.release_exists("owner/repo", "tag", "token"))

    def test_non_404_http_failure_fails_closed(self):
        error = urllib.error.HTTPError("https://api.github.com/x", 503, "Unavailable", {}, None)
        with mock.patch.object(release_state.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(release_state.ReleaseStateError):
                release_state.release_exists("owner/repo", "tag", "token")

    def test_network_failure_fails_closed(self):
        with mock.patch.object(
            release_state.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(release_state.ReleaseStateError):
                release_state.release_exists("owner/repo", "tag", "token")

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            release_state.release_exists("bad repo", "tag", "token")
        with self.assertRaises(release_state.ReleaseStateError):
            release_state.release_exists("owner/repo", "tag", "")


if __name__ == "__main__":
    unittest.main()
