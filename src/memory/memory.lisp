;;;; src/memory/memory.lisp — Skill families and retrieval (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §11-13 (dual memory, retrieval, composition).
;;;; Retrieval returns at most a handful of relevant capabilities; composition
;;;; is attempted before synthesis. Real ranking arrives with the memory lane.

(in-package :evo.memory)

(defstruct (skill-family
            (:constructor %make-skill-family))
  "Intent, applicability, abstract procedure, and specializations."
  (name (error "Skill family name required.") :type symbol)
  (intent "" :type string)
  (applies-when nil :type list)
  (procedure nil :type list)
  (specializations nil :type list))

(defun make-skill-family (name &key intent applies-when procedure
                               specializations)
  "Construct a skill family record."
  (%make-skill-family :name name :intent (or intent "")
                      :applies-when applies-when :procedure procedure
                      :specializations specializations))

(defun find-skills (goal &key (limit 8))
  "Retrieve up to LIMIT skill families relevant to GOAL. Bootstrap:
returns an empty list — no skill store exists yet."
  (declare (ignore goal limit))
  nil)

(defun record-reuse (skill task &key success-p)
  "Record reuse of SKILL on TASK. Bootstrap: accepts and returns T."
  (declare (ignore skill task success-p))
  t)
