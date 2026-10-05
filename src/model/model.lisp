;;;; src/model/model.lisp — Provider-neutral generation interface.
;;;;
;;;; Plan reference: plan.md §15-17, §47. GENERATE is the single choke point
;;;; for model calls, so token/latency accounting and green-stop (§18) can
;;;; be enforced in one place. No provider is implemented yet (bootstrap).
;;;; MODEL-CALL-RECORD is the lineage-carrying inference record: every
;;;; record immutably links task/run/candidate/generation (issues.md #66)
;;;; so experiments reconstruct causally.

(in-package :evo.model)

(defvar *autonomy-gears* '(:gear-0 :gear-1 :gear-2 :gear-3 :gear-4)
  "Autonomy gears from no-model (:gear-0) to recursive (:gear-4), §17.")

(defstruct (model-call-record
            (:constructor %make-model-call-record))
  "Latency, token, cost, and lineage metadata for one inference request.
TASK-ID / RUN-ID / CANDIDATE-ID / GENERATION immutably link the call
into task -> run -> candidate -> verdict (issues.md #66); REQUEST-ID
is the provider's idempotency key; COST-USD the accounted spend."
  (provider nil) (model nil) (latency-ms nil)
  (input-tokens nil) (output-tokens nil)
  (context-hash nil) (prompt-version nil) (result nil)
  (task-id nil) (run-id nil) (candidate-id nil) (generation nil)
  (cost-usd nil) (request-id nil))

(defun model-call-record (&key provider model latency-ms input-tokens
                               output-tokens context-hash prompt-version result
                               task-id run-id candidate-id generation
                               cost-usd request-id)
  "Construct a MODEL-CALL-RECORD with full causal lineage."
  (%make-model-call-record :provider provider :model model
                           :latency-ms latency-ms :input-tokens input-tokens
                           :output-tokens output-tokens
                           :context-hash context-hash
                           :prompt-version prompt-version :result result
                           :task-id task-id :run-id run-id
                           :candidate-id candidate-id :generation generation
                           :cost-usd cost-usd :request-id request-id))

(defgeneric generate (&key model context grammar max-tokens temperature)
  (:documentation "Generate a structured candidate. Providers implement methods.
Signals an error until a provider lane implements a method."))

(defmethod generate (&key model context grammar max-tokens temperature)
  (declare (ignore model context grammar max-tokens temperature))
  (error "No model provider implemented (bootstrap skeleton)."))
