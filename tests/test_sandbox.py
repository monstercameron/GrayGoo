"""Tests for the worker-sandbox hardening (sandbox.py + risk R6 rule).

Stdlib unittest only. SBCL tests are gated on the executable's presence;
everything else is hermetic (no worker runs, no network, no secrets).
Corpus fixtures live in TEMP with unique names and are always removed.
"""

import json
import os
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import risk
import s_expr
import sandbox
import workers
from evaluator import service as eval_service

SBCL_AVAILABLE = os.path.exists(workers.resolve_sbcl())

PURE_CANDIDATE = (
    "(candidate (:target rank-items) (:parent 0) "
    "(:definition (lambda (items) (mapcar score-item items))))"
)

# Report's kernel-mutation payload (documents/adversarial-report.md).
KERNEL_CANDIDATE = (
    "(candidate (:target evo.dispatch:invoke-capability) (:parent 0) "
    "(:definition (defun evo.dispatch:invoke-capability (id) :pwned)))"
)

PROCESS_CANDIDATE = (
    "(candidate (:target run-worker) (:parent 0) "
    '(:definition (lambda (cmd) (sb-ext:run-program cmd (list "/c" cmd)))))'
)

NETWRITE_CANDIDATE = (
    "(candidate (:target push-event) (:parent 0) "
    '(:definition (lambda (addr) (socket-connect addr 80))))'
)

FILE_READ_CANDIDATE = (
    "(candidate (:target export-report) (:parent 5) "
    "(:definition (lambda (rows path) (with-open-file "
    "(out path :direction :output) (write-rows rows out)))))"
)


def classify_text(text):
    return risk.classify(s_expr.parse_candidate(text))


class DispatchR6Test(unittest.TestCase):
    def test_report_payload_is_r6(self):
        result = classify_text(KERNEL_CANDIDATE)
        self.assertEqual(result["level"], "R6")
        self.assertEqual(result["gates"],
                         ["promotion-forbidden", "external-release-only"])
        self.assertTrue(any("promotion-forbidden" in reason
                            for reason in result["reasons"]))

    def test_setf_redefinition_of_kernel_symbol_is_r6(self):
        result = classify_text(
            "(candidate (:target hot-patch) (:parent 0) "
            "(:definition (lambda (code) "
            "(setf (symbol-function (quote evo.kernel:run)) code))))")
        self.assertEqual(result["level"], "R6")

    def test_defmethod_on_entrypoint_is_r6(self):
        result = classify_text(
            "(candidate (:target m) (:parent 0) "
            "(:definition (defmethod invoke-capability ((x t)) x)))")
        self.assertEqual(result["level"], "R6")

    def test_plain_setf_of_variable_stays_r3(self):
        result = classify_text(
            "(candidate (:target hot-patch) (:parent 2) "
            "(:definition (lambda (name code) "
            "(setf (symbol-function name) code))))")
        self.assertEqual(result["level"], "R3")

    def test_pure_code_still_r0(self):
        result = classify_text(PURE_CANDIDATE)
        self.assertEqual(result["level"], "R0")


class PolicyGateTest(unittest.TestCase):
    def test_blocks_process_spawn_payload(self):
        reasons = sandbox.refuse_to_rehearse(
            PROCESS_CANDIDATE, classify_text(PROCESS_CANDIDATE))
        self.assertTrue(reasons)
        self.assertTrue(any("process" in reason.lower()
                            for reason in reasons))

    def test_blocks_process_spawn_even_when_approved(self):
        reasons = sandbox.refuse_to_rehearse(
            PROCESS_CANDIDATE, classify_text(PROCESS_CANDIDATE),
            approved=True)
        self.assertTrue(reasons)

    def test_blocks_network_write_payload(self):
        reasons = sandbox.refuse_to_rehearse(
            NETWRITE_CANDIDATE, classify_text(NETWRITE_CANDIDATE))
        self.assertTrue(reasons)
        self.assertTrue(any("network" in reason.lower()
                            for reason in reasons))

    def test_unapproved_r4_refused_but_approved_soft_r4_allowed(self):
        assessed = classify_text(FILE_READ_CANDIDATE)
        self.assertEqual(assessed["level"], "R4")
        refused = sandbox.refuse_to_rehearse(FILE_READ_CANDIDATE, assessed)
        self.assertTrue(refused)
        self.assertTrue(any("approval" in reason.lower()
                            for reason in refused))
        self.assertEqual(
            sandbox.refuse_to_rehearse(FILE_READ_CANDIDATE, assessed,
                                       approved=True),
            [])

    def test_r6_always_refused(self):
        assessed = classify_text(KERNEL_CANDIDATE)
        self.assertEqual(assessed["level"], "R6")
        for approved in (False, True):
            reasons = sandbox.refuse_to_rehearse(
                KERNEL_CANDIDATE, assessed, approved=approved)
            self.assertTrue(reasons)
            self.assertTrue(any("R6" in reason for reason in reasons))

    def test_allows_pure_payload(self):
        self.assertEqual(
            sandbox.refuse_to_rehearse(
                PURE_CANDIDATE, classify_text(PURE_CANDIDATE)),
            [])

    def test_eval_quoted_process_payload_refused(self):
        # Issue 74: (eval '(sb-ext:run-program ...)) must not slip
        # past the hard process deny inside quoted code.
        sneaky = (
            "(candidate (:target sneak) (:parent 0) "
            "(:definition (lambda (cmd) "
            "(eval (list 'sb-ext:run-program cmd)))))"
        )
        assessed = classify_text(sneaky)
        reasons = sandbox.refuse_to_rehearse(sneaky, assessed,
                                             approved=True)
        self.assertTrue(reasons, assessed)
        self.assertTrue(any("process" in reason.lower()
                            for reason in reasons))

    def test_plain_quoted_data_still_skipped(self):
        # Quoted data NOT under eval stays unscanned (no false positive
        # from merely mentioning a denied operator as data).
        quoted_data = (
            "(candidate (:target describe) (:parent 0) "
            "(:definition (lambda () "
            "(list 'sb-ext:run-program 'socket-connect))))"
        )
        assessed = classify_text(quoted_data)
        self.assertEqual(assessed["level"], "R0")
        self.assertEqual(sandbox.refuse_to_rehearse(quoted_data, assessed),
                         [])

    def test_garbage_refused_not_allowed(self):
        pure = classify_text(PURE_CANDIDATE)
        self.assertTrue(sandbox.refuse_to_rehearse("not a form", pure))
        self.assertTrue(sandbox.refuse_to_rehearse(PURE_CANDIDATE, {}))
        with self.assertRaises(TypeError):
            sandbox.refuse_to_rehearse(None, pure)


class PreludeJailTest(unittest.TestCase):
    def test_build_prelude_installs_sandbox(self):
        self.assertIn("install-worker-sandbox", sandbox.build_prelude())

    def test_build_prelude_pins_jail_root(self):
        with sandbox.WorkerJail() as jail:
            prelude = sandbox.build_prelude(jail)
            self.assertIn(jail.root.replace("\\", "/"), prelude)

    def test_jail_lifecycle_audits_and_cleans_up(self):
        with sandbox.WorkerJail() as jail:
            root = jail.root
            self.assertTrue(os.path.isdir(root))
            jail.overlay.write("note.txt", "audit-me")
            self.assertEqual(jail.diff(), {"note.txt": "added"})
        self.assertEqual(jail.changes, {"note.txt": "added"})
        self.assertFalse(os.path.exists(root))

    def test_jail_exit_rolls_back_state(self):
        jail = sandbox.WorkerJail()
        with jail:
            jail.state.execute("CREATE TABLE t (x)")
            self.assertEqual(jail.state.depth, 1)
        # Exit drained every open transaction level.
        self.assertEqual(jail.state.depth, 0)


class CorpusEnvTest(unittest.TestCase):
    def test_env_override_respected(self):
        corpus = [{"id": "sandbox-probe-%s" % uuid.uuid4().hex[:8],
                   "checks": [{"input": "a", "expected": "a",
                               "compare": "exact"}]}]
        path = os.path.join(tempfile.gettempdir(),
                            "graygoo-corpus-%s-%s.json"
                            % (os.getpid(), uuid.uuid4().hex[:12]))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(corpus, handle)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        previous = os.environ.get(eval_service.CORPUS_ENV_VAR)
        os.environ[eval_service.CORPUS_ENV_VAR] = path
        self.addCleanup(lambda: (
            os.environ.__setitem__(eval_service.CORPUS_ENV_VAR, previous)
            if previous is not None
            else os.environ.pop(eval_service.CORPUS_ENV_VAR, None)))
        self.assertEqual(eval_service.resolve_corpus_path(), path)
        loaded = eval_service.load_hidden_cases()
        self.assertEqual([c["id"] for c in loaded], [corpus[0]["id"]])

    def test_default_when_env_unset(self):
        previous = os.environ.pop(eval_service.CORPUS_ENV_VAR, None)
        self.addCleanup(lambda: previous is None or os.environ.__setitem__(
            eval_service.CORPUS_ENV_VAR, previous))
        self.assertEqual(eval_service.resolve_corpus_path(),
                         eval_service.CASES_PATH)
        self.assertEqual(eval_service.resolve_corpus_path("explicit.json"),
                         "explicit.json")


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class SandboxedWorkerTest(unittest.TestCase):
    def test_benign_runs_but_spawn_is_denied(self):
        benign = workers.run_lisp("(+ 1 2)", timeout_s=30)
        self.assertTrue(benign["ok"], benign)
        self.assertEqual(benign["return_value"], "3")
        spawn = workers.run_lisp(
            '(sb-ext:run-program "cmd" (list "/c" "echo x") :search t)',
            timeout_s=30)
        self.assertFalse(spawn["ok"])
        lowered = (spawn["error"] or "").lower()
        self.assertTrue(any(word in lowered
                            for word in ("denied", "forbidden", "sandbox")),
                        spawn["error"][:300])


@unittest.skipUnless(SBCL_AVAILABLE, "SBCL executable not found")
class ReaderEvalAttackTest(unittest.TestCase):
    """issues.md #1: #. reader evaluation must not fire on worker reads."""

    ORACLE = ('#.(progn (defun graygoo-read-pwn () :pwned) :x) '
              '(if (fboundp (quote graygoo-read-pwn)) '
              ':read-eval-fired :not-fired)')

    def _assert_blocked(self, result):
        self.assertFalse(result["ok"], result)
        self.assertIn("READ-EVAL", (result["error"] or "").upper())
        self.assertNotIn("READ-EVAL-FIRED",
                         (result["return_value"] or "").upper())

    def test_sharp_dot_does_not_evaluate_sandboxed(self):
        self._assert_blocked(workers.run_lisp(self.ORACLE, timeout_s=30))

    def test_sharp_dot_does_not_evaluate_unsandboxed(self):
        # The fix lives in the read path itself, not the sandbox prelude.
        self._assert_blocked(
            workers.run_lisp(self.ORACLE, timeout_s=30, sandbox=False))


if __name__ == "__main__":
    unittest.main()
