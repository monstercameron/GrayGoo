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

;;;; Stable identity (issues.md #15).
;;;;
;;;; The symbol ID is the in-image binding name: it can drift across
;;;; package moves, reloads, and persistence round-trips (see
;;;; EVO.REGISTRY:RECONSTRUCT-ID, which interns into CL-USER when the
;;;; home package is missing). The STABLE-ID is the persistent identity:
;;;; a UUID version-4 string minted once per capability family, shared by
;;;; every version (see DERIVE-VERSION), and round-tripped through
;;;; persistence. DISPLAY-NAME is the human-facing binding label and
;;;; defaults to the symbol name. New code should key long-lived
;;;; references (ledgers, manifests, fingerprints) on the stable-id and
;;;; treat the symbol as display only.

(deftype stable-id ()
  "A persistent capability-identity string (UUID version 4)."
  'string)

(defvar *stable-id-random-state* (make-random-state t)
  "Random source for MAKE-STABLE-ID, seeded from the clock at load.")

(defun %uuid-hex (bytes)
  "Format 16 BYTES as a UUID version-4 string."
  (setf (aref bytes 6) (logior (logand (aref bytes 6) #x0F) #x40))
  (setf (aref bytes 8) (logior (logand (aref bytes 8) #x3F) #x80))
  (with-output-to-string (out)
    (loop for index below 16
          for byte = (aref bytes index)
          do (when (member index '(4 6 8 10)) (write-char #\- out))
             (format out "~2,'0X" byte))))

(defun make-stable-id ()
  "Mint a fresh stable identity: a UUID version-4 string.
No external dependencies; randomness comes from
*STABLE-ID-RANDOM-STATE*."
  (let ((bytes (make-array 16 :element-type '(unsigned-byte 8))))
    (loop for index below 16
          do (setf (aref bytes index)
                   (random 256 *stable-id-random-state*)))
    (string-downcase (%uuid-hex bytes))))

(defun stable-id-p (id)
  "Return true when ID is a well-formed stable-id UUID string."
  (and (stringp id)
       (= (length id) 36)
       (char= (char id 8) #\-) (char= (char id 13) #\-)
       (char= (char id 18) #\-) (char= (char id 23) #\-)
       (loop for index below 36
             for ch = (char id index)
             always (or (member index '(8 13 18 23))
                        (digit-char-p ch 16)))
       t))

(defvar *lifecycle-states*
  '(:proposed :ephemeral :patch :skill :procedural-family :stable
    :deprecated :retired)
  "Ordered lifecycle states (plan.md §10).")

(defun lifecycle-state-p (state)
  "Return true when STATE is a known lifecycle state."
  (and (member state *lifecycle-states*) t))

(defvar *capability-risk-levels*
  '(:r0 :r1 :r2 :r3 :r4 :r5 :r6)
  "Valid RISK values (issues.md #51; mirrors
EVO.REHEARSAL:*RISK-LEVELS*, plan.md §7). Kept local so this package
stays decoupled from the rehearsal stub.")

(defun risk-level-p (level)
  "Return true when LEVEL is a known capability risk level."
  (and (member level *capability-risk-levels*) t))

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
  (model nil :type t :read-only t)
  ;; Stable identity (issues.md #15): STABLE-ID is the persistent,
  ;; package-independent identity shared by every version of one family;
  ;; DISPLAY-NAME is the human/symbolic binding name. Both are additive:
  ;; older code that never passes them keeps working (see MAKE-CAPABILITY
  ;; defaults). New slots are appended so keyword construction is
  ;; unaffected.
  (stable-id nil :type (or null string) :read-only t)
  (display-name nil :type (or null string) :read-only t))

(defun make-capability (id &key (version 1) parent-version (state :proposed)
                             promotion-status (risk :r0)
                             (created-at (get-universal-time))
                             ttl intent contract inputs outputs effects
                             dependencies source creator model
                             stable-id display-name)
  "Construct an immutable capability version record.

ID must be a CAPABILITY-ID, VERSION a positive integer, and STATE /
PROMOTION-STATUS known lifecycle states (PROMOTION-STATUS defaults to
STATE). RISK must be one of *CAPABILITY-RISK-LEVELS* (issues.md #51).
TTL is NIL or a non-negative integer number of seconds.
STABLE-ID defaults to a fresh UUID string (see MAKE-STABLE-ID) and
DISPLAY-NAME defaults to the symbol name of ID; both are validated when
given explicitly."
  (unless (capability-id-p id)
    (error "Capability id must be a symbol, got ~S." id))
  (let ((stable (or stable-id (make-stable-id)))
        (display (or display-name (symbol-name id))))
    (unless (stable-id-p stable)
      (error "Stable id must be a UUID string, got ~S." stable))
    (unless (stringp display)
      (error "Display name must be a string, got ~S." display))
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
    (unless (risk-level-p risk)
      (error "Unknown risk level: ~S (want one of ~S)."
             risk *capability-risk-levels*))
    (when (and ttl (not (and (integerp ttl) (>= ttl 0))))
      (error "TTL must be NIL or a non-negative integer, got ~S." ttl))
    (%make-capability :id id :version version :parent-version parent-version
                      :state state :promotion-status promotion :risk risk
                      :created-at created-at :ttl ttl
                      :intent intent :contract contract
                      :inputs inputs :outputs outputs :effects effects
                      :dependencies dependencies :source source
                      :creator creator :model model
                      :stable-id stable :display-name display))))

(defun derive-version (capability &key version state promotion-status risk
                                    (ttl nil ttl-given-p)
                                    (intent nil intent-given-p)
                                    (contract nil contract-given-p)
                                    (inputs nil inputs-given-p)
                                    (outputs nil outputs-given-p)
                                    (effects nil effects-given-p)
                                    (dependencies nil dependencies-given-p)
                                    (source nil source-given-p)
                                    (creator nil creator-given-p)
                                    (model nil model-given-p)
                                    stable-id display-name)
  "Create the child version of CAPABILITY without mutating it.

The child keeps every parent field unless overridden. Its VERSION
defaults to one plus the parent version and its PARENT-VERSION is the
parent version, forming the lineage chain (todos: parent-version
lineage). An explicit :STATE also moves :PROMOTION-STATUS unless an
explicit :PROMOTION-STATUS is given. Pass :INTENT NIL / :CONTRACT NIL
explicitly to clear those slots; likewise an explicit NIL clears
:TTL (issues.md #50), :INPUTS/:OUTPUTS/:EFFECTS/:DEPENDENCIES
(issues.md #49), and :SOURCE/:CREATOR/:MODEL. Omitted keys inherit.
The STABLE-ID is inherited from the parent (one family, one
identity) unless explicitly overridden; the DISPLAY-NAME is
inherited likewise."
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
                     :ttl (if ttl-given-p ttl
                              (capability-ttl capability))
                     :intent (if intent-given-p intent
                                 (capability-intent capability))
                     :contract (if contract-given-p contract
                                   (capability-contract capability))
                     :inputs (if inputs-given-p inputs
                                 (capability-inputs capability))
                     :outputs (if outputs-given-p outputs
                                  (capability-outputs capability))
                     :effects (if effects-given-p effects
                                 (capability-effects capability))
                     :dependencies (if dependencies-given-p dependencies
                                       (capability-dependencies capability))
                     :source (if source-given-p source
                                 (capability-source capability))
                     :creator (if creator-given-p creator
                                  (capability-creator capability))
                     :model (if model-given-p model
                                (capability-model capability))
                     :stable-id (or stable-id
                                    (capability-stable-id capability))
                     :display-name (or display-name
                                       (capability-display-name
                                        capability)))))

(defun capability-expired-p (capability &optional (now (get-universal-time)))
  "Return true when CAPABILITY's TTL has elapsed relative to NOW.
A NIL TTL never expires."
  (let ((ttl (capability-ttl capability)))
    (and ttl (>= now (+ (capability-created-at capability) ttl)) t)))
