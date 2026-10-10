"""lispserver state: tamper checks, restarts, and isolation between requests."""
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
           '(defun gg-check (got want) (if (equal got want) t (list :fail got want)))')


@unittest.skipUnless(os.path.exists(workers.resolve_sbcl()), "SBCL not found")
class TamperCheckTests(unittest.TestCase):
    def setUp(self):
        self.srv = lispserver.LispServer(PRELUDE, timeout_s=10.0)

    def tearDown(self):
        self.srv.close()

    def assertChanged(self, names):
        report = self.srv.intact()
        self.assertEqual(report["ok"], not names, report)
        self.assertEqual(sorted(report["changed"]), sorted(names), report)

    def test_a_fresh_server_is_intact(self):
        self.assertEqual(self.srv.intact(), {"ok": True, "changed": []})
        self.assertEqual(self.srv.eval("(sq 4)")["return_value"], "16")

    def test_a_defun_redefinition_is_reported_by_name(self):
        self.srv.eval("(defun sq (x) 0)")
        self.assertChanged(["sq"])

    def test_setf_symbol_function_is_reported(self):
        self.srv.eval("(setf (symbol-function 'greet) (lambda (n) n))")
        self.assertChanged(["greet"])

    def test_setf_fdefinition_is_reported(self):
        self.srv.eval("(setf (fdefinition 'gg-check) (lambda (got want) t))")
        self.assertChanged(["gg-check"])

    def test_fmakunbound_is_reported(self):
        self.srv.eval("(fmakunbound 'sq)")
        self.assertChanged(["sq"])

    def test_forms_that_only_call_functions_report_nothing(self):
        got = self.srv.eval('(list (sq 3) (greet "Cam") (gg-check 2 2))')
        self.assertEqual(got["return_value"], '(9 "hi Cam" T)')
        self.assertChanged([])

    def test_a_declared_redefinition_is_accepted_but_a_second_change_is_not(self):
        got = self.srv.eval("(progn (defun sq (x) (+ x 100)) "
                            "(setf (symbol-function 'greet) (lambda (n) n)) (sq 1))",
                            restore=["sq"])
        self.assertEqual(got["return_value"], "101")
        self.assertChanged(["greet"])
        self.srv.eval("(defun sq (x) (* x x))", restore=["sq"])     # the saved version back
        self.assertEqual(self.srv.eval("(sq 3)")["return_value"], "9")
        self.assertChanged(["greet"])

    def test_a_declared_redefinition_leaves_no_report_on_its_own(self):
        self.srv.eval("(defun sq (x) 0)", restore=["sq"])
        self.assertChanged([])

    def test_a_form_that_fails_part_way_is_still_checked(self):
        got = self.srv.eval("(progn (defun greet (n) n) (car 5))", restore=["sq"])
        self.assertFalse(got["ok"])
        self.assertChanged(["greet"])

    def test_restart_returns_to_the_canonical_definitions(self):
        self.srv.eval("(progn (defun sq (x) 0) (defparameter *junk* 1))")
        self.assertChanged(["sq"])
        self.srv.restart()
        self.assertEqual(self.srv.intact(), {"ok": True, "changed": []})
        self.assertEqual(self.srv.eval("(sq 3)")["return_value"], "9")

    def test_extend_adds_protected_definitions(self):
        self.srv.eval("(sq 2)")
        self.assertTrue(self.srv.extend("\n(defun cube (x) (* x x x))"))
        self.assertEqual(self.srv.eval("(cube 2)")["return_value"], "8")
        self.assertChanged([])
        self.srv.eval("(defun cube (x) 0)")
        self.assertChanged(["cube"])
        self.srv.restart()
        self.assertEqual(self.srv.eval("(cube 3)")["return_value"], "27")
        self.assertChanged([])

    def test_a_global_survives_plain_evals_but_not_a_restart(self):
        self.srv.eval("(defparameter *leak* 1)")
        # contamination the caller must know about: plain evals share one image
        self.assertEqual(self.srv.eval("*leak*")["return_value"], "1")
        self.srv.restart()
        got = self.srv.eval("*leak*")
        self.assertFalse(got["ok"])
        self.assertIn("LEAK", got["error"])

    def test_concurrent_evals_each_get_their_own_answer(self):
        results, errors = {}, []

        def work(k):
            for i in range(5):
                n = k * 10 + i
                got = self.srv.eval("(sq %d)" % n)
                if not got["ok"]:
                    errors.append(got)
                results[n] = got["return_value"]
        threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 40)
        self.assertEqual(results, {n: str(n * n) for n in results})
        self.assertChanged([])

    def test_a_timed_out_form_is_followed_by_a_correct_answer_and_an_intact_image(self):
        slow = lispserver.LispServer(PRELUDE, timeout_s=1.0)
        try:
            got = slow.eval("(loop)")
            self.assertTrue(got["timed_out"])
            self.assertEqual(slow.eval("(sq 6)")["return_value"], "36")
            self.assertEqual(slow.intact(), {"ok": True, "changed": []})
        finally:
            slow.close()

    def test_a_candidate_cannot_disable_the_check_by_guessed_names(self):
        self.srv.eval("(sq 1)")
        self.assertNotIn(self.srv._sym, ("GG-INTACT", "GG-TRUSTED"))
        self.assertNotIn(self.srv._pkg, ("GG-INTACT", "GG-TRUSTED"))
        got = self.srv.eval("(progn (ignore-errors (fmakunbound 'gg-intact)) "
                            "(defun gg-intact () (list)) (defun gg-trusted () nil) t)")
        self.assertTrue(got["ok"], got)
        self.assertChanged([])
        self.srv.eval("(defun sq (x) 0)")
        self.assertChanged(["sq"])


@unittest.skipUnless(os.path.exists(workers.resolve_sbcl()), "SBCL not found")
class TamperCheckCostTests(unittest.TestCase):
    def test_one_intact_call_is_cheap_on_a_thirty_function_prelude(self):
        prelude = "\n".join("(defun f%d (x) (+ x %d))" % (i, i) for i in range(30))
        srv = lispserver.LispServer(prelude, timeout_s=10.0)
        try:
            srv.eval("(f0 1)")
            srv.intact()                                           # warm the pipe
            times = []
            for _ in range(40):
                t0 = time.perf_counter()
                self.assertTrue(srv.intact()["ok"])
                times.append((time.perf_counter() - t0) * 1000)
            median = statistics.median(times)
            print("\nintact() median %.3f ms, max %.3f ms, over %d recorded functions"
                  % (median, max(times), len(lispserver._PROTOCOL_NAMES) + 30))
            self.assertLess(median, 5.0)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
