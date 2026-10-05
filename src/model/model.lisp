;;;; src/model/model.lisp — Provider-neutral generation interface (stub).
;;;;
;;;; Plan reference: plan.md §15-17, §47. GENERATE is the single choke point
;;;; for model calls, so token/latency accounting and green-stop (§18) can
;;;; be enforced in one place. No provider calls are made by this skeleton.

(in-package :evo.model)

(defvar *autonomy-gears* '(:gear-0 :gear-1 :gear-2 :gear-3 :gear-4)
  "Autonomy gears from no-model (:gear-0) to recursive (:gear-4), §17.")

(defstruct (model-call-record
            (:constructor %make-model-call-record))
  "Latency, token, and identity metadata for a single inference request."
  (provider nil) (model nil) (latency-ms nil)
  (input-tokens nil) (output-tokens nil)
  (context-hash nil) (prompt-version nil) (result nil))

(defun model-call-record (&key provider model latency-ms input-tokens
                               output-tokens context-hash prompt-version result)
  "Construct a MODEL-CALL-RECORD. Bootstrap: plain constructor."
  (%make-model-call-record :provider provider :model model
                           :latency-ms latency-ms :input-tokens input-tokens
                           :output-tokens output-tokens
                           :context-hash context-hash
                           :prompt-version prompt-version :result result))

(defgeneric generate (&key model context grammar max-tokens temperature)
  (:documentation "Generate a structured candidate. Providers implement methods.
Signals an error until a provider lane implements a method."))

(defmethod generate (&key model context grammar max-tokens temperature)
  (declare (ignore model context grammar max-tokens temperature))
  (error "No model provider implemented (bootstrap skeleton)."))
