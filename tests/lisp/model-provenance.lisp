;;;; tests/lisp/model-provenance.lisp — Inference lineage + projection contract.
;;;;
;;;; Issues.md #66 (model-call lineage) and #67 (projection
;;;; provenance/freshness). Standalone probe: exits 0/1.

(defparameter *probe-dir*
  (make-pathname :defaults (or *load-truename* #P"./")
                 :name nil :type nil :version nil))
(defparameter *repo-root* (truename (merge-pathnames #P"../../" *probe-dir*)))

(let ((*load-verbose* nil) (*load-print* nil)
      (*compile-verbose* nil) (*compile-print* nil))
  (load (merge-pathnames #P"src/model/packages.lisp" *repo-root*))
  (load (merge-pathnames #P"src/model/model.lisp" *repo-root*))
  (load (merge-pathnames #P"src/model/world.lisp" *repo-root*)))

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

(defun fill-provenance (projection offset generation epoch now)
  (dolist (pair `((:derived-through-event . ,offset)
                  (:state-generation . ,generation)
                  (:active-epoch . ,epoch)
                  (:projected-at . ,now))
                projection)
    (setf (cdr (assoc (car pair) projection)) (cdr pair))))

(defun run-probe ()
  ;; #66: every inference record links task/run/candidate/generation.
  (let ((rec (evo.model:model-call-record
              :provider "cerebras" :model "qwen-3.8-27b"
              :input-tokens 100 :output-tokens 20 :cost-usd 0.0002d0
              :request-id "req-1" :task-id "A-TRN-08" :run-id "run-9"
              :candidate-id "cand-3" :generation 7 :result "42")))
    (check "lineage slots round-trip"
           (and (string= "A-TRN-08"
                         (evo.model::model-call-record-task-id rec))
                (string= "run-9"
                         (evo.model::model-call-record-run-id rec))
                (string= "cand-3"
                         (evo.model::model-call-record-candidate-id rec))
                (eql 7 (evo.model::model-call-record-generation rec))
                (eql 0.0002d0
                     (evo.model::model-call-record-cost-usd rec))
                (string= "req-1"
                         (evo.model::model-call-record-request-id rec)))))
  ;; #67: projections always carry provenance keys; filled ones verify.
  (let ((empty (evo.world:project-world '(:recent-failures))))
    (check "provenance keys included even for subsets"
           (every (lambda (key) (assoc key empty))
                  evo.world:*projection-provenance-keys*))
    (check-error "unfilled projection fails the contract"
      (evo.world:check-projection empty)))
  (let ((full (fill-provenance (evo.world:project-world) 120 7 3
                               3975964800)))
    (check "filled projection passes"
           (eq full (evo.world:check-projection full)))
    (let ((other (fill-provenance (evo.world:project-world) 120 7 3
                                  3975964810)))
      (check "same offset is consistent"
             (evo.world:projections-consistent-p full other)))
    (let ((stale (fill-provenance (evo.world:project-world) 100 7 3
                                  3975964810)))
      (check "different offsets are inconsistent"
             (not (evo.world:projections-consistent-p full stale))))
    (check "unprovenanced mix is inconsistent"
           (not (evo.world:projections-consistent-p
                 full (evo.world:project-world))))))

(handler-case
    (progn (run-probe)
           (if (zerop *failures*)
               (progn (format t "PROBE model-provenance: PASS~%")
                      (sb-ext:exit :code 0))
               (progn (format t "PROBE model-provenance: FAIL (~D)~%"
                              *failures*)
                      (sb-ext:exit :code 1))))
  (error (c)
    (format t "PROBE model-provenance: FAIL (toplevel ~A)~%" c)
    (sb-ext:exit :code 1)))
