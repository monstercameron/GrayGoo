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

(defvar *registry-lock* (sb-thread:make-mutex :name "evo.registry")
  "Serializes registry updates (issues.md #54). SBCL mutexes are not
recursive, so %% helpers below do the work lock-free and public
wrappers take the lock exactly once; compound helpers call only the
%% variants inside their own held lock.")

(defmacro with-registry-lock (() &body body)
  "Execute BODY holding the registry lock."
  `(sb-thread:with-mutex (*registry-lock*)
     ,@body))

(defun %register-capability (id version record parent-version)
  "Lock-free registration core; caller must hold *REGISTRY-LOCK*."
  (let ((key (cons id version)))
    (when (gethash key *registry*)
      (error "Capability ~S version ~S is immutable and already registered."
             id version))
    (when (and parent-version
               (not (gethash (cons id parent-version) *registry*)))
      (error "Parent version ~S of ~S is not registered (orphan)."
             parent-version id))
    (setf (gethash key *registry*)
          (list :id id :version version :parent-version parent-version
                :record record))
    version))

(defun register-capability (id version record &key parent-version)
  "Register RECORD as VERSION of capability ID. Versions are immutable:
re-registering an existing (ID . VERSION) signals an error.
Registration is atomic under *REGISTRY-LOCK* (issues.md #54), and a
claimed PARENT-VERSION must already be registered with a smaller
version number (issues.md #53)."
  (unless (and (integerp version) (> version 0))
    (error "Capability version must be a positive integer, got ~S."
           version))
  (when parent-version
    (unless (and (integerp parent-version) (> parent-version 0))
      (error "Parent version must be a positive integer or NIL, got ~S."
             parent-version))
    (unless (< parent-version version)
      (error "Parent version ~S must precede version ~S (acyclic)."
             parent-version version)))
  (with-registry-lock ()
    (%register-capability id version record parent-version)))

(defun %capability-versions (id)
  "Lock-free sorted version list; caller must hold *REGISTRY-LOCK*."
  (let (versions)
    (maphash (lambda (key value)
               (declare (ignore value))
               (when (eql (car key) id)
                 (push (cdr key) versions)))
             *registry*)
    (sort versions #'<)))

(defun next-version (id)
  "Next free version number for ID: one plus the registered maximum, or 1."
  (with-registry-lock ()
    (1+ (reduce #'max (%capability-versions id) :initial-value 0))))

(defun %strip-version-key (plist)
  (let (out)
    (loop for (key value) on plist by #'cddr
          unless (eql key :version)
            do (setf out (list* key value out)))
    (nreverse out)))

(defun derive-and-register-version (parent &rest overrides
                                    &key version &allow-other-keys)
  "Derive from PARENT and register the child atomically (issues.md #54).

The child version is VERSION when given, else one plus the maximum of
the parent version and every registered version of the family — so two
concurrent derivations never collide (the second becomes a forked
successor, not a failed duplicate). OVERRIDES pass through to
DERIVE-VERSION. Returns the registered child struct."
  (declare (ignore version))
  (with-registry-lock ()
    (let* ((id (evo.capability:capability-id parent))
           (clean (%strip-version-key overrides))
           (child-version
             (or (getf overrides :version)
                 (1+ (max (evo.capability:capability-version parent)
                          (reduce #'max (%capability-versions id)
                                  :initial-value 0))))))
      (let ((child (apply #'evo.capability:derive-version parent
                          :version child-version clean)))
        (%register-capability-version child)
        child))))

(defun %register-capability-version (capability)
  "Lock-free struct registration; caller must hold *REGISTRY-LOCK*."
  (let ((version (evo.capability:capability-version capability)))
    (unless (and (integerp version) (> version 0))
      (error "Capability version must be a positive integer, got ~S."
             version))
    (let ((parent (evo.capability:capability-parent-version capability)))
      (when parent
        (unless (< parent version)
          (error "Parent version ~S must precede version ~S (acyclic)."
                 parent version)))
      (%register-capability (evo.capability:capability-id capability)
                            version capability parent))))

(defun register-capability-version (capability)
  "Register a capability struct by its own id/version/parent-version."
  (with-registry-lock ()
    (%register-capability-version capability)))

(defun find-capability-version (id version)
  "Return the record plist for ID at VERSION, or NIL when absent."
  (gethash (cons id version) *registry*))

(defun capability-versions (id)
  "Return the sorted list of registered versions for capability ID."
  (with-registry-lock ()
    (let (versions)
      (maphash (lambda (key value)
                 (declare (ignore value))
                 (when (eql (car key) id)
                   (push (cdr key) versions)))
               *registry*)
      (sort versions #'<))))

(defun find-by-stable-id (stable-id)
  "Return the sorted version list for the family named by STABLE-ID.
Only entries whose record is a capability struct carrying STABLE-ID
match; raw (non-struct) records are skipped. Returns NIL when the
stable-id is unknown. Signals an error when STABLE-ID is malformed."
  (unless (evo.capability:stable-id-p stable-id)
    (error "Malformed stable-id: ~S." stable-id))
  (with-registry-lock ()
    (let (versions)
      (maphash (lambda (key value)
                 (let ((record (getf value :record)))
                   (when (and (evo.capability:capability-p record)
                              (string= (evo.capability:capability-stable-id
                                        record)
                                       stable-id))
                     (push (cdr key) versions))))
               *registry*)
      (sort versions #'<))))

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

(defun %filename-escape (name)
  "Downcase NAME, replacing every non-alphanumeric char with underscore."
  (with-output-to-string (out)
    (loop for ch across (string-downcase (string name))
          do (write-char (if (alphanumericp ch) ch #\_) out))))

(defun version-pathname (id version &optional (dir *registry-data-dir*))
  "Pathname of the persisted file for ID at VERSION under DIR.

The file name embeds the escaped package identity
(issues.md #42): PACKAGE-A:FOO and PACKAGE-B:FOO persist to
different files. Uninterned symbols use the NIL-PACKAGE marker."
  (merge-pathnames (make-pathname
                    :name (format nil "~A--~A-v~D"
                                  (%filename-escape (symbol-name id))
                                  (%filename-escape
                                   (or (and (symbol-package id)
                                            (package-name
                                             (symbol-package id)))
                                       "nil-package"))
                                  version)
                    :type "lisp")
                   dir))

(defun %fnv1a-64 (string)
  "FNV-1a 64-bit checksum of STRING, as an unsigned integer.
Pure Lisp, no dependencies. Corruption detection, NOT a cryptographic
MAC: adversarial integrity needs the OS/user separation layer."
  (let ((hash #xCBF29CE484222325))
    (loop for ch across string
          do (setf hash (logxor hash (char-code ch))
                   hash (logand #xFFFFFFFFFFFFFFFF
                                (* hash #x100000001B3))))
    hash))

(defun %serialize (object)
  "Deterministic readable serialization of OBJECT for hashing/storage."
  (let ((*print-readably* t)
        (*print-pretty* t)
        (*print-right-margin* 80)
        (*print-base* 10)
        (*print-radix* nil)
        (*print-case* :upcase)
        (*print-length* nil)
        (*print-level* nil)
        (*print-circle* t)
        (*package* (find-package :cl-user)))
    (write-to-string object :readably t :pretty t)))

(defun payload-hash (payload)
  "Hex integrity checksum (issues.md #45) for a capability payload plist."
  (format nil "~16,'0X" (%fnv1a-64 (%serialize payload))))

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

(declaim (ftype (function (t) t) %plist->capability-v1))

(defun plist->capability (plist)
  "Rebuild a capability struct from a persisted plist.

Format 2 (current): verifies :CONTENT-HASH over :PAYLOAD first and
signals an error on mismatch (issues.md #45). Format 1 (legacy):
loads with a warning and no verification; re-save to upgrade."
  (let ((version (getf plist :format-version)))
    (cond ((eql version 2)
           (let ((payload (getf plist :payload))
                 (expected (getf plist :content-hash)))
             (unless (and (stringp expected)
                          (string= expected (payload-hash payload)))
               (error "Capability payload hash mismatch: file corrupt ~
                       or tampered (issues.md #45)."))
             (%plist->capability-v1 payload)))
          ((eql version 1)
           (warn "Loading legacy v1 capability file without integrity ~
                  hash; re-save to upgrade to v2.")
           (%plist->capability-v1 plist))
          (t (error "Unsupported capability plist format: ~S." version)))))

(defun %plist->capability-v1 (plist)
  "Rebuild a capability struct from a CAPABILITY->PLIST plist."
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

(defun %read-file-string (path)
  "Read the whole text file at PATH into a string."
  (with-open-file (in path :direction :input :external-format :utf-8)
    (let ((out (make-string-output-stream)))
      (loop for ch = (read-char in nil nil)
            while ch do (write-char ch out))
      (get-output-stream-string out))))

(defun save-capability (capability &optional (dir *registry-data-dir*))
  "Persist CAPABILITY to a printable file under DIR; return the pathname.
Signals a print error when any field is not readably printable.

Version files are write-once (issues.md #43): an existing file with
identical bytes returns the path (idempotent re-save); an existing
file with different bytes signals an error instead of overwriting.
Writes go to a same-directory temp file (type TMP, invisible to the
*.lisp registry glob) and are renamed into place (issues.md #44), so
a crash can leave a stray temp but never a half-written version."
  (ensure-data-dir dir)
  (let* ((path (version-pathname (evo.capability:capability-id capability)
                                 (evo.capability:capability-version capability)
                                 dir))
         (payload (capability->plist capability))
         (document (list :format-version 2
                         :content-hash (payload-hash payload)
                         :payload payload))
         (bytes (concatenate 'string (%serialize document)
                             (string #\Newline))))
    (when (probe-file path)
      (if (string= (%read-file-string path) bytes)
          (return-from save-capability path)
          (error "Version file exists and differs: ~A is immutable ~
                  (issues.md #43)." path)))
    (let ((tmp (make-pathname :name (format nil "~A-tmp-~D-~D"
                                            (pathname-name path)
                                            (get-universal-time)
                                            (random 1000000))
                              :type "tmp"
                              :defaults path)))
      (with-open-file (out tmp :direction :output :if-exists :supersede
                                :external-format :utf-8)
        (write-string bytes out)
        (finish-output out))
      (rename-file tmp path))
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
Returns the reloaded structs, sorted by (id name, version).
Reads all files first, then registers in (id, version) order so
lineage validation (issues.md #53) always sees parents before
children regardless of glob order."
  (let ((files (directory (merge-pathnames "*.lisp" dir)))
        structs)
    (dolist (path files)
      (with-open-file (in path :direction :input :external-format :utf-8)
        (let ((*package* (find-package :cl-user))
              (*read-eval* nil))
          (push (plist->capability (read in)) structs))))
    (setf structs
          (sort structs
                (lambda (a b)
                  (let ((na (symbol-name
                             (evo.capability:capability-id a)))
                        (nb (symbol-name
                             (evo.capability:capability-id b))))
                    (or (string< na nb)
                        (and (string= na nb)
                             (< (evo.capability:capability-version a)
                                (evo.capability:capability-version b))))))))
    (dolist (capability structs structs)
      (unless (find-capability-version
               (evo.capability:capability-id capability)
               (evo.capability:capability-version capability))
        (register-capability-version capability)))))
