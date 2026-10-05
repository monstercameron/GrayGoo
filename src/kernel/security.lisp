;;;; src/kernel/security.lisp — Permission vocabulary (NOT a boundary).
;;;;
;;;; Plan reference: plan.md §56 (security model), §58-60 (filesystem,
;;;; network, resource limits). THIS FILE IS NOT A SECURITY BOUNDARY
;;;; (issues.md #47): *RESTRICTED* is an advisory dynamic flag any
;;;; in-image code can rebind, and DENY-BY-DEFAULT only fires when
;;;; called voluntarily. Real enforcement lives in sandbox.py (static
;;;; hard-deny), src/worker/worker.lisp INSTALL-WORKER-SANDBOX
;;;; (in-image clobbering), workers.py (process isolation, sanitized
;;;; env), and risk.py (classification gates) — see
;;;; documents/adversarial-report.md. Do not build checks on this flag.

(in-package :evo.security)

(defvar *restricted* nil
  "True while executing inside a restricted speculative context.")

(defmacro with-restricted-environment (&body body)
  "Execute BODY with ambient authority denied (bootstrap: dynamic flag)."
  `(let ((*restricted* t))
     ,@body))

(defun deny-by-default (operation)
  "Deny OPERATION unless explicitly brokered. Bootstrap: always denies
inside a restricted context, allows otherwise."
  (when *restricted*
    (error "Operation denied in restricted context: ~S." operation))
  t)
