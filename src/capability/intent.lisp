;;;; src/capability/intent.lisp — Semantic intent declarations.
;;;;
;;;; Plan reference: plan.md §8 (:intent clause), §12 (capability retrieval).
;;;; Intent text plus structured applicability conditions drive retrieval and
;;;; the dual procedural memory (plan.md §11). Records are immutable and
;;;; convertible to printable plists for persistence (plan.md §43).

(in-package :evo.intent)

(defstruct (intent
            (:constructor %make-intent)
            (:predicate intent-p))
  "Human/model-readable purpose plus applicability conditions. Immutable:
all slots are :read-only; build a new intent to change one."
  (text "" :type string :read-only t)
  (applies-when nil :type list :read-only t))

(defun make-intent (text &key applies-when)
  "Construct an intent with TEXT description and APPLIES-WHEN conditions."
  (%make-intent :text text :applies-when applies-when))

(defun intent->plist (intent)
  "Serialize INTENT to a printable plist for persistence."
  (list :text (intent-text intent)
        :applies-when (intent-applies-when intent)))

(defun plist->intent (plist)
  "Rebuild an intent record from an INTENT->PLIST plist."
  (make-intent (getf plist :text) :applies-when (getf plist :applies-when)))
