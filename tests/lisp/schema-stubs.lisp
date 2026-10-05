;;;; tests/lisp/schema-stubs.lisp — Migration design + disabled evolution.
;;;;
;;;; Issues.md #18: versioned-migration design records and
;;;; compatibility-check stubs must exist, and applying a migration must
;;;; refuse while SCHEMA-EVOLUTION-DISABLED is true. Standalone probe.

(defparameter *probe-dir*
  (make-pathname :defaults (or *load-truename* #P"./")
                 :name nil :type nil :version nil))
(defparameter *repo-root* (truename (merge-pathnames #P"../../" *probe-dir*)))

(let ((*load-verbose* nil) (*load-print* nil)
      (*compile-verbose* nil) (*compile-print* nil))
  (load (merge-pathnames #P"src/state/packages.lisp" *repo-root*))
  (load (merge-pathnames #P"src/state/state.lisp" *repo-root*))
  (load (merge-pathnames #P"src/state/schema.lisp" *repo-root*))
  (load (merge-pathnames #P"src/state/recovery.lisp" *repo-root*)))

(defvar *failures* 0)

(defmacro check (name test)
  `(if ,test
       (format t "  ok: ~A~%" ,name)
       (progn (incf *failures*)
              (format t "  FAIL: ~A~%" ,name))))

(defmacro check-error (name type &body body)
  `(handler-case (progn ,@body
                        (incf *failures*)
                        (format t "  FAIL (no error): ~A~%" ,name))
     (,type (c)
       (declare (ignore c))
       (format t "  ok: ~A~%" ,name))))

(defun run-probe ()
  ;; The flag exists and evolution is off.
  (check "SCHEMA-EVOLUTION-DISABLED is true"
         evo.schema:schema-evolution-disabled)
  (check-error "apply-migration refuses while disabled"
      evo.schema:schema-evolution-disabled
    (evo.schema:apply-migration 0 1))
  ;; Migration design records are inert data.
  (let ((key (evo.schema:define-migration 0 1 :compatibility "additive")))
    (check "define-migration returns the version pair"
           (equal key '(0 . 1)))
    (let ((m (evo.schema:find-migration 0 1)))
      (check "find-migration retrieves the record"
             (and (evo.schema:migration-p m)
                  (eql (evo.schema:migration-from-version m) 0)
                  (eql (evo.schema:migration-to-version m) 1)
                  (string= (evo.schema:migration-compatibility m)
                           "additive"))))
    (check "unknown migration lookup returns NIL"
           (null (evo.schema:find-migration 1 2))))
  (check-error "duplicate migration definition signals" error
    (evo.schema:define-migration 0 1))
  ;; Compatibility-check stub: claims without application.
  (let ((old (evo.schema:make-schema-version 0 :compatible-with '(1)))
        (new (evo.schema:make-schema-version 1)))
    (check "claimed compatibility yields :COMPATIBLE"
           (eq (evo.schema:check-compatibility old new) :compatible)))
  (check "bare integers yield :UNKNOWN"
         (eq (evo.schema:check-compatibility 0 1) :unknown))
  ;; Pre-existing surface still works (additive change).
  (check "current-schema-version still answers"
         (evo.schema:schema-version-p
          (evo.schema:current-schema-version)))
  (check "state generation accessor intact"
         (integerp (evo.state:state-generation))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE schema-stubs: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE schema-stubs: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (serious-condition (c)
    (format t "PROBE schema-stubs: FAIL (unexpected error: ~A)~%" c)
    (sb-ext:exit :code 1)))
