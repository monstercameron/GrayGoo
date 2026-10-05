;;;; tests/lisp/capability-identity.lisp — Stable UUID identity + display names.
;;;;
;;;; Issues.md #15: capabilities need a package-independent stable string
;;;; identity plus a symbolic display name. Standalone probe: exits 0/1.

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
    (merge-pathnames "graygoo-cap-probe/"
                     (parse-namestring (concatenate 'string base "/")))))

(defun run-probe ()
  ;; UUID minting and validation.
  (let ((a (evo.capability:make-stable-id))
        (b (evo.capability:make-stable-id)))
    (check "minted ids validate" (and (evo.capability:stable-id-p a)
                                      (evo.capability:stable-id-p b)))
    (check "minted ids are distinct" (not (string= a b)))
    (check "uuid version nibble is 4" (char= (char a 14) #\4)))
  (check "non-strings rejected" (not (evo.capability:stable-id-p 'foo)))
  (check "wrong-shape strings rejected"
         (notany #'evo.capability:stable-id-p
                 '("" "cap-123" "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"
                   "0123456789abcdef0123456789abcdef")))
  ;; Constructor defaults (additive: old call shapes still work).
  (let ((cap (evo.capability:make-capability 'probe.alpha :version 1)))
    (check "default stable-id is a valid uuid"
           (evo.capability:stable-id-p
            (evo.capability:capability-stable-id cap)))
    (check "default display-name is the symbol name"
           (string= (evo.capability:capability-display-name cap)
                    "PROBE.ALPHA")))
  ;; Explicit identity honored and validated.
  (let ((sid (evo.capability:make-stable-id)))
    (let ((cap (evo.capability:make-capability 'probe.beta :version 1
                                               :stable-id sid
                                               :display-name "Beta Cap")))
      (check "explicit stable-id kept"
             (string= (evo.capability:capability-stable-id cap) sid))
      (check "explicit display-name kept"
             (string= (evo.capability:capability-display-name cap)
                      "Beta Cap"))))
  (check-error "malformed stable-id signals"
    (evo.capability:make-capability 'probe.bad :stable-id "nope"))
  (check-error "non-string display-name signals"
    (evo.capability:make-capability 'probe.bad :display-name 'nope))
  ;; Derivation preserves family identity.
  (let* ((parent (evo.capability:make-capability 'probe.fam :version 1))
         (child (evo.capability:derive-version parent)))
    (check "derived version inherits stable-id"
           (string= (evo.capability:capability-stable-id child)
                    (evo.capability:capability-stable-id parent)))
    (check "derived version inherits display-name"
           (string= (evo.capability:capability-display-name child)
                    (evo.capability:capability-display-name parent)))
    (let ((renamed (evo.capability:derive-version
                    parent :display-name "Renamed")))
      (check "display-name override honored"
             (string= (evo.capability:capability-display-name renamed)
                      "Renamed"))
      (check "override keeps the stable-id"
             (string= (evo.capability:capability-stable-id renamed)
                      (evo.capability:capability-stable-id parent)))))
  ;; Registry round-trip through a temp dir (cleaned up afterwards).
  (let ((dir (probe-temp-dir)))
    (evo.registry:ensure-data-dir dir)
    (unwind-protect
         (let* ((cap (evo.capability:make-capability 'probe.persist
                                                     :version 3))
                (sid (evo.capability:capability-stable-id cap)))
           (evo.registry:register-capability-version cap)
           (check "find-by-stable-id locates the version"
                  (equal (evo.registry:find-by-stable-id sid) '(3)))
           (check "stable-id pathname embeds the uuid"
                  (search sid (namestring
                               (evo.registry:stable-id-pathname sid 3 dir))))
           (let ((path (evo.registry:save-capability cap dir)))
             (evo.registry:clear-registry)
             (check "registry cleared" (zerop (evo.registry:registry-count)))
             (let ((back (evo.registry:load-capability 'probe.persist 3 dir)))
               (check "reload preserves stable-id"
                      (string= (evo.capability:capability-stable-id back)
                               sid))
               (check "reload preserves display-name"
                      (string= (evo.capability:capability-display-name back)
                               "PROBE.PERSIST")))
             (ignore-errors (delete-file path)))
           (check-error "find-by-stable-id rejects malformed ids"
             (evo.registry:find-by-stable-id "bogus")))
      (ignore-errors
        (dolist (left (directory (merge-pathnames "*.lisp" dir)))
          (delete-file left))))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE capability-identity: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE capability-identity: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (serious-condition (c)
    (format t "PROBE capability-identity: FAIL (unexpected error: ~A)~%" c)
    (sb-ext:exit :code 1)))
