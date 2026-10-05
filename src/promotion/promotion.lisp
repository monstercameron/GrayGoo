;;;; src/promotion/promotion.lisp — Promotion authority (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §28, §70 (promotion algorithm), §71-72 (shadow,
;;;; canary). Promotion rejects stale generations, requires hidden-evaluator
;;;; evidence, and publishes a new dispatch epoch. Trusted code only.

(in-package :evo.promotion)

(defvar *promotion-scopes* '(:ephemeral :patch :skill :stable)
  "Promotion scopes in increasing permanence (plan.md §70).")

(defun promotion-scope (scope)
  "Validate SCOPE against known promotion scopes. Returns SCOPE."
  (unless (member scope *promotion-scopes*)
    (error "Unknown promotion scope: ~S." scope))
  scope)

(defstruct (promotion-decision
            (:constructor %make-promotion-decision))
  "Candidate, scope, target epoch, and approval flag."
  (candidate nil) (scope :ephemeral :type symbol)
  (epoch 0 :type integer) (approved-p nil :type boolean))

(defun promote (candidate scope evidence)
  "Decide promotion of CANDIDATE to SCOPE given EVIDENCE. Bootstrap:
signals an error — no evaluator or generation store exists yet."
  (declare (ignore candidate scope evidence))
  (error "Promotion authority not implemented (bootstrap skeleton)."))
