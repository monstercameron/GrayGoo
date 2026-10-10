"""lispserver: a persistent SBCL child gives the same answers as a fresh worker, fast."""
import os
import statistics
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lispserver  # noqa: E402
import workers  # noqa: E402

PRELUDE = ('(defun sq (x) (* x x))\n'
           '(defun greet (name) (format nil "hi ~a" name))\n'
           '(defun noisy (x) (format t "GGR ok 00 00~%printed ~a~%" x) (* 2 x))')


@unittest.skipUnless(os.path.exists(workers.resolve_sbcl()), "SBCL not found")
class LispServerTests(unittest.TestCase):
    def setUp(self):
        self.srv = lispserver.LispServer(PRELUDE, timeout_s=10.0)

    def tearDown(self):
        self.srv.close()

    def test_values_match_a_fresh_worker(self):
        for form in ("(sq 12)", '(greet "Cam")', "(list 1 \"a\\\"b\" :key 'sym 2.5)",
                     '(format nil "line1~%line2 \\\\ end")', "nil", "(sq (sq 3))"):
            fresh = workers.run_lisp("%s\n%s" % (PRELUDE, form))
            got = self.srv.eval(form)
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["return_value"], fresh["return_value"], form)

    def test_printed_output_is_captured_and_cannot_fake_a_reply(self):
        got = self.srv.eval("(noisy 21)")
        self.assertEqual((got["ok"], got["return_value"]), (True, "42"))
        self.assertIn("printed 21", got["stdout"])
        self.assertIn("GGR ok 00 00", got["stdout"])

    def test_errors_do_not_kill_the_server(self):
        for bad in ("(car 5)", "(no-such-function 1)", "(sq 1", "#.(sq 2)", "(error \"boom\")"):
            got = self.srv.eval(bad)
            self.assertFalse(got["ok"], bad)
            self.assertTrue(got["error"], bad)
        self.assertEqual(self.srv.eval("(sq 5)")["return_value"], "25")

    def test_reload_replaces_definitions(self):
        self.assertEqual(self.srv.eval("(sq 3)")["return_value"], "9")
        self.srv.reload("(defun sq (x) (+ x 1))")
        self.assertEqual(self.srv.eval("(sq 3)")["return_value"], "4")

    def test_a_bad_prelude_is_reported_and_reload_recovers(self):
        self.srv.reload("(defun broken (x) (")
        got = self.srv.eval("(+ 1 1)")
        self.assertFalse(got["ok"])
        self.assertIn("failed to load", got["error"])
        self.srv.reload(PRELUDE)
        self.assertEqual(self.srv.eval("(sq 4)")["return_value"], "16")

    def test_timeout_restarts_and_the_next_call_works(self):
        slow = lispserver.LispServer(PRELUDE, timeout_s=1.0)
        try:
            got = slow.eval("(loop)")
            self.assertTrue(got["timed_out"])
            self.assertFalse(got["ok"])
            self.assertEqual(slow.eval("(sq 6)")["return_value"], "36")
        finally:
            slow.close()

    def test_a_killed_child_is_replaced(self):
        self.srv.eval("(sq 2)")
        self.srv.proc.kill()
        self.srv.proc.wait(timeout=5)
        self.assertEqual(self.srv.eval("(sq 7)")["return_value"], "49")

    def test_calls_from_several_threads_are_all_correct(self):
        results, errors = {}, []

        def work(k):
            for i in range(5):
                got = self.srv.eval("(sq %d)" % (k * 10 + i))
                if not got["ok"]:
                    errors.append(got)
                results[k * 10 + i] = got["return_value"]
        threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(results, {n: str(n * n) for n in results})
        self.assertEqual(len(results), 20)

    def test_it_is_much_faster_than_a_fresh_process(self):
        self.srv.eval("(sq 1)")                                   # start-up is not the point
        times = []
        for i in range(30):
            t0 = time.perf_counter()
            self.srv.eval("(sq %d)" % i)
            times.append((time.perf_counter() - t0) * 1000)
        fresh = []
        for i in range(5):
            t0 = time.perf_counter()
            workers.run_lisp("%s\n(sq %d)" % (PRELUDE, i))
            fresh.append((time.perf_counter() - t0) * 1000)
        med, med_fresh = statistics.median(times), statistics.median(fresh)
        print("\npersistent median %.2f ms, fresh process median %.1f ms" % (med, med_fresh))
        self.assertLess(med, 50)
        self.assertLess(med, med_fresh / 3)

    def test_close_leaves_no_child_and_cleans_its_files(self):
        self.srv.eval("(sq 2)")
        proc, files = self.srv.proc, list(self.srv._files)
        self.srv.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(any(os.path.exists(f) for f in files))


@unittest.skipUnless(os.path.exists(workers.resolve_sbcl()), "SBCL not found")
class CacheTests(unittest.TestCase):
    def tearDown(self):
        lispserver.close_all()

    def test_reuse_reload_and_eviction(self):
        a = lispserver.cached_server("a", "(defun f () 1)")
        self.assertIs(lispserver.cached_server("a", "(defun f () 1)"), a)
        self.assertEqual(a.eval("(f)")["return_value"], "1")
        self.assertIs(lispserver.cached_server("a", "(defun f () 2)"), a)
        self.assertEqual(a.eval("(f)")["return_value"], "2")
        for n in range(lispserver.CACHE_MAX):
            lispserver.cached_server("k%d" % n, "(defun f () 0)")
        self.assertNotIn("a", lispserver._CACHE)
        self.assertEqual(len(lispserver._CACHE), lispserver.CACHE_MAX)
        self.assertIsNone(a.proc)                                 # evicted server was closed

    def test_a_prelude_that_only_grew_is_added_without_a_restart(self):
        a = lispserver.cached_server("grow", "(defun f () 1)")
        self.assertEqual(a.eval("(f)")["return_value"], "1")
        child = a.proc.pid
        b = lispserver.cached_server("grow", "(defun f () 1)\n(defun g () (+ 1 (f)))")
        self.assertIs(b, a)
        self.assertEqual(a.eval("(g)")["return_value"], "2")
        self.assertEqual(a.proc.pid, child)                       # same process: nothing restarted
        a.proc.kill()                                             # a restart loads EVERYTHING
        a.proc.wait(timeout=5)
        self.assertEqual(a.eval("(g)")["return_value"], "2")
        self.assertNotEqual(a.proc.pid, child)

    def test_a_changed_definition_restarts_and_a_bad_addition_is_not_half_loaded(self):
        a = lispserver.cached_server("chg", "(defun f () 1)")
        a.eval("(f)")
        child = a.proc.pid
        lispserver.cached_server("chg", "(defun f () 5)")          # not an extension
        self.assertEqual(a.eval("(f)")["return_value"], "5")
        self.assertNotEqual(a.proc.pid, child)
        lispserver.cached_server("chg", "(defun f () 5)\n(defun h () (undefined-macro-form")
        got = a.eval("(f)")
        self.assertFalse(got["ok"])                               # the broken prelude is reported
        self.assertIn("failed to load", got["error"])

    def test_drop_stops_one_server(self):
        a = lispserver.cached_server("gone", "(defun f () 1)")
        a.eval("(f)")
        lispserver.drop("gone")
        self.assertNotIn("gone", lispserver._CACHE)
        self.assertIsNone(a.proc)
        lispserver.drop("gone")                                   # twice is harmless


if __name__ == "__main__":
    unittest.main()
