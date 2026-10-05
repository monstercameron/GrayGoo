;;;; src/capability/contract.lisp — Capability contracts.
;;;;
;;;; Plan reference: plan.md §8 (:requires/:ensures clauses), §18
;;;; (green-stop success contracts). Full predicate evaluation arrives with
;;;; the rehearsal lane; this file defines the immutable contract record
;;;; plus printable plist conversion for persistence (plan.md §43).

(in-package :evo.contract)

(defstruct (contract
            (:constructor %make-contract)
            (:predicate contract-p))
  "Declarative contract attached to a capability version. Immutable:
all slots are :read-only; build a new contract to change one."
  (requires nil :type list :read-only t)
  (ensures nil :type list :read-only t)
  (must-not nil :type list :read-only t))

(defun make-contract (&key requires ensures must-not)
  "Construct a contract record from REQUIRES, ENSURES, and MUST-NOT clauses."
  (%make-contract :requires requires :ensures ensures :must-not must-not))

(defun check-contract (contract args result)
  "Evaluate CONTRACT against ARGS and RESULT. Bootstrap: accepts anything.
Returns T; the rehearsal lane implements real predicate checks."
  (declare (ignore contract args result))
  t)

(defun contract->plist (contract)
  "Serialize CONTRACT to a printable plist for persistence."
  (list :requires (contract-requires contract)
        :ensures (contract-ensures contract)
        :must-not (contract-must-not contract)))

(defun plist->contract (plist)
  "Rebuild a contract record from a CONTRACT->PLIST plist."
  (make-contract :requires (getf plist :requires)
                 :ensures (getf plist :ensures)
                 :must-not (getf plist :must-not)))
