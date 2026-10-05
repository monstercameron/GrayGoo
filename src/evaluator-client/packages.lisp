;;;; src/evaluator-client/packages.lisp — Trusted-evaluator client definition.
;;;;
;;;; Plan reference: plan.md §6 Domain D, §36 (evaluator design). The
;;;; evaluator runs outside the mutable runtime; this package is the
;;;; organism-side client. The organism can never inspect hidden tests.

(defpackage :evo.eval-client
  (:use :cl)
  (:documentation "Client for the external trusted evaluator (verdicts only).")
  (:export #:verdict
           #:make-verdict
           #:request-verdict
           #:verdict-pass-p))
