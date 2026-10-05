;;;; graygoo.asd — ASDF system definition for the GrayGoo self-evolving Lisp runtime.
;;;;
;;;; Plan reference: plan.md sections 73 (project modules) and 74 (repository
;;;; layout). Reference runtime: SBCL. This file defines the bootstrap skeleton:
;;;; package definitions plus minimal coherent stubs. No external dependencies.

(defsystem "graygoo"
  :description "Self-evolving Lisp runtime: capability synthesis, isolated rehearsal, and transfer-based promotion."
  :author "GrayGoo"
  :license "TBD"
  :version "0.1.0"
  :depends-on ()
  :serial t
  :components
  ((:module "src/kernel"
    :serial t
    :components ((:file "packages")
                 (:file "kernel")
                 (:file "dispatch")
                 (:file "events")
                 (:file "effects")
                 (:file "security")))
   (:module "src/capability"
    :serial t
    :components ((:file "packages")
                 (:file "capability")
                 (:file "contract")
                 (:file "intent")
                 (:file "registry")))
   (:module "src/model"
    :serial t
    :components ((:file "packages")
                 (:file "model")
                 (:file "cerebras")
                 (:file "context")
                 (:file "world")))
   (:module "src/worker"
    :serial t
    :components ((:file "packages")
                 (:file "worker")))
   (:module "src/evaluator-client"
    :serial t
    :components ((:file "packages")
                 (:file "eval-client")))
   (:module "src/rehearsal"
    :serial t
    :components ((:file "packages")
                 (:file "rehearsal")))
   (:module "src/state"
    :serial t
    :components ((:file "packages")
                 (:file "state")
                 (:file "schema")
                 (:file "recovery")))
   (:module "src/memory"
    :serial t
    :components ((:file "packages")
                 (:file "memory")
                 (:file "transfer")
                 (:file "consolidation")))
   (:module "src/promotion"
    :serial t
    :components ((:file "packages")
                 (:file "promotion")
                 (:file "policy")))
   (:module "src/metrics"
    :serial t
    :components ((:file "packages")
                 (:file "metrics")))))
