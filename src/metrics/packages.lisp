;;;; src/metrics/packages.lisp — Metrics package definition.
;;;;
;;;; Plan reference: plan.md §64-67 (observability, essential metrics,
;;;; learning score, primary learning criterion). Metrics prove the research
;;;; hypothesis: competence up, model work per task down, entropy sublinear.

(defpackage :evo.metrics
  (:use :cl)
  (:documentation "Task, learning, reliability, and complexity metrics.")
  (:export #:record-metric
           #:metric-value
           #:metric-count
           #:reset-metrics
           #:learning-score))
