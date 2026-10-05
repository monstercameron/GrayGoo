;;;; tests/lisp/dispatch-concurrency.lisp — Control-plane lock smoke test.
;;;;
;;;; Issues.md #16/#17: concurrent register/promote/invoke/pin traffic must
;;;; not corrupt dispatch state or lose pin counts. Small and fast (4
;;;; threads x 60 iterations). Standalone probe: exits 0/1, PASS/FAIL.

(defparameter *probe-dir*
  (make-pathname :defaults (or *load-truename* #P"./")
                 :name nil :type nil :version nil))
(defparameter *repo-root* (truename (merge-pathnames #P"../../" *probe-dir*)))

(let ((*load-verbose* nil) (*load-print* nil)
      (*compile-verbose* nil) (*compile-print* nil))
  (load (merge-pathnames #P"src/kernel/packages.lisp" *repo-root*))
  (load (merge-pathnames #P"src/kernel/dispatch.lisp" *repo-root*)))

(defvar *failures* 0)

(defmacro check (name test)
  `(if ,test
       (format t "  ok: ~A~%" ,name)
       (progn (incf *failures*)
              (format t "  FAIL: ~A~%" ,name))))

(defparameter *worker-count* 4)
(defparameter *iterations* 60)

(defun worker-body (n)
  "One thread's traffic: its own capability plus shared pin churn.
Returns :OK, or the first error encountered (errors are returned, not
signalled, so the main thread reports them as check failures)."
  (handler-case
      (let ((name (intern (format nil "W~D" n) :cl-user)))
        (evo.dispatch:register-version name 1 (lambda () (list :w n 1)))
        (evo.dispatch:register-version name 2 (lambda () (list :w n 2)))
        (loop for i below *iterations*
              do (evo.dispatch:promote-version name (if (oddp i) 1 2))
                 (evo.dispatch:invoke-capability name '())
                 (evo.dispatch:with-epoch (evo.dispatch:latest-epoch)
                   (evo.dispatch:invoke-capability name '())))
        :ok)
    (serious-condition (c) c)))

(defun run-probe ()
  (check "dispatch lock is a mutex"
         (typep evo.dispatch::*dispatch-lock* 'sb-thread:mutex))
  ;; LOOP reuses one binding for N across iterations, so each closure
  ;; captures a fresh copy — otherwise every thread would run as W4.
  (let ((threads (loop for n below *worker-count*
                       collect (let ((k n))
                                 (sb-thread:make-thread
                                  (lambda () (worker-body k))
                                  :name (format nil "dispatch-probe-~D"
                                                k))))))
    (let ((results (mapcar #'sb-thread:join-thread threads)))
      (check "all worker threads returned :OK"
             (every (lambda (r) (eq r :ok)) results))
      (loop for r in results
            for n below *worker-count*
            unless (eq r :ok)
            do (format t "  thread ~D error: ~A~%" n r))))
  ;; Post-traffic consistency: every worker capability resolves, pins
  ;; drained to zero, version lists intact.
  (loop for n below *worker-count*
        for name = (intern (format nil "W~D" n) :cl-user)
        do (check (format nil "~A resolves to a tagged value" name)
                  (equal (butlast (evo.dispatch:invoke-capability name '()) 1)
                         (list :w n)))
           (check (format nil "~A keeps both versions" name)
                  (equal (evo.dispatch:capability-versions name) '(1 2))))
  ;; %UNPIN-EPOCH zeroes counts but keeps entries (only DRAIN-EPOCH
  ;; remhashes), so assert all-zero values rather than an empty table.
  (check "all pin counts return to zero after all threads join"
         (loop for count being the hash-values
               of evo.dispatch::*epoch-pins*
               always (zerop count))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE dispatch-concurrency: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE dispatch-concurrency: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (serious-condition (c)
    (format t "PROBE dispatch-concurrency: FAIL (unexpected error: ~A)~%" c)
    (sb-ext:exit :code 1)))
