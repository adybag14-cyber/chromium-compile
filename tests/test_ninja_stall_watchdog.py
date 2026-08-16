import importlib.util
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "ninja_stall_watchdog.py"
SPEC = importlib.util.spec_from_file_location("ninja_stall_watchdog", PATH)
assert SPEC and SPEC.loader
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)


class NinjaStallWatchdogTests(unittest.TestCase):
    def test_cli_stall_policy_matches_workflow_bounds(self):
        self.assertEqual(
            watchdog.validate_compiler_stall_seconds(90 * 60),
            90 * 60,
        )
        for invalid in (0, 29 * 60, 181 * 60, True):
            with self.subTest(invalid=invalid), self.assertRaises(watchdog.WatchdogError):
                watchdog.validate_compiler_stall_seconds(invalid)

    def test_compiler_command_is_fixed_except_for_bounded_timeout(self):
        self.assertEqual(
            watchdog.compiler_command(123),
            [
                "timeout", "-k", "120s", "123s", "autoninja", "-C",
                "out/Release_x86", "-j3", "chrome",
                "chrome/installer/linux:installer_deps",
            ],
        )
        for invalid in (0, -1, watchdog.MAX_COMPILER_TIMEOUT_SECONDS + 1, True):
            with self.subTest(invalid=invalid), self.assertRaises(watchdog.WatchdogError):
                watchdog.compiler_command(invalid)

    def test_progress_fingerprint_rejects_non_regular_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            self.assertIsNone(watchdog.progress_fingerprint(root / "missing"))
            regular = root / ".ninja_log"
            regular.write_text("# ninja log v5\n", encoding="utf-8")
            self.assertIsNotNone(watchdog.progress_fingerprint(regular))
            with self.assertRaises(watchdog.WatchdogError):
                watchdog.progress_fingerprint(root)

    def test_error_marker_helpers_are_fixed_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            marker = root / "watchdog.error"
            watchdog.write_error_marker(marker, "failure detail")
            self.assertEqual(marker.read_text(encoding="utf-8"), "failure detail\n")
            watchdog.clear_marker(marker, "test")
            self.assertFalse(marker.exists())

    def test_child_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            result = watchdog.run_with_watchdog(
                [sys.executable, "-c", "raise SystemExit(7)"],
                progress_log=root / ".ninja_log",
                stall_seconds=5,
                poll_seconds=1,
                kill_grace_seconds=1,
                stall_marker=root / "stall.marker",
            )
            self.assertEqual(result, 7)

    def test_negative_child_signal_is_normalized_to_shell_status(self):
        class FakeProc:
            def poll(self):
                return -15

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            watchdog.subprocess, "Popen", return_value=FakeProc()
        ):
            root = pathlib.Path(temp)
            result = watchdog.run_with_watchdog(
                ["fake-child"],
                progress_log=root / ".ninja_log",
                stall_seconds=5,
                poll_seconds=1,
                kill_grace_seconds=1,
                stall_marker=root / "stall.marker",
            )
            self.assertEqual(result, 143)

    def test_natural_reserved_exit_code_does_not_create_stall_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            marker = root / "stall.marker"
            marker.write_text("stale\n", encoding="utf-8")
            result = watchdog.run_with_watchdog(
                [sys.executable, "-c", f"raise SystemExit({watchdog.STALL_EXIT_CODE})"],
                progress_log=root / ".ninja_log",
                stall_seconds=5,
                poll_seconds=1,
                kill_grace_seconds=1,
                stall_marker=marker,
            )
            self.assertEqual(result, watchdog.STALL_EXIT_CODE)
            self.assertFalse(marker.exists())

    def test_real_progress_prevents_false_stall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            progress = root / ".ninja_log"
            code = (
                "import pathlib,time; p=pathlib.Path(r'" + str(progress) + "'); "
                "time.sleep(.2); p.write_text('# ninja log v5\\n1\\t2\\t0\\tobj/a.o\\tx\\n'); time.sleep(.2)"
            )
            result = watchdog.run_with_watchdog(
                [sys.executable, "-c", code],
                progress_log=progress,
                stall_seconds=2,
                poll_seconds=1,
                kill_grace_seconds=1,
                stall_marker=root / "stall.marker",
            )
            self.assertEqual(result, 0)
            self.assertFalse((root / "stall.marker").exists())

    def test_stalled_child_is_terminated_and_marked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            started = time.monotonic()
            result = watchdog.run_with_watchdog(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                progress_log=root / ".ninja_log",
                stall_seconds=1,
                poll_seconds=1,
                kill_grace_seconds=1,
                stall_marker=root / "stall.marker",
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result, watchdog.STALL_EXIT_CODE)
            self.assertTrue((root / "stall.marker").is_file())
            self.assertLess(elapsed, 6)

    def test_invalid_policy_fails_before_child_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with self.assertRaises(watchdog.WatchdogError):
                watchdog.run_with_watchdog(
                    [sys.executable, "-c", "raise SystemExit(0)"],
                    progress_log=root / ".ninja_log",
                    stall_seconds=0,
                    poll_seconds=1,
                    kill_grace_seconds=1,
                    stall_marker=root / "stall.marker",
                )


if __name__ == "__main__":
    unittest.main()
