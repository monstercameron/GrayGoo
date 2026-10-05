;;;; src/kernel/packages.lisp — Trusted-kernel package definitions (plan.md §6, Domain A).
;;;;
;;;; The kernel is non-self-modifiable (plan.md §4.3). These packages own
;;;; versioned dispatch, the event ledger, effect declarations, and the
;;;; permission-enforcement API surface.

(defpackage :evo.kernel
  (:use :cl)
  (:documentation "Trusted kernel: runtime identity, generation fingerprints, epoch plumbing.")
  (:export #:*runtime-version*
           #:runtime-version
           #:generation-fingerprint
           #:make-generation-fingerprint
           #:current-generation))

(defpackage :evo.dispatch
  (:use :cl)
  (:documentation "Versioned capability dispatch cells and request epochs (plan.md §26-27).")
  (:export #:invoke-capability
           #:register-version
           #:current-version
           #:capability-versions
           #:promote-version
           #:rollback-to
           #:with-epoch
           #:current-epoch
           #:latest-epoch
           #:epoch-pinned-p
           #:drain-epoch))

(defpackage :evo.events
  (:use :cl)
  (:documentation "Append-only canonical event ledger (plan.md §41).")
  (:export #:log-event
           #:event-count
           #:events-since
           #:clear-ledger))

(defpackage :evo.effects
  (:use :cl)
  (:documentation "Effect declarations and static effect categories (plan.md §22).")
  (:export #:*known-effects*
           #:known-effect-p
           #:check-declared-effects))

(defpackage :evo.security
  (:use :cl)
  (:documentation "Permission enforcement and ambient-authority denial (plan.md §56-60).")
  (:export #:with-restricted-environment
           #:deny-by-default
           #:*restricted*))
