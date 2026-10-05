;;;; src/memory/consolidation.lisp — Consolidation (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §32-33. Periodic maintenance merges, aliases,
;;;; deprecates, or retires redundant capabilities through the normal
;;;; rehearsal process, keeping entropy growth sublinear.

(in-package :evo.consolidation)

(defun propose-consolidation ()
  "Propose merge/deprecate/retire actions. Bootstrap: no proposals —
no capability store exists yet."
  nil)

(defun entropy-report ()
  "Return a capability-entropy summary alist. Bootstrap: zero counts."
  (list :capability-count 0 :unused-ratio 0 :duplicate-ratio 0))
