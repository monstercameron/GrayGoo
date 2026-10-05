;;;; src/model/cerebras.lisp — Cerebras/Qwen adapter (bootstrap stub).
;;;;
;;;; Plan reference: plan.md §47-49. The adapter owns connection pooling,
;;;; timeouts, token accounting, retries, and structured-response validation.
;;;; This skeleton performs NO network calls; credentials are never read here.

(in-package :evo.cerebras)

(defvar *default-model* "qwen-3-8-27b"
  "Primary synthesis model (plan.md §15). Name only; no calls are made.")

(defvar *api-key-env-var* "CEREBRAS_API_KEY"
  "Environment variable naming the Cerebras key. Read only by the
provider lane at request time; never printed or logged.")

(defun cerebras-generate (&key context max-tokens temperature)
  "Request one structured candidate from Cerebras. Bootstrap: signals an
error (no network, no credentials in the skeleton). CONTEXT, MAX-TOKENS,
and TEMPERATURE are accepted for API stability."
  (declare (ignore context max-tokens temperature))
  (error "Cerebras adapter not implemented (bootstrap skeleton)."))
