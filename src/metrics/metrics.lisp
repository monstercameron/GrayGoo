;;;; src/metrics/metrics.lisp — In-memory metrics (bootstrap).
;;;;
;;;; Plan reference: plan.md §64-67. Counters and gauges with the research
;;;; score from §66: (held-out × transfer) / (cost × latency × regressions).
;;;; Durable metric storage arrives with the persistence lane.

(in-package :evo.metrics)

(defvar *metrics* (make-hash-table :test 'equal)
  "Metric name -> numeric value.")

(defun record-metric (name value)
  "Record VALUE for metric NAME (overwrites). Returns VALUE."
  (setf (gethash name *metrics*) value))

(defun metric-value (name &optional (default 0))
  "Return the value of metric NAME, or DEFAULT when unrecorded."
  (gethash name *metrics* default))

(defun metric-count ()
  "Return the number of recorded metrics."
  (hash-table-count *metrics*))

(defun reset-metrics ()
  "Clear all metrics. Test/bootstrap use only."
  (clrhash *metrics*)
  0)

(defun learning-score (&key held-out-success transfer-success cost latency
                         regression-penalty)
  "Compute the research utility score (plan.md §66):
\(held-out × transfer) / (cost × latency × regressions).
Signals an error on non-positive denominators."
  (let ((denominator (* cost latency regression-penalty)))
    (unless (plusp denominator)
      (error "Learning score needs positive cost, latency, and penalty."))
    (/ (* held-out-success transfer-success) denominator)))
