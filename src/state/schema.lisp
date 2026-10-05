;;;; src/state/schema.lisp — Schema versions + versioned-migration design.
;;;;
;;;; Plan reference: plan.md §25 (schema evolution). Schema mutations are
;;;; first-class: old/new version, forward migration, compatibility
;;;; statement, rollback strategy. MVP excludes self-migration (§79).
;;;;
;;;; VERSIONED-MIGRATION DESIGN (issues.md #18; NOT ENABLED):
;;;;
;;;; A migration moves canonical state from one schema version to the next:
;;;;
;;;;   1. DEFINE-MIGRATION records a MIGRATION (from/to versions, FORWARD
;;;;      and BACKWARD thunks, COMPATIBILITY statement, ROLLBACK-STRATEGY).
;;;;      Registration is inert data: it never touches canonical state.
;;;;   2. CHECK-COMPATIBILITY statically compares the two endpoint schema
;;;;      versions (compatible-with lists, declared additions/removals)
;;;;      and returns a verdict WITHOUT applying anything.
;;;;   3. APPLY-MIGRATION would run FORWARD inside a speculative branch
;;;;      (EVO.STATE:WITH-SPECULATIVE-BRANCH), rehearse the branch, and
;;;;      canonically commit only on green — with BACKWARD + the rollback
;;;;      strategy armed for instant reversion.
;;;;
;;;; Step 3 is DISABLED: APPLY-MIGRATION signals SCHEMA-EVOLUTION-DISABLED
;;;; unconditionally. Enabling it requires, at minimum: rehearsed
;;;; migration runs, compatibility verdicts wired into promotion gates,
;;;; rollback drills on production-shaped data, and a coordinator decision
;;;; flipping SCHEMA-EVOLUTION-DISABLED to NIL. Until then schema changes
;;;; ship only via external release (same rule as plan.md §4.3 trust-root
;;;; changes).

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

;;;; Disabled-migration machinery (design + stubs; never enabled here). ----

(defvar SCHEMA-EVOLUTION-DISABLED t
  "Explicit kill-switch for schema evolution (issues.md #18).
While true (the only supported value), APPLY-MIGRATION refuses every
migration: schema changes ship via external release only. Flip to NIL
only after the design note at the top of this file is satisfied and
the coordinator approves.")

(define-condition schema-evolution-disabled (error)
  ((migration :initarg :migration :reader disabled-migration))
  (:report (lambda (condition stream)
             (format stream "Schema evolution is disabled; refusing ~S. ~
See EVO.SCHEMA:SCHEMA-EVOLUTION-DISABLED."
                     (disabled-migration condition)))))

(defstruct (migration
            (:constructor %make-migration)
            (:predicate migration-p))
  "Inert versioned-migration record: FROM-VERSION and TO-VERSION are
schema version integers; FORWARD/BACKWARD are thunks (or design-time
descriptions); COMPATIBILITY is the compatibility statement;
ROLLBACK-STRATEGY names how a committed migration reverts."
  (from-version 0 :type integer :read-only t)
  (to-version 0 :type integer :read-only t)
  (forward nil :type t :read-only t)
  (backward nil :type t :read-only t)
  (compatibility nil :type t :read-only t)
  (rollback-strategy nil :type t :read-only t))

(defvar *migrations* (make-hash-table :test 'equal)
  "(FROM-VERSION . TO-VERSION) -> MIGRATION design records.")

(defun define-migration (from-version to-version &key forward backward
                                                  compatibility
                                                  rollback-strategy)
  "Record a versioned migration design from FROM-VERSION to TO-VERSION.
Inert: registers the record for inspection and rehearsal design only;
it never touches canonical state. Re-registering a pair is an error."
  (unless (and (integerp from-version) (integerp to-version))
    (error "Migration versions must be integers, got ~S -> ~S."
           from-version to-version))
  (let ((key (cons from-version to-version)))
    (when (gethash key *migrations*)
      (error "Migration ~S -> ~S is already defined." from-version
             to-version))
    (setf (gethash key *migrations*)
          (%make-migration :from-version from-version
                           :to-version to-version
                           :forward forward :backward backward
                           :compatibility compatibility
                           :rollback-strategy rollback-strategy))
    key))

(defun find-migration (from-version to-version)
  "Return the MIGRATION record for FROM-VERSION -> TO-VERSION, or NIL."
  (gethash (cons from-version to-version) *migrations*))

(defun check-compatibility (old-version new-version)
  "Stub: static compatibility verdict for OLD-VERSION -> NEW-VERSION.
Compares the endpoint schema versions' COMPATIBLE-WITH claims without
applying anything. Returns (VALUES VERDICT DETAIL) where VERDICT is one
of :COMPATIBLE or :UNKNOWN (no claim recorded either way;
:INCOMPATIBLE is reserved for when removal declarations exist). Both
arguments are SCHEMA-VERSION records or version integers (integers
carry no claims, so they always yield :UNKNOWN)."
  (let ((old (if (schema-version-p old-version)
                 old-version
                 (make-schema-version old-version)))
        (new (if (schema-version-p new-version)
                 new-version
                 (make-schema-version new-version))))
    (cond ((member (schema-version-version new)
                   (schema-version-compatible-with old))
           (values :compatible "new version is claimed compatible by old"))
          ((member (schema-version-version old)
                   (schema-version-compatible-with new))
           (values :compatible "old version is claimed compatible by new"))
          (t (values :unknown "no compatibility claim recorded")))))

(defun apply-migration (from-version to-version)
  "Apply the FROM-VERSION -> TO-VERSION migration. DISABLED: always
signals SCHEMA-EVOLUTION-DISABLED. Present so callers wire the real
entry point and fail loudly instead of hand-rolling state surgery."
  (error 'schema-evolution-disabled
         :migration (list :from from-version :to to-version)))
