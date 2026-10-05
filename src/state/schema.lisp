;;;; src/state/schema.lisp — Schema versions (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §25 (schema evolution). Schema mutations are
;;;; first-class: old/new version, forward migration, compatibility
;;;; statement, rollback strategy. MVP excludes self-migration (§79).

(in-package :evo.schema)

(defstruct (schema-version
            (:constructor %make-schema-version))
  "Version number plus compatibility statement."
  (version 0 :type integer)
  (compatible-with nil :type list))

(defvar *current-schema-version* (%make-schema-version :version 0)
  "Active schema version of this image.")

(defun make-schema-version (version &key compatible-with)
  "Construct a schema version record."
  (%make-schema-version :version version :compatible-with compatible-with))

(defun current-schema-version ()
  "Return the active schema version record."
  *current-schema-version*)
