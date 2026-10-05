;;;; src/kernel/effects.lisp — Effect categories and declaration checking.
;;;;
;;;; Plan reference: plan.md §22 (effect system), §23 (effect
;;;; virtualization). Generated code must declare its effects; rehearsal
;;;; virtualizes every non-pure effect.

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
  "Validate a DECLARED effect list. Signals an error on unknown effects.
Returns DECLARED unchanged when valid."
  (dolist (effect declared declared)
    (unless (known-effect-p effect)
      (error "Unknown declared effect: ~S." effect))))
