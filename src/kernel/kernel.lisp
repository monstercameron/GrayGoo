;;;; src/kernel/kernel.lisp — Runtime identity and generation fingerprints.
;;;;
;;;; Plan reference: plan.md §19 (worker generation fingerprints), §45
;;;; (checkpointing). A generation fingerprint pins the exact code/data
;;;; versions a worker rehearsed against; stale generations must be rebased.

(in-package :evo.kernel)

(defvar *runtime-version* "0.1.0"
  "Skeleton runtime version string. Bumped by the external release process.")

(defun runtime-version ()
  "Return the running kernel version string."
  *runtime-version*)

(defstruct generation-fingerprint
  "Identifies one immutable application/schema/capability generation."
  (application-generation 0 :type integer)
  (schema-generation 0 :type integer)
  (capability-set-hash "" :type string)
  (runtime-version *runtime-version* :type string)
  (kernel-protocol 1 :type integer))

(defvar *current-generation* (make-generation-fingerprint)
  "The generation this image was booted from.")

(defun current-generation ()
  "Return the current generation fingerprint of this image."
  *current-generation*)
