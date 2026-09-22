import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/validate-port-infrastructure.yml"


@unittest.skipIf(os.name == "nt", "The hosted probe uses a Linux Bash shell")
class UpstreamSourceReadinessTests(unittest.TestCase):
    def run_probe(self, availability_status):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        start = workflow.index("      - name: Resolve latest source-declared tool pins\n")
        end = workflow.index("  validate_checkpoint_recovery:\n", start)
        step = workflow[start:end]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            common = root / ".github/scripts/chromium_i686_common.sh"
            common.parent.mkdir(parents=True)
            common.write_text(textwrap.dedent("""\
                resolve_latest_version() { echo 153.0.8010.52; }
                python3() {
                  if [[ "$*" == *--availability-only* ]]; then
                    echo "availability" >> "$PROBE_CALLS"
                    if [ "$AVAILABILITY_STATUS" = 0 ]; then
                      echo available
                    elif [ "$AVAILABILITY_STATUS" = 3 ]; then
                      echo pending
                    else
                      echo 'upstream metadata request failed' >&2
                    fi
                    return "$AVAILABILITY_STATUS"
                  fi
                  echo "metadata" >> "$PROBE_CALLS"
                  return 42
                }
                """), encoding="utf-8")
            output = root / "github-output"
            calls = root / "calls"
            env = dict(os.environ, AVAILABILITY_STATUS=str(availability_status),
                       PROBE_CALLS=str(calls), GITHUB_OUTPUT=str(output),
                       GITHUB_STEP_SUMMARY=str(root / "summary"), RUNNER_TEMP=str(root))
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                    cwd=root, env=env, capture_output=True, text=True,
                                    timeout=10, check=False)
            return (result, output.read_text() if output.exists() else "",
                    calls.read_text().splitlines() if calls.exists() else [])

    def test_unpublished_source_defers_without_probing_contract_or_marking_validated(self):
        result, output, calls = self.run_probe(3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["availability"])
        self.assertIn("validated=false", output)
        self.assertNotIn("validated=true", output)
        self.assertIn("not published yet", result.stdout)

    def test_availability_errors_remain_failures(self):
        for status in (1, 22):
            with self.subTest(status=status):
                result, output, calls = self.run_probe(status)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(calls, ["availability"])
                self.assertNotIn("validated=true", output)

    def test_available_source_continues_and_contract_errors_still_fail(self):
        result, output, calls = self.run_probe(0)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(calls, ["availability", "metadata"])
        self.assertNotIn("validated=true", output)


if __name__ == "__main__":
    unittest.main()
