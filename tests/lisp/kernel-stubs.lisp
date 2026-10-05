;;;; tests/lisp/kernel-stubs.lisp — Kernel honesty boundaries.
;;;;
;;;; Issues.md #47 (restricted flag is advisory, not a boundary) and
;;;; #48 (declaration shape validation; observed-vs-declared lives in
;;;; effects.py). The #47 checks pin the KNOWN LIMITATION so nobody
;;;; mistakes the flag for protection. Standalone probe: exits 0/1.

(defparameter *probe-dir*
  (make-pathname :defaults (or *load-truename* #P"./")
                 :name nil :type nil :version nil))
(defparameter *repo-root* (truename (merge-pathnames #P"../../" *probe-dir*)))

(let ((*load-verbose* nil) (*load-print* nil)
      (*compile-verbose* nil) (*compile-print* nil))
  (load (merge-pathnames #P"src/kernel/packages.lisp" *repo-root*))
  (load (merge-pathnames #P"src/kernel/effects.lisp" *repo-root*))
  (load (merge-pathnames #P"src/kernel/security.lisp" *repo-root*)))

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
  ;; #48: declaration shape validation.
  (check "pure alone accepted"
         (equal '(:pure)
                (evo.effects:check-declared-effects '(:pure))))
  (check "non-pure sets accepted"
         (equal '(:filesystem-read :network-read)
                (evo.effects:check-declared-effects
                 '(:filesystem-read :network-read))))
  (check-error "unknown effect signals"
    (evo.effects:check-declared-effects '(:teleport)))
  (check-error "pure alongside others signals"
    (evo.effects:check-declared-effects '(:pure :filesystem-write)))
  ;; #47: the flag is advisory — pin the limitation, do not rely on it.
  (check "deny-by-default fires inside the restricted form"
         (handler-case
             (evo.security:with-restricted-environment
               (evo.security:deny-by-default :filesystem-write)
               nil)
           (error (c)
             (declare (ignore c))
             t)))
  (check "flag is advisory: rebinding defeats it (known limitation)"
         (evo.security:with-restricted-environment
           (setf evo.security:*restricted* nil)
           (evo.security:deny-by-default :filesystem-write))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE kernel-stubs: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE kernel-stubs: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (error (c)
    (format t "PROBE kernel-stubs: FAIL (toplevel ~A)~%" c)
    (sb-ext:exit :code 1)))
