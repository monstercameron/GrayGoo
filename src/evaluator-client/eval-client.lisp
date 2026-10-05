;;;; src/evaluator-client/eval-client.lisp — Evaluator client (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §21 phase I (hidden evaluation), §36-37. The
;;;; client ships candidate + evidence to the evaluator and receives a
;;;; pass/fail verdict. No hidden-test access exists on this side.

(in-package :evo.eval-client)

(defstruct (verdict
            (:constructor %make-verdict))
  "Pass/fail plus opaque rationale. Never carries hidden-test content."
  (pass-p nil :type boolean)
  (rationale "" :type string))

(defun make-verdict (pass-p &optional (rationale ""))
  "Construct a verdict record. Test/bootstrap use only."
  (%make-verdict :pass-p pass-p :rationale rationale))

(defun request-verdict (candidate evidence)
  "Request a hidden-evaluation verdict. Bootstrap: signals an error —
no evaluator service exists yet. CANDIDATE and EVIDENCE are accepted
for API stability."
  (declare (ignore candidate evidence))
  (error "Evaluator service not implemented (bootstrap skeleton)."))
