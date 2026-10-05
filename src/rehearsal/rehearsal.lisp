;;;; src/rehearsal/rehearsal.lisp — Rehearsal orchestration (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §7 (R0–R6), §21 (phases A–I), §69. Structural
;;;; validation and risk classification run deterministically; worker
;;;; execution arrives with the rehearsal lane.

(in-package :evo.rehearsal)

(defvar *risk-levels* '(:r0 :r1 :r2 :r3 :r4 :r5 :r6)
  "Mutation risk classes, pure function (:r0) to kernel (:r6), §7.")

(defstruct (rehearsal-verdict
            (:constructor %make-rehearsal-verdict))
  "Green/red plus the phase that decided and collected evidence."
  (green-p nil :type boolean)
  (deciding-phase nil :type symbol)
  (evidence nil :type list))

(defun rehearsal-verdict (green-p deciding-phase &optional evidence)
  "Construct a rehearsal verdict. Test/bootstrap use only."
  (%make-rehearsal-verdict :green-p green-p :deciding-phase deciding-phase
                           :evidence evidence))

(defun classify-risk (candidate)
  "Classify CANDIDATE into a risk level. Bootstrap: unknown candidates
are conservatively classified :r6 (unpromotable) until the static
effect scanner lands."
  (declare (ignore candidate))
  :r6)

(defun rehearse (candidate risk)
  "Rehearse CANDIDATE at RISK level through phases A–I. Bootstrap:
signals an error — no worker pool exists yet."
  (declare (ignore candidate risk))
  (error "Rehearsal pipeline not implemented (bootstrap skeleton)."))

;;;; Rehearsal-thunk entry point (worker spike; plan.md §21).
;;;;
;;;; Thin alias over the worker-side convention so rehearsal phases can call
;;;; RUN-TEST-THUNK without depending on worker internals. Requires the
;;;; EVO.WORKER package (its module loads before this one; see graygoo.asd).

(defun run-test-thunk (thunk)
  "Call THUNK via the worker thunk convention.
See EVO.WORKER:RUN-TEST-THUNK for the returned values."
  (evo.worker:run-test-thunk thunk))
