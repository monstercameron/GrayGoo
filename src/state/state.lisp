;;;; src/state/state.lisp — State generations and branches (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §24. Canonical events form the trunk; each
;;;; candidate executes against a speculative branch that is discarded on
;;;; rejection and canonically applied on promotion.

(in-package :evo.state)

(defvar *state-generation* 0
  "Current canonical state generation (bootstrap counter).")

(defun state-generation ()
  "Return the current canonical state generation."
  *state-generation*)

(defun current-state-generation ()
  "Alias for STATE-GENERATION (plan.md §42 projection key)."
  (state-generation))

(defmacro with-speculative-branch ((branch-var) &body body)
  "Execute BODY against a speculative branch bound to BRANCH-VAR.
Bootstrap: binds a fresh list; transactional storage arrives later."
  `(let ((,branch-var (list :branch-of *state-generation*)))
     ,@body))
