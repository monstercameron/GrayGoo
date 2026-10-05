;;;; tests/lisp/persistence-integrity.lisp — Registry persistence guarantees.
;;;;
;;;; Issues.md #42 (cross-package filename collision), #43 (write-once
;;;; versions), #44 (atomic temp+rename writes), #45 (integrity hash).
;;;; Standalone probe: exits 0/1.

(defparameter *probe-dir*
  (make-pathname :defaults (or *load-truename* #P"./")
                 :name nil :type nil :version nil))
(defparameter *repo-root* (truename (merge-pathnames #P"../../" *probe-dir*)))

(let ((*load-verbose* nil) (*load-print* nil)
      (*compile-verbose* nil) (*compile-print* nil))
  (load (merge-pathnames #P"src/capability/packages.lisp" *repo-root*))
  (load (merge-pathnames #P"src/capability/capability.lisp" *repo-root*))
  (load (merge-pathnames #P"src/capability/contract.lisp" *repo-root*))
  (load (merge-pathnames #P"src/capability/intent.lisp" *repo-root*))
  (load (merge-pathnames #P"src/capability/registry.lisp" *repo-root*)))

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

(defun probe-temp-dir ()
  (let ((base (or (sb-ext:posix-getenv "TEMP")
                  (sb-ext:posix-getenv "TMP")
                  "/tmp")))
    (merge-pathnames "graygoo-persist-probe/"
                     (parse-namestring (concatenate 'string base "/")))))

(defun read-whole-file (path)
  (with-open-file (in path :direction :input :external-format :utf-8)
    (let ((out (make-string-output-stream)))
      (loop for ch = (read-char in nil nil)
            while ch do (write-char ch out))
      (get-output-stream-string out))))

(defun run-probe ()
  (defpackage :persist-a (:use :cl))
  (defpackage :persist-b (:use :cl))
  (let ((dir (probe-temp-dir)))
    (evo.registry:ensure-data-dir dir)
    (unwind-protect
         (progn
           ;; #42: same symbol name in two packages -> distinct files.
           (let* ((a (evo.capability:make-capability
                      (intern "WIDGET" :persist-a) :version 1))
                  (b (evo.capability:make-capability
                      (intern "WIDGET" :persist-b) :version 1))
                  (pa (evo.registry:save-capability a dir))
                  (pb (evo.registry:save-capability b dir)))
             (check "cross-package versions persist to distinct files"
                    (not (equal pa pb))))
           ;; #43: identical re-save is idempotent.
           (let* ((cap (evo.capability:make-capability 'probe.same
                                                       :version 1))
                  (p1 (evo.registry:save-capability cap dir))
                  (p2 (evo.registry:save-capability cap dir)))
             (check "identical re-save returns the same path"
                    (equal p1 p2)))
           ;; #43: differing overwrite signals.
           (let ((v1 (evo.capability:make-capability 'probe.clash
                                                     :version 1 :ttl 10))
                 (v2 (evo.capability:make-capability 'probe.clash
                                                     :version 1 :ttl 20)))
             (evo.registry:save-capability v1 dir)
             (check-error "differing overwrite of a version signals"
               (evo.registry:save-capability v2 dir)))
           ;; #45: round-trip verifies silently; tamper signals mismatch.
           (let* ((cap (evo.capability:make-capability 'probe.sealed
                                                       :version 2))
                  (path (evo.registry:save-capability cap dir)))
             (evo.registry:clear-registry)
             (let ((back (evo.registry:load-capability 'probe.sealed 2
                                                       dir)))
               (check "v2 round-trip preserves the ttl"
                      (eql (evo.capability:capability-ttl back)
                           (evo.capability:capability-ttl cap))))
             (let ((tampered (read-whole-file path)))
               (setf tampered (concatenate
                               'string
                               (subseq tampered 0
                                       (search ":TTL" tampered))
                               ":TTX"
                               (subseq tampered
                                       (+ (search ":TTL" tampered) 4))))
               (with-open-file (out path :direction :output
                                         :if-exists :supersede
                                         :external-format :utf-8)
                 (write-string tampered out)))
             (evo.registry:clear-registry)
             (handler-case
                 (progn
                   (evo.registry:load-capability 'probe.sealed 2 dir)
                   (incf *failures*)
                   (format t "  FAIL (no error): tampered file loads~%"))
               (error (c)
                 (check "tampered file signals hash mismatch"
                        (search "hash mismatch"
                                (princ-to-string c)))))
             ;; Remove the poisoned file so later dir-wide checks stay clean.
             (ignore-errors (delete-file path)))
           ;; #44: temp files use a non-lisp type; none strand after save.
           (check "no .tmp files strand after saves"
                  (null (directory (merge-pathnames "*.tmp" dir))))
           (let ((decoy (merge-pathnames "stray-decoy.tmp" dir)))
             (with-open-file (out decoy :direction :output
                                       :if-exists :supersede)
               (write-string "not a capability" out))
             (evo.registry:clear-registry)
             (let ((loaded (evo.registry:load-registry dir)))
               (check "load-registry ignores .tmp decoys"
                      (every #'evo.capability:capability-p loaded)))
             (ignore-errors (delete-file decoy))))
      (ignore-errors
        (dolist (pattern '("*.lisp" "*.tmp"))
          (dolist (left (directory (merge-pathnames pattern dir)))
            (delete-file left)))))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE persistence-integrity: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE persistence-integrity: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (error (c)
    (format t "PROBE persistence-integrity: FAIL (toplevel ~A)~%" c)
    (sb-ext:exit :code 1)))
