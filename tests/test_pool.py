"""Tests for the persistent SBCL worker pool (pool.py). Stdlib only.

Live tests spawn real SBCL workers with short lifetimes and are
skipped when SBCL is unavailable; protocol unit tests always run.
"""

import os
import sys
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ossandbox
import pool
import workers

SBCL_AVAILABLE = os.path.exists(workers.resolve_sbcl())
POOL_SOURCE = os.path.join(os.path.dirname(os.path.abspath(pool.__file__)), "pool.py")

WORKER_KEYS = {"ok", "stdout", "return_value", "error", "timed_out",
               "elapsed_ms"}


class ProtocolUnitTest(unittest.TestCase):
    def test_encode_text(self):
        self.assertEqual(pool._encode_text(""), "-")
        import base64
        self.assertEqual(pool._encode_text("(+ 1 2)"),
                         base64.b64encode(b"(+ 1 2)").decode("ascii"))
        self.assertEqual(pool._encode_text("caf\u00e9"),
                         base64.b64encode("caf\u00e9".encode("utf-8"))
                         .decode("ascii"))
        with self.assertRaises(TypeError):
            pool._encode_text(42)

    def test_parse_result_line(self):
        decoded = pool._parse_result_line(
            'RESULT abc123 {"ok": true, "stdout": ""}', "abc123")
        self.assertTrue(decoded["ok"])
        with self.assertRaises(pool.WorkerPoisonedError):
            pool._parse_result_line(
                'RESULT other {"ok": true}', "abc123")
        with self.assertRaises(pool.WorkerPoisonedError):
            pool._parse_result_line('RESULT abc123 {nope', "abc123")
        with self.assertRaises(pool.WorkerPoisonedError):
            pool._parse_result_line("PONG abc123", "abc123")
        with self.assertRaises(pool.WorkerPoisonedError):
            pool._parse_result_line("RESULT", "abc123")

    def test_scrub_env(self):
        env = {"PATH": "x", "GRAYGOO_SBCL": "y",
               "CEREBRAS_API_KEY": "live-key",
               "CEREBRAS": "live-key-2"}
        cleaned = pool._scrub_env(env)
        self.assertEqual(cleaned, {"PATH": "x", "GRAYGOO_SBCL": "y"})
        # The input mapping is not mutated.
        self.assertIn("CEREBRAS_API_KEY", env)

    def test_build_script_shape(self):
        script = pool._build_script("(do-setup)", "gen-1", "wid-1")
        for marker in ("READY ", "PONG ", "RESET-OK", "RESULT ", "JOB ",
                       "graygoo-job", "*read-eval*", "gen-1", "wid-1",
                       "(do-setup)"):
            self.assertIn(marker, script)
        self.assertNotIn("@@", script)
        self.assertNotIn("~~", script)
        for secret in ("CEREBRAS_API_KEY", "CEREBRAS="):
            self.assertNotIn(secret, script)

    def test_result_keys_cover_worker_envelope(self):
        self.assertLessEqual(WORKER_KEYS, pool.RESULT_KEYS)

    def test_pool_size_validated_before_spawn(self):
        with self.assertRaises(ValueError):
            pool.Pool(0)

    def test_missing_sbcl_fails_fast_without_spawn(self):
        with self.assertRaises(pool.WorkerSpawnError):
            pool.PersistentWorker(executable="definitely-not-sbcl-xyz")


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class PersistentWorkerTest(unittest.TestCase):
    def test_run_round_trip(self):
        with pool.PersistentWorker(generation="test-gen") as worker:
            result = worker.run("(+ 1 2)")
            self.assertEqual(set(result), pool.RESULT_KEYS)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["return_value"], "3")
            self.assertEqual(result["error"], "")
            self.assertFalse(result["timed_out"])
            self.assertTrue(result["pooled"])
            self.assertTrue(result["generation_ok"])
            self.assertEqual(result["worker_id"], worker.worker_id)
            self.assertGreater(result["elapsed_ms"], 0)

    def test_state_persists_across_jobs(self):
        with pool.PersistentWorker(generation="test-gen") as worker:
            first = worker.run("(defparameter *pool-test-x* 41)")
            self.assertTrue(first["ok"], first)
            second = worker.run("(+ *pool-test-x* 1)")
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["return_value"], "42")
            self.assertEqual(first["worker_id"], second["worker_id"])
            self.assertEqual(worker.jobs_completed, 2)

    def test_stdout_error_unicode_prelude(self):
        with pool.PersistentWorker(generation="test-gen") as worker:
            out = worker.run('(progn (format t "hello-pool") 7)')
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["return_value"], "7")
            self.assertIn("hello-pool", out["stdout"])
            err = worker.run('(error "boom-456")')
            self.assertFalse(err["ok"])
            self.assertIn("boom-456", err["error"])
            uni = worker.run('(format nil "caf\u00e9")')
            self.assertTrue(uni["ok"], uni)
            self.assertIn("caf\u00e9", uni["return_value"])
            pre = worker.run("(+ 1 1)", prelude="(defparameter *pp* 9)")
            self.assertTrue(pre["ok"], pre)
            self.assertEqual(pre["return_value"], "2")
            self.assertEqual(worker.run("*pp*")["return_value"], "9")

    def test_ping_reset_health(self):
        with pool.PersistentWorker(generation="test-gen") as worker:
            self.assertTrue(worker.ping())
            self.assertTrue(worker.reset())
            again = worker.run("(+ 2 3)")
            self.assertTrue(again["ok"], again)
            self.assertEqual(again["return_value"], "5")
            health = worker.health()
            self.assertTrue(health["alive"])
            self.assertTrue(health["ping_ok"])
            self.assertGreaterEqual(health["jobs_completed"], 1)

    def test_generation_mismatch_flagged(self):
        worker = pool.PersistentWorker(generation="gen-pinned")
        try:
            worker.generation = "gen-tampered"
            result = worker.run("(+ 1 2)")
            self.assertFalse(result["generation_ok"])
            self.assertFalse(result["ok"])
            self.assertIn("generation mismatch", result["error"])
        finally:
            worker.close()


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class PoolTest(unittest.TestCase):
    def test_prewarm_and_reuse(self):
        with pool.Pool(size=1, generation="pool-gen-1") as fleet:
            self.assertTrue(fleet.prewarm_evidence["all_ok"],
                            fleet.prewarm_evidence)
            first = fleet.run("(+ 10 20)")
            second = fleet.run("(* 6 7)")
            self.assertTrue(first["ok"], first)
            self.assertTrue(second["ok"], second)
            self.assertEqual(first["return_value"], "30")
            self.assertEqual(second["return_value"], "42")
            # Same long-lived worker served both jobs.
            self.assertEqual(first["worker_id"], second["worker_id"])
            self.assertTrue(first["pooled"])
            self.assertEqual(fleet.stats["jobs"], 2)
            self.assertEqual(fleet.stats["poisoned"], 0)

    def test_recycle_after_max_jobs(self):
        with pool.Pool(size=1, generation="pool-gen-2",
                       max_jobs_per_worker=2,
                       oneshot_fallback=False) as fleet:
            ids = [fleet.run("(+ 1 %d)" % i)["worker_id"]
                   for i in range(5)]
            self.assertEqual(len(set(ids)), 3)  # 2 + 2 + 1 jobs
            self.assertGreaterEqual(fleet.stats["recycled"], 2)
            self.assertEqual(fleet.stats["jobs"], 5)

    def test_timeout_kills_and_pool_recovers(self):
        with pool.Pool(size=1, generation="pool-gen-3",
                       oneshot_fallback=False) as fleet:
            slow = fleet.run("(loop)", timeout_s=2)
            self.assertFalse(slow["ok"])
            self.assertTrue(slow["timed_out"])
            self.assertIn("timeout", slow["error"].lower())
            self.assertEqual(fleet.stats["timeouts"], 1)
            self.assertGreaterEqual(fleet.stats["poisoned"], 1)
            # The replacement worker serves the next job.
            again = fleet.run("(+ 1 2)")
            self.assertTrue(again["ok"], again)
            self.assertEqual(again["return_value"], "3")
            self.assertNotEqual(again["worker_id"], slow["worker_id"])

    def test_killed_worker_poisons_and_falls_back(self):
        with pool.Pool(size=1, generation="pool-gen-4",
                       oneshot_fallback=True) as fleet:
            first = fleet.run("(+ 1 2)")
            self.assertTrue(first["pooled"])
            victim = fleet._idle[0]
            victim._proc.kill()
            recovered = fleet.run("(+ 3 4)")
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(recovered["return_value"], "7")
            self.assertGreaterEqual(fleet.stats["poisoned"], 1)

    def test_reset_all_and_regenerate(self):
        with pool.Pool(size=1, generation="pool-gen-5") as fleet:
            fleet.run("(+ 1 1)")
            ok, total = fleet.reset_all()
            self.assertEqual((ok, total), (1, 1))
            self.assertEqual(fleet.stats["resets"], 1)
            before = fleet.run("(+ 2 2)")["worker_id"]
            fleet.regenerate("pool-gen-6")
            after = fleet.run("(+ 3 3)")
            self.assertTrue(after["ok"], after)
            self.assertNotEqual(after["worker_id"], before)
            self.assertGreaterEqual(fleet.stats["recycled"], 1)

    def test_health_snapshot(self):
        with pool.Pool(size=1, generation="pool-gen-7") as fleet:
            fleet.run("(+ 1 1)")
            snap = fleet.health()
            self.assertEqual(snap["size"], 1)
            self.assertEqual(snap["total"], 1)
            self.assertEqual(snap["idle"], 1)
            self.assertFalse(snap["shutdown"])
            self.assertEqual(len(snap["workers"]), 1)
            self.assertTrue(snap["workers"][0]["ping_ok"])

    def test_concurrent_runs(self):
        with pool.Pool(size=2, generation="pool-gen-8") as fleet:
            results = []
            errors = []

            def job(n):
                try:
                    results.append(fleet.run("(+ %d 100)" % n))
                except Exception as exc:  # noqa: BLE001 - collected
                    errors.append(exc)

            threads = [threading.Thread(target=job, args=(n,))
                       for n in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 6)
            self.assertTrue(all(r["ok"] for r in results), results)
            self.assertEqual(
                sorted(r["return_value"] for r in results),
                [str(100 + n) for n in range(6)])

    def test_run_after_shutdown_raises(self):
        fleet = pool.Pool(size=1, generation="pool-gen-9")
        fleet.shutdown()
        with self.assertRaises(RuntimeError):
            fleet.run("(+ 1 2)")

    def test_bad_inputs_rejected(self):
        with pool.Pool(size=1, generation="pool-gen-10") as fleet:
            with self.assertRaises(TypeError):
                fleet.run(42)
            with self.assertRaises(ValueError):
                fleet.run("(+ 1 2)", timeout_s=0)


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class FallbackTest(unittest.TestCase):
    def test_oneshot_fallback_serves_when_spawn_fails(self):
        # Break persistent spawning only (run_lisp stays viable): the
        # job must still succeed via one-shot fallback.
        with unittest.mock.patch.object(
                pool.Pool, "_spawn_one",
                side_effect=pool.WorkerSpawnError("boom")):
            with pool.Pool(size=1, oneshot_fallback=True) as fleet:
                self.assertFalse(fleet.prewarm_evidence["all_ok"])
                result = fleet.run("(+ 4 5)")
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["return_value"], "9")
                self.assertFalse(result["pooled"])
                self.assertEqual(result["worker_id"], "oneshot")
                self.assertEqual(fleet.stats["oneshot_fallbacks"], 1)

    def test_no_fallback_raises_when_spawn_fails(self):
        with pool.Pool(size=1, sbcl_exe="definitely-not-sbcl-xyz",
                       oneshot_fallback=False) as fleet:
            with self.assertRaises(pool.WorkerSpawnError):
                fleet.run("(+ 4 5)")


class SandboxBoundaryTest(unittest.TestCase):
    """Workers start their child through ossandbox.popen, never a bare Popen."""

    def test_pool_source_has_no_bare_popen(self):
        with open(POOL_SOURCE, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("subprocess.Popen(", source)
        self.assertIn("ossandbox.popen(", source)

    def test_worker_child_is_started_through_ossandbox_popen(self):
        # sys.executable exists, so the spawn reaches popen; popen is patched
        # to refuse. The refusal must surface as a clear spawn error and no
        # bare subprocess.Popen may run.
        refusal = ossandbox.SandboxError("forced refusal for the test")
        with unittest.mock.patch.object(pool.ossandbox, "popen",
                                        side_effect=refusal) as spy, \
                unittest.mock.patch.object(pool.subprocess, "Popen") as bare:
            with self.assertRaises(pool.WorkerSpawnError) as caught:
                pool.PersistentWorker(executable=sys.executable,
                                      generation="test-gen")
        spy.assert_called_once()
        argv = spy.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("--load", argv)
        self.assertEqual(spy.call_args[1]["stdin"], pool.subprocess.PIPE)
        bare.assert_not_called()
        self.assertIn("OS sandbox refused", str(caught.exception))
        self.assertIn("forced refusal", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ossandbox.SandboxError)

    def test_pool_refusal_is_a_spawn_error_without_one_shot_fallback(self):
        refusal = ossandbox.SandboxError("forced refusal for the test")
        with unittest.mock.patch.object(pool.ossandbox, "popen", side_effect=refusal), \
                unittest.mock.patch.object(pool.subprocess, "Popen") as bare:
            fleet = pool.Pool(size=1, sbcl_exe=sys.executable, generation="gen-x",
                              oneshot_fallback=False)
            try:
                self.assertFalse(fleet.prewarm_evidence["all_ok"])
                with self.assertRaises(pool.WorkerSpawnError):
                    fleet.run("(+ 1 2)")
            finally:
                fleet.shutdown()
        bare.assert_not_called()


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class SandboxedWorkerTest(unittest.TestCase):
    def test_real_worker_child_runs_inside_the_boundary(self):
        real_popen = ossandbox.popen
        with unittest.mock.patch.object(pool.ossandbox, "popen",
                                        wraps=real_popen) as spy:
            with pool.PersistentWorker(generation="test-gen") as worker:
                result = worker.run("(+ 1 2)")
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["return_value"], "3")
                mechanisms = tuple(worker._proc.mechanisms)
        spy.assert_called_once()
        if ossandbox.available()[0]:
            self.assertEqual(set(mechanisms), set(ossandbox.MECHANISMS))

    def test_teardown_releases_the_sandboxed_child(self):
        real_release = ossandbox.release
        with unittest.mock.patch.object(pool.ossandbox, "release",
                                        wraps=real_release) as spy:
            worker = pool.PersistentWorker(generation="test-gen")
            proc = worker._proc
            worker.close()
        spy.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()
