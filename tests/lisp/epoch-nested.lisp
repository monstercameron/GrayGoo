;;;; tests/lisp/epoch-nested.lisp — Nested capability calls across pinned epochs.
;;;;
;;;; Issues.md #17: mixed-version workflows must resolve every nested call
;;;; through the requesting epoch. Each probe is standalone: it loads the
;;;; accused sources by path, runs its checks, prints PASS/FAIL markers,
;;;; and exits 0 (all green) or 1 (any failure). See README.md.

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
  (let ((e1-saved nil))
    (declare (ignore e1-saved))
    ;; FOO v1/v2 return their own version tag; BAR nests a FOO call and
    ;; must inherit the caller's epoch (no explicit :epoch passed).
    (evo.dispatch:register-version 'foo 1 (lambda () :v1))
    (evo.dispatch:register-version 'foo 2 (lambda () :v2))
    (evo.dispatch:register-version 'bar 1
      (lambda () (list :bar (evo.dispatch:invoke-capability 'foo '()))))
    ;; BAZ nests two levels (BAZ -> BAR -> FOO). Registered before the
    ;; promotion so epoch e1 routes it; registration only adds first
    ;; versions to the latest snapshot.
    (evo.dispatch:register-version 'baz 1
      (lambda () (list :baz (evo.dispatch:invoke-capability 'bar '()))))
    (let ((e1 (evo.dispatch:latest-epoch)))
      (check "initial epoch is 1" (eql e1 1))
      (check "latest resolves foo v1 before promotion"
             (eq (evo.dispatch:invoke-capability 'foo '()) :v1))
      (check "nested call resolves foo v1 before promotion"
             (equal (evo.dispatch:invoke-capability 'bar '()) '(:bar :v1)))
      (let ((e2 (evo.dispatch:promote-version 'foo 2)))
        (check "promotion publishes a fresh epoch" (> e2 e1))
        (check "latest resolves foo v2 after promotion"
               (eq (evo.dispatch:invoke-capability 'foo '()) :v2))
        (check "nested call resolves foo v2 under latest"
               (equal (evo.dispatch:invoke-capability 'bar '())
                      '(:bar :v2)))
        ;; Pinned requests keep old behavior, including nested calls.
        (evo.dispatch:with-epoch e1
          (check "pinned e1 still resolves foo v1"
                 (eq (evo.dispatch:invoke-capability 'foo '()) :v1))
          (check "pinned e1 nested call still resolves foo v1"
                 (equal (evo.dispatch:invoke-capability 'bar '())
                        '(:bar :v1)))
          (check "current-epoch reports the pinned epoch"
                 (eql (evo.dispatch:current-epoch) e1)))
        ;; Explicit :epoch overrides the ambient epoch.
        (check "explicit :epoch e1 resolves foo v1 under latest"
               (eq (evo.dispatch:invoke-capability 'foo '() :epoch e1) :v1))
        (check "explicit :epoch e2 resolves foo v2"
               (eq (evo.dispatch:invoke-capability 'foo '() :epoch e2) :v2))
        ;; Double nesting: BAZ -> BAR -> FOO, all epoch-pinned.
        (evo.dispatch:with-epoch e1
          (check "double-nested call under e1 resolves v1 chain"
                 (equal (evo.dispatch:invoke-capability 'baz '())
                        '(:baz (:bar :v1)))))
        (check "double-nested call under latest resolves v2 chain"
               (equal (evo.dispatch:invoke-capability 'baz '())
                      '(:baz (:bar :v2))))))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE epoch-nested: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE epoch-nested: FAIL (~D check(s))~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (serious-condition (c)
    (format t "PROBE epoch-nested: FAIL (unexpected error: ~A)~%" c)
    (sb-ext:exit :code 1)))
