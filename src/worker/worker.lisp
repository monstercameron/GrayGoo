;;;; src/worker/worker.lisp — Worker lifecycle (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §19-20, §52. Real workers are separate OS
;;;; processes with CPU/memory/time limits; this skeleton defines the worker
;;;; record and pool sizing, and refuses to spawn until the lane lands.

(in-package :evo.worker)

(defvar *pool-min* 4
  "Minimum prewarmed workers (plan.md §20).")

(defvar *pool-target-max* 16
  "Target maximum prewarmed workers (plan.md §20).")

(defstruct (worker
            (:constructor %make-worker))
  "Generation-pinned worker handle. WORKER-ALIVE-P tracks liveness."
  (id 0 :type integer)
  (generation nil)
  (role :compile :type symbol)
  (alive-p nil :type boolean))

(defun make-worker (id &key generation (role :compile))
  "Construct a (not yet spawned) worker handle for ID and GENERATION."
  (%make-worker :id id :generation generation :role role :alive-p nil))

(defun spawn-worker (worker)
  "Spawn WORKER as an isolated process. Bootstrap: signals an error."
  (declare (ignore worker))
  (error "Worker spawning not implemented (bootstrap skeleton)."))

(defun terminate-worker (worker)
  "Terminate WORKER. Bootstrap: marks the handle dead without OS calls."
  (setf (worker-alive-p worker) nil))

;;;; Rehearsal-thunk convention (worker spike; plan.md §19, §21 phase C).
;;;;
;;;; The Python driver (workers.py) loads this file into each fresh SBCL
;;;; child and calls RUN-TEST-THUNK with a zero-argument thunk. The driver
;;;; owns orchestration (timeouts, envelopes, pooling); this side only runs
;;;; the thunk and reports ok/condition/backtrace as values.

(defun %capture-backtrace ()
  "Best-effort backtrace string; never signals."
  (let ((s (make-string-output-stream)))
    (ignore-errors (sb-debug:print-backtrace :stream s :count 24))
    (get-output-stream-string s)))

(defun run-test-thunk (thunk)
  "Call THUNK (a function of zero arguments) and report the outcome.
Returns three values: OK-P, PAYLOAD, BACKTRACE-STRING. On success PAYLOAD
is the PRIN1 representation of the primary value (print length/level
capped); on failure PAYLOAD is the condition report string."
  (check-type thunk function)
  (let ((*print-length* 100)
        (*print-level* 10)
        (*print-readably* nil))
    (handler-case
        (let ((result (funcall thunk)))
          (handler-case
              (values t (prin1-to-string result) "")
            (serious-condition (condition)
              (values nil
                      (format nil "result unprintable: ~A" condition)
                      (%capture-backtrace)))))
      (serious-condition (condition)
        (values nil (princ-to-string condition) (%capture-backtrace))))))

;;;; Worker sandbox (adversarial hardening; plan.md §19, §60).
;;;;
;;;; INSTALL-WORKER-SANDBOX establishes Lisp-level containment inside a
;;;; rehearsal worker AFTER the driver loads this file and BEFORE candidate
;;;; code runs (the Python driver invokes it from the sandbox prelude; see
;;;; sandbox.py). It denies file/process/foreign-module operations by
;;;; replacing their function cells with denials, gates REQUIRE against a
;;;; module denylist, disables UNLOCK-PACKAGE, and re-locks the
;;;; implementation packages. Verified on SBCL 2.6.9 (Windows): CL/SB-EXT
;;;; ship locked, so unlock/redefine/relock works; symbols that do not
;;;; exist there (e.g. SB-EXT:LAUNCH-PROGRAM) are skipped via FIND-SYMBOL
;;;; so this file never names an absent symbol at read time.
;;;;
;;;; This is a speed bump, NOT a security boundary: SB-UNIX/SB-IMPL
;;;; internals and SB-ALIEN routines on already-loaded libraries stay
;;;; reachable in-image, and the OS still runs the worker as the user with
;;;; full rights. Real isolation needs OS enforcement (separate worker
;;;; user, deny ACLs, job objects). See sandbox.py and
;;;; documents/adversarial-report.md.

(defvar *worker-sandbox-installed* nil
  "Non-nil once INSTALL-WORKER-SANDBOX has run in this image.")

(defvar *worker-sandbox-denied-modules*
  '("SB-BSD-SOCKETS" "SB-POSIX" "ASDF")
  "Module names REQUIRE must refuse once the sandbox is installed.")

(defvar *worker-sandbox-denied-module-substrings*
  '("SOCKET" "POSIX")
  "Substring fallback for the REQUIRE denylist (names are upcased first).")

(defvar *worker-sandbox-denied-ops*
  '((:cl "OPEN") (:cl "PROBE-FILE") (:cl "LOAD") (:cl "COMPILE-FILE")
    (:cl "DELETE-FILE") (:cl "RENAME-FILE") (:cl "ENSURE-DIRECTORIES-EXIST")
    (:cl "TRUENAME") (:cl "DIRECTORY") (:cl "FILE-WRITE-DATE")
    (:cl "USER-HOMEDIR-PATHNAME")
    (:sb-ext "RUN-PROGRAM") (:sb-ext "LAUNCH-PROGRAM")
    (:sb-ext "SAVE-LISP-AND-DIE")
    (:sb-ext "PROCESS-OUTPUT") (:sb-ext "PROCESS-INPUT")
    (:sb-ext "PROCESS-CLOSE") (:sb-ext "PROCESS-WAIT") (:sb-ext "PROCESS-KILL")
    (:sb-alien "LOAD-SHARED-OBJECT"))
  "(PACKAGE SHORT-NAME) pairs whose function cells the sandbox denies.
Resolved at install time via FIND-SYMBOL; absent symbols are skipped so
the table stays portable across SBCL versions.")

(defun %sandbox-denied-module-p (module)
  "True when REQUIRE must refuse MODULE (name or string)."
  (let ((name (string-upcase (string module))))
    (or (member name *worker-sandbox-denied-modules* :test #'string=)
        (some (lambda (sub) (search sub name))
              *worker-sandbox-denied-module-substrings*))))

(defun %sandbox-deny (label)
  "Signal the standard sandbox denial for LABEL (a string)."
  (error "rehearsal sandbox denied: ~A is forbidden in this worker" label))

(defun %sandbox-clobber (package name)
  "Replace PACKAGE:NAME's function cell with a denial. T when done.
Missing packages, missing symbols, and unbound symbols are skipped."
  (let* ((pkg (find-package package))
         (sym (and pkg (find-symbol name pkg))))
    (when (and sym (fboundp sym))
      (setf (symbol-function sym)
            (let ((label (format nil "~A:~A" (package-name pkg) name)))
              (lambda (&rest args)
                (declare (ignore args))
                (%sandbox-deny label))))
      t)))

(defun install-worker-sandbox ()
  "Install Lisp-level containment in this worker image (idempotent).
Unlocks CL/SB-EXT/SB-ALIEN, denies *WORKER-SANDBOX-DENIED-OPS*, wraps
REQUIRE with the module denylist, disables UNLOCK-PACKAGE, and relocks.
Returns :INSTALLED on first run, :ALREADY-INSTALLED afterwards."
  (when *worker-sandbox-installed*
    (return-from install-worker-sandbox :already-installed))
  (dolist (pkg '(:cl :sb-ext :sb-alien))
    (when (find-package pkg)
      (sb-ext:unlock-package pkg)))
  ;; Wrap REQUIRE first so later denials cannot strand the module loader.
  (let ((require-sym (find-symbol "REQUIRE" :cl)))
    (when (and require-sym (fboundp require-sym))
      (let ((original (symbol-function require-sym)))
        (setf (symbol-function require-sym)
              (lambda (module &rest args)
                (if (%sandbox-denied-module-p module)
                    (%sandbox-deny
                     (format nil "MODULE ~A" (string module)))
                    (apply original module args)))))))
  (let ((denied 0))
    (dolist (op *worker-sandbox-denied-ops*)
      (when (%sandbox-clobber (first op) (second op))
        (incf denied)))
    ;; Disabling UNLOCK-PACKAGE keeps the relock below from being trivially
    ;; undone by candidate code (originals are unreachable anyway).
    (%sandbox-clobber :sb-ext "UNLOCK-PACKAGE")
    (dolist (pkg '(:cl :sb-ext :sb-alien))
      (when (find-package pkg)
        (sb-ext:lock-package pkg)))
    (setf *worker-sandbox-installed* t)
    (values :installed denied)))
