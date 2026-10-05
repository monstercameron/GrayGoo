;;;; src/promotion/policy.lisp — Agent-policy generation (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §39-40. Meta-improvement changes one policy
;;;; variable at a time, validated by offline replay and paired benchmarks.
;;;; MVP excludes policy self-modification (plan.md §79).

(in-package :evo.policy)

(defvar *policy-generation* 0
  "Current agent-policy generation (bootstrap counter).")

(defun policy-generation ()
  "Return the current agent-policy generation."
  *policy-generation*)

(defun current-policy-generation ()
  "Alias for POLICY-GENERATION."
  (policy-generation))

(defun propose-policy-change (variable value)
  "Propose a one-variable policy change. Bootstrap: signals an error —
meta-improvement is post-MVP (plan.md §79)."
  (declare (ignore variable value))
  (error "Policy evolution not implemented (bootstrap skeleton)."))
