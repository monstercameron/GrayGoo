;;;; src/state/packages.lisp — State, schema, and recovery definitions.
;;;;
;;;; Plan reference: plan.md §24 (state model), §25 (schema evolution),
;;;; §44-45 (recovery, checkpointing). Canonical state is transactional and
;;;; versioned; candidates touch only speculative branches.

(defpackage :evo.state
  (:use :cl)
  (:documentation "Canonical state handles and speculative branches.")
  (:export #:state-generation
           #:current-state-generation
           #:with-speculative-branch))

(defpackage :evo.schema
  (:use :cl)
  (:documentation "Schema versions and migration records.")
  (:export #:schema-version
           #:make-schema-version
           #:current-schema-version))

(defpackage :evo.recovery
  (:use :cl)
  (:documentation "Boot recovery from stable generation manifests.")
  (:export #:recover
           #:verify-manifest))
