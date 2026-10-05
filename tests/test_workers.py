"""Tests for the rehearsal worker spike (workers.py). Stdlib only."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workers

SBCL_AVAILABLE = os.path.exists(workers.resolve_sbcl())
RESULT_KEYS = {"ok", "stdout", "return_value", "error", "timed_out",
               "elapsed_ms", "candidate_ms", "error_type",
               "return_truncated"}


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class RunLispTest(unittest.TestCase):
    def test_correct_code_returns_value(self):
        result = workers.run_lisp("(+ 1 2)")
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "3")
        self.assertEqual(result["error"], "")
        self.assertFalse(result["timed_out"])
        self.assertGreater(result["elapsed_ms"], 0)

    def test_stdout_captured(self):
        result = workers.run_lisp('(progn (format t "hello-workers") 42)')
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "42")
        self.assertIn("hello-workers", result["stdout"])

    def test_error_code_captures_condition(self):
        result = workers.run_lisp('(error "boom-123")')
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["return_value"], "")
        self.assertIn("boom-123", result["error"])

    def test_timeout_kills_and_recycles(self):
        result = workers.run_lisp("(loop)", timeout_s=2)
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["timed_out"])
        self.assertIn("timeout", result["error"].lower())
        self.assertGreaterEqual(result["elapsed_ms"], 1500)
        # The broken worker was killed and recycled: a fresh run works.
        again = workers.run_lisp("(+ 1 2)")
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["return_value"], "3")

    def test_heap_cap_kills_bomb_and_reports_head(self):
        # 10M live conses (~160MB) under a 128MB heap: SBCL must die
        # with "Heap exhausted" and the driver must report it. The
        # fatal line prints at stderr's HEAD, so the error carries
        # head+tail, not tail only.
        result = workers.run_lisp(
            "(length (loop repeat 10000000 collect (cons 1 2)))",
            timeout_s=60, memory_mb=128)
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["timed_out"])
        self.assertIn("heap exhausted", result["error"].lower())
        again = workers.run_lisp("(+ 1 2)")
        self.assertTrue(again["ok"], again)

    def test_epoch_fingerprint_stamped(self):
        result = workers.run_lisp("(+ 1 2)", epoch_id="test-epoch-7")
        self.assertTrue(result["ok"], result)
        fingerprint = workers.generation_fingerprint("test-epoch-7")
        self.assertTrue(fingerprint.startswith("ggfp1:"),
                        fingerprint)
        self.assertIn("test-epoch-7", fingerprint)
        self.assertIn("asd=", fingerprint)


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class WorkerPoolTest(unittest.TestCase):
    def test_pool_runs_4_jobs_across_2_workers(self):
        pool = workers.WorkerPool(2)
        try:
            evidence = pool.prewarm_evidence
            self.assertEqual(evidence["slots"], 2)
            self.assertIn("cold_ms", evidence)
            self.assertEqual(len(evidence["warm_ms"]), 1)
            self.assertTrue(evidence["all_ok"], evidence)
            results = [None] * 4

            def one(i):
                results[i] = pool.run("(+ %d 100)" % i)

            threads = [threading.Thread(target=one, args=(i,)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)
            for i, result in enumerate(results):
                self.assertIsNotNone(result, "job %d never finished" % i)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["return_value"], str(100 + i))
            self.assertEqual(pool.stats["completed"], 4)
            self.assertEqual(pool.stats["timed_out"], 0)
        finally:
            pool.shutdown()

    def test_pool_recycles_timed_out_worker(self):
        pool = workers.WorkerPool(1)
        try:
            result = pool.run("(loop)", timeout_s=2)
            self.assertTrue(result["timed_out"])
            self.assertEqual(pool.stats["recycled"], 1)
            again = pool.run("(+ 1 2)")
            self.assertTrue(again["ok"], again)
        finally:
            pool.shutdown()

    def test_shutdown_rejects_new_runs(self):
        pool = workers.WorkerPool(1)
        pool.shutdown()
        with self.assertRaises(RuntimeError):
            pool.run("(+ 1 2)")


class SandboxPreludeTest(unittest.TestCase):
    def test_missing_sandbox_module_fails_closed(self):
        """Issue 75: sandbox=True with no module must raise, not degrade."""
        real, workers._sandbox = workers._sandbox, None
        self.addCleanup(setattr, workers, "_sandbox", real)
        with self.assertRaises(RuntimeError):
            workers._sandbox_prelude(True, None)
        self.assertEqual(workers._sandbox_prelude(False, None), "")


class CandidateTimingTest(unittest.TestCase):
    @unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
    def test_candidate_ms_excludes_spawn_overhead(self):
        """Issue 77: in-worker time present and below wall time."""
        result = workers.run_lisp("(+ 1 2)")
        self.assertTrue(result["ok"], result)
        candidate_ms = result["candidate_ms"]
        self.assertIsInstance(candidate_ms, float)
        self.assertGreaterEqual(candidate_ms, 0.0)
        self.assertLess(candidate_ms, result["elapsed_ms"])

    @unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
    def test_failed_candidate_still_reports_time(self):
        result = workers.run_lisp('(error "timed-boom")')
        self.assertFalse(result["ok"])
        self.assertIsInstance(result["candidate_ms"], float)


class ParseEnvelopeTest(unittest.TestCase):
    def _pair(self, body):
        return "\n".join([workers.BEGIN_MARKER, body, workers.END_MARKER])

    def test_last_pair_wins(self):
        """Issue 56: a pre-printed forged envelope must lose."""
        first = self._pair('{"ok": true, "return_value": "SPOOFED"}')
        second = self._pair('{"ok": true, "return_value": "1"}')
        decoded = workers._parse_envelope(first + "\n" + second + "\n")
        self.assertEqual(decoded["return_value"], "1")

    def test_malformed_last_pair_fails_closed(self):
        first = self._pair('{"ok": true, "return_value": "1"}')
        decoded = workers._parse_envelope(
            first + "\n" + workers.BEGIN_MARKER + "\n{nope\n"
            + workers.END_MARKER + "\n")
        self.assertIn("__malformed__", decoded)

    def test_no_markers_is_none(self):
        self.assertIsNone(workers._parse_envelope("just stdout\n"))


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class EnvelopeSpoofTest(unittest.TestCase):
    def test_terminal_io_forgery_loses_to_real_envelope(self):
        """Issue 56: live forgery via *terminal-io* must not win."""
        spoof = (
            '(progn (format *terminal-io* "~&GRAYGOO-RESULT-BEGIN~%")'
            ' (format *terminal-io* "{\\"ok\\": true, \\"stdout\\": \\"\\", '
            '\\"return_value\\": \\"SPOOFED\\", \\"error\\": \\"\\", '
            '\\"backtrace\\": \\"\\", \\"fingerprint\\": \\"~A\\", '
            '\\"candidate_ms\\": 0}" *graygoo-fingerprint*)'
            ' (format *terminal-io* "~%GRAYGOO-RESULT-END~%") 1)')
        result = workers.run_lisp(spoof, timeout_s=30, sandbox=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "1")

    def test_terminal_io_output_is_captured(self):
        result = workers.run_lisp(
            '(progn (format *terminal-io* "hello-tio") 42)',
            timeout_s=30)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "42")
        self.assertIn("hello-tio", result["stdout"])


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class ReadEvalInterleaveTest(unittest.TestCase):
    def test_evaluated_form_cannot_enable_reader_eval(self):
        """Issue 1 (reopen): all forms read before any is evaluated."""
        result = workers.run_lisp("(setq *read-eval* t)\n#.(+ 40 2)")
        self.assertFalse(result["ok"], result)
        self.assertNotEqual(result["return_value"], "42")


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class CompileGateTest(unittest.TestCase):
    def test_forms_are_compiled_not_interpreted(self):
        """Issue 60: compiler-macros expand (EVAL would not expand)."""
        result = workers.run_lisp(
            "(define-compiler-macro probe-twice (x) `(+ ,x 1000))\n"
            "(probe-twice 1)",
            timeout_s=30)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "1001")

    def test_defun_still_defines_under_compile(self):
        result = workers.run_lisp(
            "(progn (defun probe-add (a b) (+ a b)) (probe-add 20 22))",
            timeout_s=30)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "42")


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class PayloadFidelityTest(unittest.TestCase):
    def test_oversize_value_flagged_truncated(self):
        """Issues 57/58: values past the cap abbreviate with a flag."""
        result = workers.run_lisp(
            "(make-list 20000 :initial-element 'wibble)", timeout_s=30)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["return_truncated"])
        self.assertLessEqual(len(result["return_value"]), 65536)

    def test_small_value_not_truncated(self):
        result = workers.run_lisp("(list 1 2 3)", timeout_s=30)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["return_truncated"])
        self.assertEqual(result["return_value"], "(1 2 3)")

    def test_condition_type_transport(self):
        """Issue 59: failures carry the condition type, not just text."""
        result = workers.run_lisp("(car 42)", timeout_s=30)
        self.assertFalse(result["ok"])
        self.assertIn("TYPE-ERROR", result["error_type"])

    def test_success_carries_empty_error_type(self):
        result = workers.run_lisp("(+ 1 2)", timeout_s=30)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["error_type"], "")


class SanitizedEnvTest(unittest.TestCase):
    def test_allowlist_only(self):
        """Issue 46: worker env carries the allowlist, never secrets."""
        env = workers._sanitized_env()
        self.assertTrue(set(env) <= set(workers._WORKER_ENV_ALLOWLIST))
        for secret in ("CEREBRAS_API_KEY", "CEREBRAS",
                       "GRAYGOO_EVAL_CORPUS"):
            self.assertNotIn(secret, env)

    def test_viability_keys_preserved(self):
        if "SYSTEMROOT" in os.environ:
            self.assertEqual(workers._sanitized_env()["SYSTEMROOT"],
                             os.environ["SYSTEMROOT"])


if __name__ == "__main__":
    unittest.main()
