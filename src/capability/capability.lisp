;;;; src/capability/capability.lisp — Capability identity and lifecycle.
;;;;
;;;; Plan reference: plan.md §8 (capability model), §9 (metadata), §10
;;;; (lifecycle: proposed → ephemeral → patch → skill → family → stable).
;;;;
;;;; Versions are immutable: every CAPABILITY slot is :read-only, so any SETF
;;;; on a version signals an error. DERIVE-VERSION creates the child version
;;;; without touching the parent, linking them via PARENT-VERSION.

(in-package :evo.capability)

(deftype capability-id ()
  "Identity of a capability family (todos: Define `capability-id`).
Symbols name globally addressable capabilities."
  'symbol)

(defun capability-id-p (id)
  "Return true when ID is a valid CAPABILITY-ID."
  (typep id 'capability-id))

(defvar *lifecycle-states*
  '(:proposed :ephemeral :patch :skill :procedural-family :stable
    :deprecated :retired)
  "Ordered lifecycle states (plan.md §10).")

(defun lifecycle-state-p (state)
  "Return true when STATE is a known lifecycle state."
  (and (member state *lifecycle-states*) t))

(defstruct (capability
            (:constructor %make-capability)
            (:predicate capability-p))
  "Immutable record for one version of a capability (plan.md §8-§9).

ID names the capability family; VERSION numbers this revision and
PARENT-VERSION links the version it was derived from (NIL for roots).
INTENT holds an EVO.INTENT:INTENT struct (or NIL), CONTRACT holds an
EVO.CONTRACT:CONTRACT struct (or NIL); both slots are :TYPE T so this
file keeps no load-order dependency on those packages. INPUTS, OUTPUTS,
EFFECTS, and DEPENDENCIES are printable metadata lists. TTL is a
lifetime in seconds (NIL means no expiry); PROMOTION-STATUS tracks the
lifecycle state for promotion decisions. SOURCE keeps the defining
source (a string or a readable form) for persistence (plan.md §43)."
  (id (error "Capability id required.") :type symbol :read-only t)
  (version 0 :type integer :read-only t)
  (parent-version nil :type (or null integer) :read-only t)
  (state :proposed :type symbol :read-only t)
  (promotion-status :proposed :type symbol :read-only t)
  (risk :r0 :type symbol :read-only t)
  (created-at (get-universal-time) :type integer :read-only t)
  (ttl nil :type (or null integer) :read-only t)
  (intent nil :type t :read-only t)
  (contract nil :type t :read-only t)
  (inputs nil :type list :read-only t)
  (outputs nil :type list :read-only t)
  (effects nil :type list :read-only t)
  (dependencies nil :type list :read-only t)
  (source nil :type t :read-only t)
  (creator nil :type t :read-only t)
  (model nil :type t :read-only t))

(defun make-capability (id &key (version 1) parent-version (state :proposed)
                             promotion-status (risk :r0)
                             (created-at (get-universal-time))
                             ttl intent contract inputs outputs effects
                             dependencies source creator model)
  "Construct an immutable capability version record.

ID must be a CAPABILITY-ID, VERSION a positive integer, and STATE /
PROMOTION-STATUS known lifecycle states (PROMOTION-STATUS defaults to
STATE). TTL is NIL or a non-negative integer number of seconds."
  (unless (capability-id-p id)
    (error "Capability id must be a symbol, got ~S." id))
  (unless (and (integerp version) (> version 0))
    (error "Capability version must be a positive integer, got ~S." version))
  (when (and parent-version
             (not (and (integerp parent-version) (> parent-version 0))))
    (error "Parent version must be a positive integer or NIL, got ~S."
           parent-version))
  (unless (lifecycle-state-p state)
    (error "Unknown lifecycle state: ~S." state))
  (let ((promotion (or promotion-status state)))
    (unless (lifecycle-state-p promotion)
      (error "Unknown promotion status: ~S." promotion))
    (when (and ttl (not (and (integerp ttl) (>= ttl 0))))
      (error "TTL must be NIL or a non-negative integer, got ~S." ttl))
    (%make-capability :id id :version version :parent-version parent-version
                      :state state :promotion-status promotion :risk risk
                      :created-at created-at :ttl ttl
                      :intent intent :contract contract
                      :inputs inputs :outputs outputs :effects effects
                      :dependencies dependencies :source source
                      :creator creator :model model)))

(defun derive-version (capability &key version state promotion-status risk ttl
                                    (intent nil intent-given-p)
                                    (contract nil contract-given-p)
                                    inputs outputs effects
                                    dependencies source creator model)
  "Create the child version of CAPABILITY without mutating it.

The child keeps every parent field unless overridden. Its VERSION
defaults to one plus the parent version and its PARENT-VERSION is the
parent version, forming the lineage chain (todos: parent-version
lineage). An explicit :STATE also moves :PROMOTION-STATUS unless an
explicit :PROMOTION-STATUS is given. Pass :INTENT NIL / :CONTRACT NIL
explicitly to clear those slots."
  (unless (capability-p capability)
    (error "Not a capability: ~S." capability))
  (let* ((child-version (or version (1+ (capability-version capability))))
         (new-state (or state (capability-state capability)))
         (new-promotion (or promotion-status state
                            (capability-promotion-status capability))))
    (make-capability (capability-id capability)
                     :version child-version
                     :parent-version (capability-version capability)
                     :state new-state
                     :promotion-status new-promotion
                     :risk (or risk (capability-risk capability))
                     :ttl (if (null ttl) (capability-ttl capability) ttl)
                     :intent (if intent-given-p intent
                                 (capability-intent capability))
                     :contract (if contract-given-p contract
                                   (capability-contract capability))
                     :inputs (or inputs (capability-inputs capability))
                     :outputs (or outputs (capability-outputs capability))
                     :effects (or effects (capability-effects capability))
                     :dependencies (or dependencies
                                       (capability-dependencies capability))
                     :source (or source (capability-source capability))
                     :creator (or creator (capability-creator capability))
                     :model (or model (capability-model capability)))))

(defun capability-expired-p (capability &optional (now (get-universal-time)))
  "Return true when CAPABILITY's TTL has elapsed relative to NOW.
A NIL TTL never expires."
  (let ((ttl (capability-ttl capability)))
    (and ttl (>= now (+ (capability-created-at capability) ttl)) t)))
