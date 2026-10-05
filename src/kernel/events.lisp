;;;; src/kernel/events.lisp — In-memory append-only event ledger (bootstrap).
;;;;
;;;; Plan reference: plan.md §41 (event ledger), §76 (event schema).
;;;; Production persistence (PostgreSQL/SQLite) arrives in a later lane;
;;;; this skeleton keeps an in-memory ledger with the same append-only API.

(in-package :evo.events)

(defvar *ledger* (make-array 0 :adjustable t :fill-pointer 0)
  "Append-only vector of event plists. Never mutated in place.")

(defun log-event (event-type &rest payload)
  "Append an event of EVENT-TYPE with PAYLOAD plist to the ledger.
Returns the event index."
  (vector-push-extend (list :index (length *ledger*)
                            :timestamp (get-universal-time)
                            :type event-type
                            :payload payload)
                      *ledger*)
  (1- (length *ledger*)))

(defun event-count ()
  "Return the number of events in the ledger."
  (length *ledger*))

(defun events-since (index)
  "Return a list of events with index >= INDEX."
  (loop for i from index below (length *ledger*)
        collect (aref *ledger* i)))

(defun clear-ledger ()
  "Empty the ledger. Test/bootstrap use only; production ledger is append-only."
  (setf (fill-pointer *ledger*) 0)
  0)
