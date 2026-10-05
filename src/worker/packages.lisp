;;;; src/worker/packages.lisp — Speculative worker package definition.
;;;;
;;;; Plan reference: plan.md §6 Domain C (speculative workers), §19-20
;;;; (rehearsal workers, prewarmed pool). Workers are disposable SBCL
;;;; processes pinned to one immutable generation; nothing there is canonical.

(defpackage :evo.worker
  (:use :cl)
  (:documentation "Disposable rehearsal worker lifecycle (spawn, pool, recycle).")
  (:export #:worker
           #:make-worker
           #:spawn-worker
           #:terminate-worker
           #:worker-alive-p
           #:run-test-thunk
           #:*pool-min*
           #:*pool-target-max*))
