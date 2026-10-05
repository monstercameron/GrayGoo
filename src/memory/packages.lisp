;;;; src/memory/packages.lisp — Procedural memory package definitions.
;;;;
;;;; Plan reference: plan.md §11 (dual procedural memory), §30-33 (transfer
;;;; evaluation, expiration, consolidation, entropy). Memory pairs semantic
;;;; skill descriptions with executable specializations plus evidence.

(defpackage :evo.memory
  (:use :cl)
  (:documentation "Skill families, retrieval, and TTL (dual memory).")
  (:export #:skill-family
           #:make-skill-family
           #:find-skills
           #:record-reuse))

(defpackage :evo.transfer
  (:use :cl)
  (:documentation "Transfer gate: patch → skill promotion evidence (§29-30).")
  (:export #:transfer-evidence
           #:make-transfer-evidence
           #:transfer-green-p))

(defpackage :evo.consolidation
  (:use :cl)
  (:documentation "Duplicate detection, merges, and retirement (§32-33).")
  (:export #:propose-consolidation
           #:entropy-report))
