;;;; src/capability/registry.lisp — Immutable version registry + persistence.
;;;;
;;;; Plan reference: plan.md §43 (persistence model). Versions are immutable
;;;; and linked by parent-version lineage. The in-memory table is the live
;;;; index; SAVE-CAPABILITY / LOAD-CAPABILITY persist each version as one
;;;; printable file under a data dir so the image is reconstructible.

(in-package :evo.registry)

(defvar *registry* (make-hash-table :test 'equal)
  "(capability-id . version) -> record plist.")

(defvar *registry-data-dir* #P"data/capabilities/"
  "Default directory for persisted capability versions. Relative paths
resolve against *DEFAULT-PATHNAME-DEFAULTS*; rebind to an absolute dir
(e.g. under the OS temp area) for tests.")

(defun register-capability (id version record &key parent-version)
  "Register RECORD as VERSION of capability ID. Versions are immutable:
re-registering an existing (ID . VERSION) signals an error."
  (let ((key (cons id version)))
    (when (gethash key *registry*)
      (error "Capability ~S version ~S is immutable and already registered."
             id version))
    (setf (gethash key *registry*)
          (list :id id :version version :parent-version parent-version
                :record record))
  version))

(defun register-capability-version (capability)
  "Register a capability struct by its own id/version/parent-version."
  (register-capability (evo.capability:capability-id capability)
                       (evo.capability:capability-version capability)
                       capability
                       :parent-version
                       (evo.capability:capability-parent-version capability)))

(defun find-capability-version (id version)
  "Return the record plist for ID at VERSION, or NIL when absent."
  (gethash (cons id version) *registry*))

(defun capability-versions (id)
  "Return the sorted list of registered versions for capability ID."
  (let (versions)
    (maphash (lambda (key value)
               (declare (ignore value))
               (when (eql (car key) id)
                 (push (cdr key) versions)))
             *registry*)
    (sort versions #'<)))

(defun find-by-stable-id (stable-id)
  "Return the sorted version list for the family named by STABLE-ID.
Only entries whose record is a capability struct carrying STABLE-ID
match; raw (non-struct) records are skipped. Returns NIL when the
stable-id is unknown. Signals an error when STABLE-ID is malformed."
  (unless (evo.capability:stable-id-p stable-id)
    (error "Malformed stable-id: ~S." stable-id))
  (let (versions)
    (maphash (lambda (key value)
               (let ((record (getf value :record)))
                 (when (and (evo.capability:capability-p record)
                            (string= (evo.capability:capability-stable-id
                                      record)
                                     stable-id))
                   (push (cdr key) versions))))
             *registry*)
    (sort versions #'<)))

(defun stable-id-pathname (stable-id version &optional (dir *registry-data-dir*))
  "Drift-proof pathname for the family named by STABLE-ID at VERSION.
Unlike VERSION-PATHNAME (symbol-derived, human-browsable), this name
survives package moves and renames; both spellings persist the same
plist shape. Signals an error when STABLE-ID is malformed."
  (unless (evo.capability:stable-id-p stable-id)
    (error "Malformed stable-id: ~S." stable-id))
  (merge-pathnames (make-pathname
                    :name (format nil "~A-v~D" stable-id version)
                    :type "lisp")
                   dir))

(defun lineage (id version)
  "Return the parent-version chain for ID at VERSION, newest first."
  (loop with v = version
        for entry = (find-capability-version id v)
        while entry
        collect v into chain
        do (setf v (getf entry :parent-version))
        finally (return chain)))

(defun clear-registry ()
  "Remove every entry from the in-memory registry (persistence tests)."
  (clrhash *registry*))

(defun registry-count ()
  "Return the number of entries in the in-memory registry."
  (hash-table-count *registry*))

(defun ensure-data-dir (&optional (dir *registry-data-dir*))
  "Create DIR (and parents) when missing; return DIR."
  (ensure-directories-exist (merge-pathnames #P"dummy" dir))
  dir)

(defun version-pathname (id version &optional (dir *registry-data-dir*))
  "Pathname of the persisted file for ID at VERSION under DIR."
  (merge-pathnames (make-pathname
                    :name (format nil "~A-v~D"
                                  (string-downcase (symbol-name id)) version)
                    :type "lisp")
                   dir))

(defun capability->plist (capability)
  "Serialize CAPABILITY to a printable plist (plan.md §43).

The id is stored as name + package name so READ restores the right
symbol even across packages. INTENT/CONTRACT structs become nested
plists; every other field must already be printable (see SAVE-CAPABILITY)."
  (let ((id (evo.capability:capability-id capability))
        (intent (evo.capability:capability-intent capability))
        (contract (evo.capability:capability-contract capability)))
    (list :format-version 1
          :id-name (symbol-name id)
          :id-package (package-name (symbol-package id))
          :version (evo.capability:capability-version capability)
          :parent-version
          (evo.capability:capability-parent-version capability)
          :state (evo.capability:capability-state capability)
          :promotion-status
          (evo.capability:capability-promotion-status capability)
          :risk (evo.capability:capability-risk capability)
          :created-at (evo.capability:capability-created-at capability)
          :ttl (evo.capability:capability-ttl capability)
          :intent (and intent (evo.intent:intent->plist intent))
          :contract (and contract (evo.contract:contract->plist contract))
          :inputs (evo.capability:capability-inputs capability)
          :outputs (evo.capability:capability-outputs capability)
          :effects (evo.capability:capability-effects capability)
          :dependencies (evo.capability:capability-dependencies capability)
          :source (evo.capability:capability-source capability)
          :creator (evo.capability:capability-creator capability)
          :model (evo.capability:capability-model capability)
          ;; Stable identity (issues.md #15): round-tripped so reloads
          ;; keep the family identity across package moves. Optional on
          ;; read: files written before this field existed load with a
          ;; fresh stable-id (see PLIST->CAPABILITY).
          :stable-id (evo.capability:capability-stable-id capability)
          :display-name (evo.capability:capability-display-name
                         capability))))

(defun reconstruct-id (name package-name)
  "Rebuild the capability-id symbol stored as NAME + PACKAGE-NAME.
Keywords restore exactly; symbols whose package is missing at load
time are interned in CL-USER."
  (cond ((string= package-name "KEYWORD") (intern name :keyword))
        ((find-package package-name) (intern name package-name))
        (t (intern name :cl-user))))

(defun plist->capability (plist)
  "Rebuild a capability struct from a CAPABILITY->PLIST plist."
  (unless (eql (getf plist :format-version) 1)
    (error "Unsupported capability plist format: ~S."
           (getf plist :format-version)))
  (let ((intent-plist (getf plist :intent))
        (contract-plist (getf plist :contract)))
    (evo.capability:make-capability
     (reconstruct-id (getf plist :id-name) (getf plist :id-package))
     :version (getf plist :version)
     :parent-version (getf plist :parent-version)
     :state (getf plist :state)
     :promotion-status (getf plist :promotion-status)
     :risk (getf plist :risk)
     :created-at (getf plist :created-at)
     :ttl (getf plist :ttl)
     :intent (and intent-plist (evo.intent:plist->intent intent-plist))
     :contract (and contract-plist
                    (evo.contract:plist->contract contract-plist))
     :inputs (getf plist :inputs)
     :outputs (getf plist :outputs)
     :effects (getf plist :effects)
     :dependencies (getf plist :dependencies)
     :source (getf plist :source)
     :creator (getf plist :creator)
     :model (getf plist :model)
     ;; Missing keys (pre-#15 files) fall back to MAKE-CAPABILITY
     ;; defaults: a fresh stable-id and the symbol-name display name.
     :stable-id (getf plist :stable-id)
     :display-name (getf plist :display-name))))

(defun save-capability (capability &optional (dir *registry-data-dir*))
  "Persist CAPABILITY to a printable file under DIR; return the pathname.
Signals a print error when any field is not readably printable."
  (ensure-data-dir dir)
  (let ((path (version-pathname (evo.capability:capability-id capability)
                                (evo.capability:capability-version capability)
                                dir)))
    (with-open-file (out path :direction :output :if-exists :supersede
                              :external-format :utf-8)
      (let ((*print-readably* t)
            (*print-pretty* t)
            (*package* (find-package :cl-user)))
        (write (capability->plist capability) :stream out
               :readably t :pretty t)
        (terpri out)))
    path))

(defun load-capability (id version &optional (dir *registry-data-dir*))
  "Reload version VERSION of ID from DIR, register it when absent,
and return the struct. Reading binds *READ-EVAL* to NIL."
  (let ((path (version-pathname id version dir)))
    (with-open-file (in path :direction :input :external-format :utf-8)
      (let ((*package* (find-package :cl-user))
            (*read-eval* nil))
        (let ((capability (plist->capability (read in))))
          (unless (find-capability-version
                   (evo.capability:capability-id capability)
                   (evo.capability:capability-version capability))
            (register-capability-version capability))
          capability)))))

(defun save-registry (&optional (dir *registry-data-dir*))
  "Persist every registered capability struct under DIR.
Entries whose record is not a capability struct are skipped.
Returns the sorted list of pathnames written."
  (let (paths)
    (maphash (lambda (key value)
               (declare (ignore key))
               (let ((record (getf value :record)))
                 (when (evo.capability:capability-p record)
                   (push (save-capability record dir) paths))))
             *registry*)
    (sort paths #'string< :key #'namestring)))

(defun load-registry (&optional (dir *registry-data-dir*))
  "Reload every persisted capability file under DIR into the registry.
Returns the reloaded structs, sorted by (id name, version)."
  (let ((files (directory (merge-pathnames "*.lisp" dir)))
        structs)
    (dolist (path (sort files #'string< :key #'namestring) structs)
      (with-open-file (in path :direction :input :external-format :utf-8)
        (let ((*package* (find-package :cl-user))
              (*read-eval* nil))
          (let ((capability (plist->capability (read in))))
            (unless (find-capability-version
                     (evo.capability:capability-id capability)
                     (evo.capability:capability-version capability))
              (register-capability-version capability))
            (push capability structs)))))
    (sort structs
          (lambda (a b)
            (let ((na (symbol-name (evo.capability:capability-id a)))
                  (nb (symbol-name (evo.capability:capability-id b))))
              (or (string< na nb)
                  (and (string= na nb)
                       (< (evo.capability:capability-version a)
                          (evo.capability:capability-version b)))))))))
