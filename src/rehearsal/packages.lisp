;;;; src/rehearsal/packages.lisp — Rehearsal orchestration definition.
;;;;
;;;; Plan reference: plan.md §7 (mutation risk levels), §21 (rehearsal
;;;; phases A–I), §69 (rehearsal algorithm). Every candidate rehearses in
;;;; disposable workers before any promotion decision.

(defpackage :evo.rehearsal
  (:use :cl)
  (:documentation "Risk classification and rehearsal orchestration.")
  (:export #:*risk-levels*
           #:classify-risk
           #:rehearse
           #:rehearsal-verdict
           #:run-test-thunk))
