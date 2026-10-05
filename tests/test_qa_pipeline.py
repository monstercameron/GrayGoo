"""QA seam tests: pipeline.py stage handling + pipeline<->worker drift.

Proves QA-03 (unknown stage keys silently ignored), QA-04
(expect-vs-PRIN1-string drift against the real worker), QA-10 (empty
check list vacuous pass). Guards lock the fail-closed paths and prove
the live pipeline<->worker envelope seam end to end (one SBCL spawn).
"""

import unittest

import pipeline

CANDIDATE = "(candidate (:target foo) (:parent 0) (:definition (foo 1)))"


def _ok_worker(code):
    return {"ok": True, "stdout": "", "return_value": "3", "error": "",
            "timed_out": False, "elapsed_ms": 1.0}


def _r0(parsed):
    return {"level": "R0"}


class PipelineStageSeamTest(unittest.TestCase):
    def test_unknown_stage_key_is_not_silently_ignored(self):
        """QA-03: typo'd stage name must fail, not pass vacuously.

        Currently tests={"directt": [...]} yields ok=True with reason
        "candidate parsed; no checks requested" — the caller believes
        checks ran. Suggested fix: fail (or record) unknown stage keys.
        """
        result = pipeline.run_candidate(
            CANDIDATE, tests={"directt": ["(+ 1 2)"]},
            worker_fn=_ok_worker, risk_fn=_r0)
        self.assertFalse(result["ok"])

    def test_empty_check_list_does_not_pass(self):
        """QA-10: a stage with zero items must not report success.

        Currently tests={"direct": []} yields ok=True, "all requested
        stages passed", with evidence total=0. Suggested fix: fail the
        stage (or skip it explicitly) when it runs zero items.
        """
        result = pipeline.run_candidate(
            CANDIDATE, tests={"direct": []},
            worker_fn=_ok_worker, risk_fn=_r0)
        self.assertFalse(result["ok"])

    def test_numeric_expect_matches_worker_string_result(self):
        """QA-04: pipeline<->worker type drift on return_value.

        The real SBCL worker always reports return_value as a PRIN1
        string ("3"), but _eval_code_item compares with raw ==, so a
        numeric expect (3) can never pass against the real worker.
        Suggested fix: normalize the comparison (e.g. compare printed
        representations) or document string-only expects.
        """
        passed, _ = pipeline._eval_code_item(
            {"code": "(+ 1 2)", "expect": 3}, _ok_worker)
        self.assertTrue(passed)

    def test_r6_blocks_before_any_worker_use(self):
        """Guard: R6 risk short-circuits without touching the worker."""
        calls = []

        def spy(code):
            calls.append(code)
            return _ok_worker(code)

        result = pipeline.run_candidate(
            CANDIDATE, tests={"direct": ["(+ 1 2)"]},
            worker_fn=spy, risk_fn=lambda p: {"level": "R6"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "risk")
        self.assertEqual(calls, [])

    def test_risk_fn_raising_fails_closed(self):
        """Guard: classifier exception fails the run, never passes."""

        def boom(parsed):
            raise RuntimeError("classifier down")

        result = pipeline.run_candidate(
            CANDIDATE, tests={"direct": ["(+ 1 2)"]},
            worker_fn=_ok_worker, risk_fn=boom)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "risk")

    def test_worker_raising_becomes_evidence_not_exception(self):
        """Guard: worker_fn raising is recorded per-item, not raised."""

        def boom(code):
            raise RuntimeError("worker down")

        result = pipeline.run_candidate(
            CANDIDATE, tests={"direct": ["(+ 1 2)"]},
            worker_fn=boom, risk_fn=_r0)
        self.assertFalse(result["ok"])
        self.assertIn("worker_fn raised",
                      result["evidence"]["direct"]["items"][0]["error"])

    def test_live_worker_envelope_matches_pipeline_contract(self):
        """Guard: real run_lisp envelope feeds _run_worker (1 SBCL run)."""
        import workers

        envelope = workers.run_lisp("(+ 1 2)", timeout_s=30)
        for key in ("ok", "stdout", "return_value", "error",
                    "timed_out", "elapsed_ms"):
            self.assertIn(key, envelope)
        passed, record = pipeline._run_worker(workers.run_lisp, "(+ 1 2)")
        self.assertTrue(passed, record)
        self.assertEqual(record["return_value"], "3")


if __name__ == "__main__":
    unittest.main()
