;;;; src/model/packages.lisp — Model-interaction package definitions.
;;;;
;;;; Plan reference: plan.md §14 (context compiler), §15-17 (LLM interaction,
;;;; output grammar, autonomy gears), §42 (world model), §47 (Cerebras
;;;; integration). The model emits S-expressions; prose stays off the hot path.

(defpackage :evo.model
  (:use :cl)
  (:documentation "Provider-neutral generation interface and autonomy gears.")
  (:export #:generate
           #:*autonomy-gears*
           #:model-call-record))

(defpackage :evo.cerebras
  (:use :cl)
  (:documentation "Cerebras / Qwen inference adapter (plan.md §47).")
  (:export #:cerebras-generate
           #:*default-model*
           #:*api-key-env-var*))

(defpackage :evo.context
  (:use :cl)
  (:documentation "Minimal task-context compiler (plan.md §14).")
  (:export #:compile-context
           #:*context-token-budget*))

(defpackage :evo.world
  (:use :cl)
  (:documentation "Model-facing world-model projections (plan.md §42).")
  (:export #:project-world
           #:*projection-keys*))
