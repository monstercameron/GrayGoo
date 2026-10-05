;;;; src/promotion/packages.lisp — Promotion-authority package definitions.
;;;;
;;;; Plan reference: plan.md §28 (promotion authority), §39-40
;;;; (meta-improvement loop). Only the promotion subsystem may alter
;;;; canonical executable state (plan.md §4.1); policy evolution is separate
;;;; from application evolution and never touches the trust kernel.

(defpackage :evo.promotion
  (:use :cl)
  (:documentation "Trusted promotion authority and dispatch epochs.")
  (:export #:promote
           #:promotion-scope
           #:promotion-decision))

(defpackage :evo.policy
  (:use :cl)
  (:documentation "Agent-policy representation (retrieval, context, repair).")
  (:export #:policy-generation
           #:current-policy-generation
           #:propose-policy-change))
