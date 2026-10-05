;;;; src/state/recovery.lisp — Boot recovery (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §44 (recovery), §45 (checkpointing). Recovery
;;;; starts the trusted kernel, loads the stable manifest, verifies hashes,
;;;; and only then starts the application — never from mutable state.

(in-package :evo.recovery)

(defun verify-manifest (manifest)
  "Verify a stable generation MANIFEST. Bootstrap: accepts any
non-null manifest; hash verification arrives with persistence."
  (not (null manifest)))

(defun recover (&optional manifest)
  "Recover the runtime from MANIFEST (default: stable). Bootstrap:
signals an error — no manifest store exists yet."
  (declare (ignore manifest))
  (error "Recovery not implemented (bootstrap skeleton)."))
