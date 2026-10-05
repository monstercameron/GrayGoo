"""Tests for the rehearsal worker spike (workers.py). Stdlib only."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workers

SBCL_AVAILABLE = os.path.exists(workers.resolve_sbcl())
RESULT_KEYS = {"ok", "stdout", "return_value", "error", "timed_out", "elapsed_ms"}


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


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class ReadEvalInterleaveTest(unittest.TestCase):
    def test_evaluated_form_cannot_enable_reader_eval(self):
        """Issue 1 (reopen): all forms read before any is evaluated."""
        result = workers.run_lisp("(setq *read-eval* t)\n#.(+ 40 2)")
        self.assertFalse(result["ok"], result)
        self.assertNotEqual(result["return_value"], "42")


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
