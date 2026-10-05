;;;; tests/lisp/capability-model.lisp — Constructor validation + derivation.
;;;;
;;;; Issues.md #49 (derive cannot clear list fields), #50 (TTL cannot
;;;; be removed), #51 (risk values unvalidated), #52 (lifecycle /
;;;; promotion contradiction), #53 (unvalidated parent lineage), #54
;;;; (version collision under concurrency). Standalone probe: exits 0/1.

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

(defun run-probe ()
  ;; #51: risk validated at construction.
  (check "all seven risk levels accepted"
         (every (lambda (level)
                  (evo.capability:capability-p
                   (evo.capability:make-capability 'probe.risk
                                                   :risk level)))
                evo.capability:*capability-risk-levels*))
  (check "risk-level-p classifies"
         (and (evo.capability:risk-level-p :r3)
              (not (evo.capability:risk-level-p :bogus))
              (not (evo.capability:risk-level-p nil))))
  (check-error "bogus risk signals at construction"
    (evo.capability:make-capability 'probe.risk :risk :bogus))
  (check-error "bogus risk signals on derive"
    (let ((parent (evo.capability:make-capability 'probe.risk2)))
      (evo.capability:derive-version parent :risk :bogus)))
  ;; #50: TTL removable via explicit NIL, inherited when omitted.
  (let* ((parent (evo.capability:make-capability 'probe.ttl :ttl 100))
         (cleared (evo.capability:derive-version parent :ttl nil))
         (kept (evo.capability:derive-version parent)))
    (check "explicit :ttl nil removes the TTL"
           (null (evo.capability:capability-ttl cleared)))
    (check "omitted :ttl inherits"
           (eql 100 (evo.capability:capability-ttl kept))))
  ;; #49: explicit NIL clears list (and source) fields.
  (let* ((parent (evo.capability:make-capability
                  'probe.clear
                  :inputs '(a) :outputs '(b) :effects '(c)
                  :dependencies '(d) :source "s" :creator "u"
                  :model "m"))
         (child (evo.capability:derive-version
                 parent :inputs nil :outputs nil :effects nil
                 :dependencies nil :source nil :creator nil
                 :model nil))
         (kept (evo.capability:derive-version parent)))
    (check "explicit nil clears inputs" (null (evo.capability:capability-inputs child)))
    (check "explicit nil clears outputs" (null (evo.capability:capability-outputs child)))
    (check "explicit nil clears effects" (null (evo.capability:capability-effects child)))
    (check "explicit nil clears dependencies"
           (null (evo.capability:capability-dependencies child)))
    (check "explicit nil clears source" (null (evo.capability:capability-source child)))
    (check "explicit nil clears creator" (null (evo.capability:capability-creator child)))
    (check "explicit nil clears model" (null (evo.capability:capability-model child)))
    (check "omitted keys still inherit"
           (and (equal '(a) (evo.capability:capability-inputs kept))
                (equal '(b) (evo.capability:capability-outputs kept))
                (equal '(c) (evo.capability:capability-effects kept))
                (equal '(d) (evo.capability:capability-dependencies kept))
                (string= "s" (evo.capability:capability-source kept))
                (string= "u" (evo.capability:capability-creator kept))
                (string= "m" (evo.capability:capability-model kept))))))

(defun probe-temp-dir ()
  (let ((base (or (sb-ext:posix-getenv "TEMP")
                  (sb-ext:posix-getenv "TMP")
                  "/tmp")))
    (merge-pathnames "graygoo-lineage-probe/"
                     (parse-namestring (concatenate 'string base "/")))))

(defun run-lineage-probe ()
  ;; #52: promotion-status must not lag state in lifecycle order.
  (check-error "lagging promotion-status signals"
    (evo.capability:make-capability 'probe.life :state :stable
                                    :promotion-status :proposed))
  (check "leading promotion-status accepted"
         (evo.capability:capability-p
          (evo.capability:make-capability 'probe.life :state :proposed
                                          :promotion-status :stable)))
  (evo.registry:clear-registry)
  ;; #53: claimed parents must exist and precede the child.
  (check-error "orphan parent signals"
    (evo.registry:register-capability 'probe.orph 2 :rec
                                      :parent-version 1))
  (check-error "self-parent signals"
    (evo.registry:register-capability 'probe.orph 2 :rec
                                      :parent-version 2))
  (evo.registry:register-capability 'probe.chain 1 :rec1)
  (evo.registry:register-capability 'probe.chain 2 :rec2
                                    :parent-version 1)
  (check "valid parent chain registers"
         (equal (evo.registry:capability-versions 'probe.chain) '(1 2)))
  ;; #54: atomic next-version assignment, no collisions.
  (check "next-version is max+1"
         (eql 3 (evo.registry:next-version 'probe.chain)))
  (check "next-version of unknown family is 1"
         (eql 1 (evo.registry:next-version 'probe.fresh)))
  (let* ((parent (evo.capability:make-capability 'probe.race
                                                 :version 1)))
    (evo.registry:register-capability-version parent)
    (let ((threads
           (loop repeat 4 collect
                 (sb-thread:make-thread
                  (lambda ()
                    (loop repeat 10 do
                      (evo.registry:derive-and-register-version
                       parent)))))))
    (dolist (thread threads) (sb-thread:join-thread thread))
    (let ((versions (evo.registry:capability-versions 'probe.race)))
      (check "40 concurrent derivations yield 40 unique versions"
             (and (= (length versions) 41)
                  (equal versions
                         (loop for v from 1 to 41 collect v)))))))
  (check-error "explicit duplicate version still rejected"
    (evo.registry:derive-and-register-version
     (evo.capability:make-capability 'probe.chain :version 9)
     :version 2))
  ;; #53: load-registry orders parents before children on its own.
  (let ((dir (probe-temp-dir)))
    (evo.registry:ensure-data-dir dir)
    (unwind-protect
         (let* ((v1 (evo.capability:make-capability 'probe.persist-chain
                                                    :version 1))
                (v2 (evo.capability:derive-version v1))
                (v3 (evo.capability:derive-version v2)))
           ;; Save child-first to prove load order independence.
           (evo.registry:save-capability v3 dir)
           (evo.registry:save-capability v1 dir)
           (evo.registry:save-capability v2 dir)
           (evo.registry:clear-registry)
           (let ((loaded (evo.registry:load-registry dir)))
             (check "chain reloads despite reverse save order"
                    (= (length loaded) 3))
             (let ((v3 (find 3 loaded
                             :key #'evo.capability:capability-version)))
               (check "reloaded chain keeps parent links"
                      (and v3 (eql 2 (evo.capability:capability-parent-version
                                      v3)))))))
      (ignore-errors
        (dolist (pattern '("*.lisp" "*.tmp"))
          (dolist (left (directory (merge-pathnames pattern dir)))
            (delete-file left))))))
  (evo.registry:clear-registry))

(handler-case
    (progn (run-probe)
           (run-lineage-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE capability-model: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE capability-model: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (error (c)
    (format t "PROBE capability-model: FAIL (toplevel ~A)~%" c)
    (sb-ext:exit :code 1)))
