;;;; src/model/world.lisp — World-model projections (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §42. The LLM receives derived projections
;;;; (capabilities, reliability, failures, epochs), never raw ledger history.

(in-package :evo.world)

(defvar *projection-keys*
  '(:current-capabilities :capability-reliability :recent-failures
    :skill-usefulness :task-clusters :dependency-graph
    :state-generation :active-epoch :mutation-history)
  "Projection keys the world model may expose (plan.md §42).")

(defun project-world (&optional keys)
  "Project the world model for KEYS (default: all). Bootstrap: returns an
empty projection alist with nil values; the runtime lane fills it in."
  (loop for key in (or keys *projection-keys*)
        collect (cons key nil)))
