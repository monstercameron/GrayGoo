;;;; src/worker/worker.lisp — Worker lifecycle (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §19-20, §52. Real workers are separate OS
;;;; processes with CPU/memory/time limits; this skeleton defines the worker
;;;; record and pool sizing, and refuses to spawn until the lane lands.

(in-package :evo.worker)

(defvar *pool-min* 4
  "Minimum prewarmed workers (plan.md §20).")

(defvar *pool-target-max* 16
  "Target maximum prewarmed workers (plan.md §20).")

(defstruct (worker
            (:constructor %make-worker))
  "Generation-pinned worker handle. WORKER-ALIVE-P tracks liveness."
  (id 0 :type integer)
  (generation nil)
  (role :compile :type symbol)
  (alive-p nil :type boolean))

(defun make-worker (id &key generation (role :compile))
  "Construct a (not yet spawned) worker handle for ID and GENERATION."
  (%make-worker :id id :generation generation :role role :alive-p nil))

(defun spawn-worker (worker)
  "Spawn WORKER as an isolated process. Bootstrap: signals an error."
  (declare (ignore worker))
  (error "Worker spawning not implemented (bootstrap skeleton)."))

(defun terminate-worker (worker)
  "Terminate WORKER. Bootstrap: marks the handle dead without OS calls."
  (setf (worker-alive-p worker) nil))

;;;; Rehearsal-thunk convention (worker spike; plan.md §19, §21 phase C).
;;;;
;;;; The Python driver (workers.py) loads this file into each fresh SBCL
;;;; child and calls RUN-TEST-THUNK with a zero-argument thunk. The driver
;;;; owns orchestration (timeouts, envelopes, pooling); this side only runs
;;;; the thunk and reports ok/condition/backtrace as values.

(defun %capture-backtrace ()
  "Best-effort backtrace string; never signals."
  (let ((s (make-string-output-stream)))
    (ignore-errors (sb-debug:print-backtrace :stream s :count 24))
    (get-output-stream-string s)))

(defun run-test-thunk (thunk)
  "Call THUNK (a function of zero arguments) and report the outcome.
Returns three values: OK-P, PAYLOAD, BACKTRACE-STRING. On success PAYLOAD
is the PRIN1 representation of the primary value (print length/level
capped); on failure PAYLOAD is the condition report string."
  (check-type thunk function)
  (let ((*print-length* 100)
        (*print-level* 10)
        (*print-readably* nil))
    (handler-case
        (let ((result (funcall thunk)))
          (handler-case
              (values t (prin1-to-string result) "")
            (serious-condition (condition)
              (values nil
                      (format nil "result unprintable: ~A" condition)
                      (%capture-backtrace)))))
      (serious-condition (condition)
        (values nil (princ-to-string condition) (%capture-backtrace))))))
