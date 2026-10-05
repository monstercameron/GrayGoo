;;;; tests/lisp/epoch-rollback.lisp — Rollback visibility across epochs.
;;;;
;;;; Issues.md #17: ROLLBACK-TO moves the live pointer instantly under a
;;;; fresh epoch; requests pinned to older epochs keep their behavior and
;;;; no version is ever deleted. Standalone probe: exits 0/1, PASS/FAIL.

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

(defun run-probe ()
  (evo.dispatch:register-version 'foo 1 (lambda () :v1))
  (evo.dispatch:register-version 'foo 2 (lambda () :v2))
  (let ((e1 (evo.dispatch:latest-epoch)))
    (let ((e2 (evo.dispatch:promote-version 'foo 2)))
      (check "promote moves latest to v2"
             (eq (evo.dispatch:invoke-capability 'foo '()) :v2))
      (let ((e3 (evo.dispatch:rollback-to 'foo 1)))
        (check "rollback publishes a fresh epoch" (> e3 e2))
        (check "latest resolves v1 after rollback"
               (eq (evo.dispatch:invoke-capability 'foo '()) :v1))
        (check "current-version pointer reads v1"
               (eql (evo.dispatch:current-version 'foo) 1))
        ;; Older epochs are unaffected by the rollback.
        (evo.dispatch:with-epoch e2
          (check "pinned pre-rollback epoch still resolves v2"
                 (eq (evo.dispatch:invoke-capability 'foo '()) :v2)))
        (evo.dispatch:with-epoch e1
          (check "pinned original epoch still resolves v1"
                 (eq (evo.dispatch:invoke-capability 'foo '()) :v1)))
        ;; Rollback deletes nothing: both versions stay registered and
        ;; re-promotion works.
        (check "versions survive rollback"
               (equal (evo.dispatch:capability-versions 'foo) '(1 2)))
        (let ((e4 (evo.dispatch:promote-version 'foo 2)))
          (check "re-promotion publishes a fresh epoch" (> e4 e3))
          (check "latest resolves v2 after re-promotion"
                 (eq (evo.dispatch:invoke-capability 'foo '()) :v2))
          (evo.dispatch:with-epoch e3
            (check "pinned rollback epoch still resolves v1"
                   (eq (evo.dispatch:invoke-capability 'foo '())
                       :v1))))))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE epoch-rollback: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE epoch-rollback: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (serious-condition (c)
    (format t "PROBE epoch-rollback: FAIL (unexpected error: ~A)~%" c)
    (sb-ext:exit :code 1)))
