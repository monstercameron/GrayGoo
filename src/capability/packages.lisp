;;;; src/capability/packages.lisp — Capability-model package definitions.
;;;;
;;;; Plan reference: plan.md §8-11 (capability model, metadata, lifecycle,
;;;; dual procedural memory). A capability is the fundamental learned
;;;; executable unit: immutable versions plus intent, contracts, and evidence.

(defpackage :evo.capability
  (:use :cl)
  (:documentation "Capability identity, versions, and lifecycle states.")
  (:export #:capability
           #:capability-p
           #:capability-id
           #:capability-id-p
           #:capability-version
           #:capability-parent-version
           #:capability-state
           #:capability-promotion-status
           #:capability-risk
           #:capability-created-at
           #:capability-ttl
           #:capability-intent
           #:capability-contract
           #:capability-inputs
           #:capability-outputs
           #:capability-effects
           #:capability-dependencies
           #:capability-source
           #:capability-creator
           #:capability-model
           #:make-capability
           #:derive-version
           #:capability-expired-p
           #:*lifecycle-states*
           #:lifecycle-state-p
           #:*capability-risk-levels*
           #:risk-level-p
           #:stable-id
           #:stable-id-p
           #:make-stable-id
           #:capability-stable-id
           #:capability-display-name))

(defpackage :evo.contract
  (:use :cl)
  (:documentation "Preconditions, postconditions, and effect contracts.")
  (:export #:contract
           #:contract-p
           #:contract-requires
           #:contract-ensures
           #:contract-must-not
           #:make-contract
           #:check-contract
           #:contract->plist
           #:plist->contract))

(defpackage :evo.intent
  (:use :cl)
  (:documentation "Semantic intent declarations for capabilities.")
  (:export #:intent
           #:intent-p
           #:intent-text
           #:intent-applies-when
           #:make-intent
           #:intent->plist
           #:plist->intent))

(defpackage :evo.registry
  (:use :cl)
  (:documentation "Immutable version registry and lineage (plan.md §43).")
  (:export #:register-capability
           #:register-capability-version
           #:find-capability-version
           #:capability-versions
           #:lineage
           #:clear-registry
           #:registry-count
           #:*registry-data-dir*
           #:ensure-data-dir
           #:version-pathname
           #:capability->plist
           #:plist->capability
           #:save-capability
           #:load-capability
           #:save-registry
           #:load-registry
           #:find-by-stable-id
           #:stable-id-pathname))
