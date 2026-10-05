;;;; src/kernel/effects.lisp — Effect vocabulary + declaration checking.
;;;;
;;;; Plan reference: plan.md §22 (effect system), §23 (effect
;;;; virtualization). This file validates declaration SHAPE (known
;;;; keywords, :PURE exclusivity). Observed-vs-declared enforcement —
;;;; rejecting operations the candidate did not declare (issues.md
;;;; #48) — lives in the Python broker layer effects.py (EffectGrant,
;;;; OverlayFS, DenyNetwork, StateSandbox), which interposes on every
;;;; operation; nothing in-image can reliably self-report.

(in-package :evo.effects)

(defvar *known-effects*
  '(:pure :state-read :state-write :filesystem-read :filesystem-write
    :network-read :network-write :queue-read :queue-write :email :payment
    :process :clock :randomness :ffi)
  "Effect keywords recognised by the runtime (plan.md §22).")

(defun known-effect-p (effect)
  "Return true when EFFECT is a recognised effect keyword."
  (and (member effect *known-effects*) t))

(defun check-declared-effects (declared)
  "Validate a DECLARED effect list. Signals an error on unknown effects
and on contradictory declarations (:PURE alongside any other effect).
Returns DECLARED unchanged when valid. Shape-only (issues.md #48):
observed-vs-declared enforcement lives in effects.py."
  (dolist (effect declared)
    (unless (known-effect-p effect)
      (error "Unknown declared effect: ~S." effect)))
  (when (and (member :pure declared)
             (> (length (remove-duplicates declared)) 1))
    (error "Contradictory effects: :PURE must be the sole declared effect."))
  declared)
