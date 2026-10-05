"""QA seam tests: benchmarks/runner.py malformed-input handling.

Proves QA findings QA-01 (crash on non-string recorded output),
QA-02 (vacuous 0-task pass exits 0), QA-12 (validate_tasks ignores
check value types). Guards document the adjacent behavior that already
holds (json-mode safety, summary arithmetic).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

import runner

TASKS_DIR = str(Path(__file__).resolve().parent.parent / "benchmarks"
                / "family-a")
PASS_FIXTURE = str(Path(__file__).resolve().parent.parent / "benchmarks"
                   / "family-a" / "recorded" / "stub_all_pass.json")


def _task(**over):
    task = {"id": "T1", "family": "A", "split": "exposure", "prompt": "p",
            "notes": "n", "checks": [{"input": "in", "expected": "out",
                                      "compare": "exact"}]}
    task.update(over)
    return task


def _recorded(payload):
    fd, path = tempfile.mkstemp(suffix=".json", prefix="qa-runner-")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class RunnerMalformedInputTest(unittest.TestCase):
    def test_non_string_recorded_output_fails_check_without_crash(self):
        """QA-01: int recorded output must fail the check, not raise.

        Currently run() propagates AttributeError ('int' has no
        'strip') out of compare(), aborting the whole benchmark run
        and discarding every other task's results.
        """
        path = _recorded({"T1": [12345]})
        self.addCleanup(os.unlink, path)
        with redirect_stdout(io.StringIO()):
            summary = runner.run([_task()], runner.StubAdapter(path))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["tasks"][0]["passed"], False)

    def test_zero_selected_tasks_is_not_success(self):
        """QA-02: selecting zero tasks must not exit 0.

        Currently `--only NO-SUCH-TASK` prints "0/0 tasks passed" and
        main() returns 0, so a typo'd filter reports success.
        """
        with redirect_stdout(io.StringIO()):
            code = runner.main(["--tasks", TASKS_DIR, "--adapter", "stub",
                                "--recorded", PASS_FIXTURE,
                                "--only", "NO-SUCH-TASK-ID"])
        self.assertNotEqual(code, 0)

    def test_validate_tasks_rejects_non_string_check_values(self):
        """QA-12: check input/expected must be strings.

        Currently validate_tasks() reports no problem for numeric
        check values, letting fixtures that crash run() (see QA-01)
        through the schema gate.
        """
        bad = [_task(checks=[{"input": 1, "expected": 2,
                              "compare": "exact"}])]
        self.assertTrue(runner.validate_tasks(bad))

    def test_json_mode_non_string_output_fails_without_crash(self):
        """Guard: json compare already coerces safely (contrast QA-01)."""
        task = _task(checks=[{"input": "in", "expected": '{"a": 1}',
                              "compare": "json"}])
        path = _recorded({"T1": [12345]})
        self.addCleanup(os.unlink, path)
        with redirect_stdout(io.StringIO()):
            summary = runner.run([task], runner.StubAdapter(path))
        self.assertEqual(summary["failed"], 1)

    def test_summarize_counts_are_consistent(self):
        """Guard: summary arithmetic on a mixed pass/fail run."""
        good = _task(id="GOOD", checks=[{"input": "i", "expected": "o",
                                         "compare": "exact"}])
        bad = _task(id="BAD", checks=[{"input": "i", "expected": "o",
                                       "compare": "exact"}])
        path = _recorded({"GOOD": ["o"], "BAD": ["wrong"]})
        self.addCleanup(os.unlink, path)
        with redirect_stdout(io.StringIO()):
            summary = runner.run([good, bad], runner.StubAdapter(path))
        self.assertEqual((summary["total"], summary["passed"],
                          summary["failed"]), (2, 1, 1))

    def test_stub_missing_entry_fails_check_without_crash(self):
        """Guard: unrecorded task id yields "" and fails honestly."""
        path = _recorded({})
        self.addCleanup(os.unlink, path)
        with redirect_stdout(io.StringIO()):
            summary = runner.run([_task()], runner.StubAdapter(path))
        self.assertEqual(summary["failed"], 1)


if __name__ == "__main__":
    unittest.main()
