;;;; src/model/world.lisp — World-model projections (bootstrap values).
;;;;
;;;; Plan reference: plan.md §42. The LLM receives derived projections
;;;; (capabilities, reliability, failures, epochs), never raw ledger history.
;;;; Values are still stubbed (NIL), but the provenance contract is real
;;;; (issues.md #67): every projection carries derived-through-event,
;;;; state generation, active epoch, and projection time, so async
;;;; consumers can refuse stale mixes. The runtime lane fills values in.

(in-package :evo.world)

(defvar *projection-keys*
  '(:current-capabilities :capability-reliability :recent-failures
    :skill-usefulness :task-clusters :dependency-graph
    :state-generation :active-epoch :mutation-history)
  "Projection keys the world model may expose (plan.md §42).")

(defvar *projection-provenance-keys*
  '(:derived-through-event :state-generation :active-epoch :projected-at)
  "Provenance every projection must carry (issues.md #67):
DERIVED-THROUGH-EVENT is the ledger offset the projection covers,
STATE-GENERATION and ACTIVE-EPOCH pin the consistency boundary, and
PROJECTED-AT is the universal-time of derivation.")

(defun project-world (&optional keys)
  "Project the world model for KEYS (default: all). Bootstrap: returns an
empty projection alist with nil values; the runtime lane fills it in.
Provenance keys are always included so consumers can check freshness."
  (loop for key in (union (or keys *projection-keys*)
                          *projection-provenance-keys*)
        collect (cons key nil)))

(defun check-projection (projection)
  "Validate PROJECTION's provenance contract (issues.md #67). Signals
an error when a provenance key is missing or unbound (NIL); returns
PROJECTION unchanged when every provenance slot is filled. Callers
combining projections must additionally require equal
:DERIVED-THROUGH-EVENT (same ledger offset) — see PROJECTIONS-CONSISTENT-P."
  (dolist (key *projection-provenance-keys* projection)
    (let ((cell (assoc key projection)))
      (unless (and cell (cdr cell))
        (error "Projection lacks provenance ~S (stale or unfilled)."
               key)))))

(defun projections-consistent-p (&rest projections)
  "True when every PROJECTION covers the same ledger offset (and each
carries provenance). NIL when any projection is unprovenanced."
  (ignore-errors
   (let ((offsets (mapcar (lambda (projection)
                            (check-projection projection)
                            (cdr (assoc :derived-through-event projection)))
                          projections)))
     (and offsets (every (lambda (offset) (eql offset (first offsets)))
                         offsets)))))
