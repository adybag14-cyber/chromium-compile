import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "github_immutable_release.py"
SPEC = importlib.util.spec_from_file_location("github_immutable_release", PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


class ImmutableReleaseTests(unittest.TestCase):
    def _asset(self, root: pathlib.Path, name="asset.zip", data=b"release"):
        path = root / name
        path.write_bytes(data)
        return path

    def _remote(self, name, digest):
        return {
            "isDraft": True,
            "isImmutable": False,
            "assets": [
                {"name": name, "digest": digest, "state": "uploaded", "size": 7}
            ],
        }

    def test_identical_remote_asset_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._asset(pathlib.Path(temp))
            local = release._local_assets([path])
            digest = local[path.name][1]
            self.assertEqual(
                release._validate_asset_state(
                    self._remote(path.name, digest), local, allow_missing=False
                ),
                [],
            )

    def test_different_or_unexpected_remote_assets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._asset(pathlib.Path(temp))
            local = release._local_assets([path])
            with self.assertRaises(release.ImmutableReleaseError):
                release._validate_asset_state(
                    self._remote(path.name, "sha256:" + "0" * 64),
                    local,
                    allow_missing=False,
                )
            payload = self._remote("unexpected.zip", "sha256:" + "0" * 64)
            with self.assertRaises(release.ImmutableReleaseError):
                release._validate_asset_state(payload, local, allow_missing=True)

    def test_missing_draft_asset_is_uploadable_but_published_missing_is_not(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._asset(pathlib.Path(temp))
            local = release._local_assets([path])
            payload = {"isDraft": True, "isImmutable": False, "assets": []}
            self.assertEqual(
                release._validate_asset_state(payload, local, allow_missing=True), [path]
            )
            with self.assertRaises(release.ImmutableReleaseError):
                release._validate_asset_state(payload, local, allow_missing=False)

    def test_local_assets_must_be_nonempty_regular_and_unique(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            empty = self._asset(root, data=b"")
            with self.assertRaises(release.ImmutableReleaseError):
                release._local_assets([empty])
            first = self._asset(root, "asset.zip", b"a")
            with self.assertRaises(release.ImmutableReleaseError):
                release._local_assets([first, first])


if __name__ == "__main__":
    unittest.main()
