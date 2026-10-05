;;;; src/kernel/security.lisp — Permission-enforcement surface (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §56 (security model), §58-60 (filesystem,
;;;; network, resource limits). Generated code gets no ambient authority;
;;;; real enforcement (brokered handles, OS sandboxing) arrives later.

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
