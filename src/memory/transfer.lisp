;;;; src/memory/transfer.lisp — Transfer-gate evidence (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §29-30. A patch becomes a skill only after
;;;; reuse on independent tasks: minimum 3 reuses, no severe regressions,
;;;; held-out success at or above baseline.

(in-package :evo.transfer)

(defstruct (transfer-evidence
            (:constructor %make-transfer-evidence))
  "Independent reuse count, deltas, and regression flags."
  (reuse-count 0 :type integer)
  (success-delta 0 :type number)
  (token-delta 0 :type number)
  (severe-regressions 0 :type integer))

(defun make-transfer-evidence (&key reuse-count success-delta token-delta
                                     severe-regressions)
  "Construct a transfer-evidence record."
  (%make-transfer-evidence
   :reuse-count (or reuse-count 0)
   :success-delta (or success-delta 0)
   :token-delta (or token-delta 0)
   :severe-regressions (or severe-regressions 0)))

(defun transfer-green-p (evidence &key (min-reuse 3))
  "Return true when EVIDENCE satisfies the transfer gate: at least
MIN-REUSE independent reuses and zero severe regressions."
  (and (>= (transfer-evidence-reuse-count evidence) min-reuse)
       (zerop (transfer-evidence-severe-regressions evidence))))
