;;;; tests/lisp/epoch-drain-refusal.lisp — Drain-while-pinned refusal.
;;;;
;;;; Issues.md #17: DRAIN-EPOCH must refuse the latest epoch and any epoch
;;;; with pinned requests, and succeed once requests drain. Standalone
;;;; probe: loads sources by path, prints PASS/FAIL markers, exits 0/1.

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

(defmacro check-error (name &body body)
  `(handler-case (progn ,@body
                        (incf *failures*)
                        (format t "  FAIL (no error): ~A~%" ,name))
     (error (c)
       (declare (ignore c))
       (format t "  ok: ~A~%" ,name))))

(defun run-probe ()
  (evo.dispatch:register-version 'foo 1 (lambda () :v1))
  (evo.dispatch:register-version 'foo 2 (lambda () :v2))
  (let ((e1 (evo.dispatch:latest-epoch)))
    (let ((e2 (evo.dispatch:promote-version 'foo 2)))
      (check "promotion publishes a fresh epoch" (> e2 e1))
      ;; Latest epoch can never drain.
      (check-error "drain refuses the latest epoch"
        (evo.dispatch:drain-epoch e2))
      (check-error "drain refuses an unknown epoch"
        (evo.dispatch:drain-epoch 9999))
      ;; Pinned epoch cannot drain; the refusal must not disturb routing.
      (evo.dispatch:with-epoch e1
        (check "e1 reports pinned inside with-epoch"
               (evo.dispatch:epoch-pinned-p e1))
        (check-error "drain refuses a pinned epoch"
          (evo.dispatch:drain-epoch e1))
        (check "refused drain leaves e1 routing intact"
               (eq (evo.dispatch:invoke-capability 'foo '()) :v1)))
      (check "e1 reports unpinned after with-epoch"
             (not (evo.dispatch:epoch-pinned-p e1)))
      ;; Once drained, the snapshot is gone but versions live on.
      (check "drain returns the epoch"
             (eql (evo.dispatch:drain-epoch e1) e1))
      (check-error "invoke under a drained epoch signals"
        (evo.dispatch:invoke-capability 'foo '() :epoch e1))
      (check "drain twice signals (already drained)"
             (handler-case (progn (evo.dispatch:drain-epoch e1) nil)
               (error (c) (declare (ignore c)) t)))
      (check "versions survive the drain"
             (equal (evo.dispatch:capability-versions 'foo) '(1 2)))
      (check "latest still resolves v2 after the drain"
             (eq (evo.dispatch:invoke-capability 'foo '()) :v2)))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE epoch-drain-refusal: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE epoch-drain-refusal: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (serious-condition (c)
    (format t "PROBE epoch-drain-refusal: FAIL (unexpected error: ~A)~%" c)
    (sb-ext:exit :code 1)))
