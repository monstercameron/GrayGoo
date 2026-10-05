;;;; src/model/context.lisp — Minimal context compiler (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §14 (context compiler), §4.6 (minimize model
;;;; context). The compiler selects goal, relevant capabilities, contracts,
;;;; applicable failures, allowed effects, and schema generation — nothing more.

(in-package :evo.context)

(defvar *context-token-budget* 2500
  "Maximum input tokens for a synthesis context (plan.md §48).")

(defun compile-context (goal &key capabilities contracts failures effects)
  "Compile a minimal model-facing context plist for GOAL. Bootstrap:
assembles the documented keys without retrieval or ranking."
  (list :goal goal
        :capabilities capabilities
        :contracts contracts
        :failures failures
        :allowed-effects effects))
