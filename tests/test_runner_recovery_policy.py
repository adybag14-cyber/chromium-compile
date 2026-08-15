import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "scripts" / "chromium_runner_recovery.py"
SPEC = importlib.util.spec_from_file_location("chromium_runner_recovery", PATH)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


class RunnerRecoveryPolicyTests(unittest.TestCase):
    def test_valid_recovery_computes_next_attempt(self):
        self.assertEqual(recovery.next_retry("151.0.7922.108", "3", "0", "2"), 1)
        self.assertEqual(recovery.next_retry("151.0.7922.108", "50", "9", "10"), 10)

    def test_exhausted_budget_fails_closed(self):
        for retry_count, maximum in (("0", "0"), ("2", "2"), ("10", "10")):
            with self.subTest(retry_count=retry_count, maximum=maximum):
                with self.assertRaises(recovery.RecoveryPolicyError):
                    recovery.next_retry("151.0.7922.108", "3", retry_count, maximum)

    def test_invalid_or_unbounded_values_fail_before_arithmetic(self):
        bad_cases = [
            ("bad", "3", "0", "2"),
            ("151.0.7922.108", "0", "0", "2"),
            ("151.0.7922.108", "51", "0", "2"),
            ("151.0.7922.108", "3", "999999999999999999", "2"),
            ("151.0.7922.108", "3", "0", "11"),
            ("151.0.7922.108", "3", "-1", "2"),
        ]
        for args in bad_cases:
            with self.subTest(args=args):
                with self.assertRaises(recovery.RecoveryPolicyError):
                    recovery.next_retry(*args)


if __name__ == "__main__":
    unittest.main()
