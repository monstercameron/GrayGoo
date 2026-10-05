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
           #:current-schema-version
           #:schema-version-p
           #:schema-version-version
           #:schema-version-compatible-with
           #:schema-evolution-disabled
           #:disabled-migration
           #:migration
           #:migration-p
           #:migration-from-version
           #:migration-to-version
           #:migration-forward
           #:migration-backward
           #:migration-compatibility
           #:migration-rollback-strategy
           #:define-migration
           #:find-migration
           #:check-compatibility
           #:apply-migration))

(defpackage :evo.recovery
  (:use :cl)
  (:documentation "Boot recovery from stable generation manifests.")
  (:export #:recover
           #:verify-manifest))
