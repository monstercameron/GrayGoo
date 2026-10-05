;;;; src/kernel/dispatch.lisp — Versioned dispatch cells and request epochs.
;;;;
;;;; Plan reference: plan.md §26 (versioned dispatch), §27 (epoch-based
;;;; promotion), §28 (promotion authority), §46 (rollback). Capabilities are
;;;; never redefined in place; the dispatch cell points at one immutable
;;;; version per epoch. INVOKE-CAPABILITY is the ONLY legal call path for
;;;; mutable capabilities: callers resolve the epoch-pinned version through
;;;; the cell and never hold direct function references.

(in-package :evo.dispatch)

;;; --- Dispatch cells ------------------------------------------------
;;; Each cell maps a capability-id to an immutable version list (entries are
;;; added, never removed) plus a current pointer naming the live version.

(defvar *cells* (make-hash-table :test 'eq)
  "Capability name -> (version -> function) table.
Versions are immutable once registered: added, never removed.")

(defvar *current-versions* (make-hash-table :test 'eq)
  "Capability name -> currently dispatched version (the live pointer).")

;;; --- Epochs ---------------------------------------------------------
;;; Every promotion and every rollback publishes a fresh epoch holding a
;;; full name->version snapshot. Requests pin the epoch they started under,
;;; so in-flight requests keep their old behavior while new requests move
;;; on. Old snapshots stay resolvable until DRAIN-EPOCH reclaims them; the
;;; version functions themselves are always retained.

(defvar *latest-epoch* 1
  "Newest published dispatch epoch. Bumped by every promotion/rollback.")

(defvar *epoch-snapshots* (make-hash-table :test 'eql)
  "Epoch -> snapshot table of capability name -> version.")

(defvar *epoch-pins* (make-hash-table :test 'eql)
  "Epoch -> count of live requests pinned to it (see WITH-EPOCH).")

(defvar *current-epoch* nil
  "Dynamically bound request epoch (plan.md §27), or NIL for \"latest\".")

(unless (gethash 1 *epoch-snapshots*)
  (setf (gethash 1 *epoch-snapshots*) (make-hash-table :test 'eq)))

(defun latest-epoch ()
  "Return the newest published dispatch epoch."
  *latest-epoch*)

(defun current-epoch ()
  "Return the epoch of the current dynamic request context.
Falls back to the latest epoch when no request epoch is bound."
  (or *current-epoch* *latest-epoch*))

(defun epoch-pinned-p (epoch)
  "Return true when live requests still pin EPOCH."
  (plusp (gethash epoch *epoch-pins* 0)))

(defun %snapshot (epoch)
  "Return the routing snapshot for EPOCH, or signal an error when EPOCH
is unknown or already drained."
  (or (gethash epoch *epoch-snapshots*)
      (error "Unknown or drained epoch ~S." epoch)))

(defun %pin-epoch (epoch)
  "Pin EPOCH for one live request. Signals when EPOCH is not live."
  (%snapshot epoch)
  (setf (gethash epoch *epoch-pins*)
        (1+ (gethash epoch *epoch-pins* 0))))

(defun %unpin-epoch (epoch)
  "Release one request pin on EPOCH."
  (let ((count (gethash epoch *epoch-pins* 0)))
    (when (plusp count)
      (setf (gethash epoch *epoch-pins*) (1- count)))))

(defmacro with-epoch (epoch &body body)
  "Execute BODY with requests pinned to EPOCH (plan.md §27).
EPOCH must be a live (non-drained) epoch; it stays pinned for the
duration so DRAIN-EPOCH cannot reclaim it underneath BODY."
  (let ((epoch-var (gensym "EPOCH")))
    `(let ((,epoch-var ,epoch))
       (%pin-epoch ,epoch-var)
       (unwind-protect
            (let ((*current-epoch* ,epoch-var))
              ,@body)
         (%unpin-epoch ,epoch-var)))))

;;; --- Registration ---------------------------------------------------

(defun register-version (name version function)
  "Register FUNCTION as VERSION of capability NAME. Returns VERSION.
The first version registered for NAME becomes the dispatched version;
later versions stay dormant until promoted (PROMOTE-VERSION).
Re-registering an existing version is an error: versions are immutable."
  (check-type name symbol)
  (unless (functionp function)
    (error "Not a function: ~S." function))
  (let ((versions (or (gethash name *cells*)
                      (setf (gethash name *cells*)
                            (make-hash-table :test 'eql)))))
    (when (nth-value 1 (gethash version versions))
      (error "Version ~S of capability ~S is already registered (immutable)."
             version name))
    (setf (gethash version versions) function)
    (unless (nth-value 1 (gethash name *current-versions*))
      (setf (gethash name *current-versions*) version)
      (setf (gethash name (%snapshot *latest-epoch*)) version))
    version))

(defun capability-versions (name)
  "Return the ascending list of registered versions for capability NAME.
The list only grows: versions are never deleted (plan.md §4.7)."
  (let ((versions (gethash name *cells*)))
    (unless versions
      (error "Unknown capability ~S." name))
    (sort (loop for v being the hash-keys of versions collect v) #'<)))

(defun current-version (name)
  "Return the currently dispatched version of capability NAME, or NIL."
  (gethash name *current-versions*))

;;; --- Promotion / rollback --------------------------------------------

(defun %publish-epoch (name version)
  "Point NAME at VERSION under a fresh epoch. Returns the new epoch.
Copies the latest snapshot so older epochs keep resolving exactly what
they resolved before; never deletes any version."
  (let ((versions (gethash name *cells*)))
    (unless (and versions (nth-value 1 (gethash version versions)))
      (error "No such version ~S of capability ~S." version name)))
  (let ((snapshot (make-hash-table :test 'eq))
        (new-epoch (1+ *latest-epoch*)))
    (maphash (lambda (k v) (setf (gethash k snapshot) v))
             (%snapshot *latest-epoch*))
    (setf (gethash name snapshot) version)
    (setf (gethash new-epoch *epoch-snapshots*) snapshot)
    (setf *latest-epoch* new-epoch)
    (setf (gethash name *current-versions*) version)
    new-epoch))

(defun promote-version (name version)
  "Promote VERSION of capability NAME to current under a new epoch
(plan.md §27-28). Requests pinned to older epochs keep resolving their
old versions. Returns the new epoch."
  (check-type name symbol)
  (%publish-epoch name version))

(defun rollback-to (name version)
  "Move the dispatch pointer for NAME back to VERSION under a new epoch
(plan.md §46). Instant: only the pointer moves — no version is ever
deleted, and requests pinned to older epochs are unaffected.
Returns the new epoch."
  (check-type name symbol)
  (%publish-epoch name version))

(defun drain-epoch (epoch)
  "Reclaim the routing snapshot for EPOCH once its requests have drained.
Refuses the latest epoch and any epoch with pinned requests. The
version functions are retained: draining drops only the epoch's routing
table. Returns EPOCH."
  (when (eql epoch *latest-epoch*)
    (error "Cannot drain the latest epoch ~S." epoch))
  (%snapshot epoch)
  (when (epoch-pinned-p epoch)
    (error "Cannot drain epoch ~S: ~D request(s) still pinned."
           epoch (gethash epoch *epoch-pins*)))
  (remhash epoch *epoch-snapshots*)
  (remhash epoch *epoch-pins*)
  epoch)

;;; --- Invocation ------------------------------------------------------

(defun %resolve-version (name epoch)
  "Resolve capability NAME to its dispatched version under EPOCH."
  (let ((snapshot (%snapshot epoch)))
    (multiple-value-bind (version present-p) (gethash name snapshot)
      (unless present-p
        (error "Capability ~S has no version in epoch ~S." name epoch))
      version)))

(defun invoke-capability (name args &key (epoch (current-epoch)))
  "Invoke capability NAME with argument list ARGS under EPOCH.
This is the ONLY legal call path for mutable capabilities (plan.md §26):
the epoch-pinned version is resolved through the dispatch cell, so
callers never hold direct function references. EPOCH defaults to the
current request epoch (see WITH-EPOCH), or the latest epoch outside one."
  (check-type name symbol)
  (check-type args list)
  (let* ((version (%resolve-version name epoch))
         (fn (gethash version (gethash name *cells*))))
    (unless fn
      (error "Version ~S of capability ~S is missing from its cell."
             version name))
    (apply fn args)))
