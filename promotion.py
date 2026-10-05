"""Promotion authority with mandatory hidden-evaluator verdict.

Implements plan.md sections 28 (promotion authority), 29-30 (transfer
gate), 36 (evaluator design), 41 (event ledger), 46 (rollback), and the
"Make evaluator verdict mandatory for promotion" item from todos.md.

Gate order in :func:`evaluate_promotion` (all fail closed):

1. ``(a)`` the candidate's source generation is still current;
2. ``(b)`` the risk-level gates for ``R0``-``R6`` (see :mod:`risk`) are
   satisfied by the evidence, and any transfer evidence present passes
   the :mod:`transfer` gate;
3. ``(c)`` the hidden evaluator (``evaluator`` package, Domain D)
   returns a ``pass`` verdict over its subprocess protocol. A ``fail``
   verdict, a protocol error, a candidate-id mismatch, or an
   unreachable service ALL reject promotion. There is no override.

On pass, an immutable version record plus a monotonic epoch are
written under ``versions/`` (``versions/<capability>.json``,
``versions/epochs.json``), a promotion event is appended to the given
ledger, and ``{"decision": "promote", ...}`` is returned.

Stdlib + ``evaluator`` + ``events`` (+ ``risk``/``transfer`` for gate
definitions) only. No network, no model calls.
"""

import hashlib
import json
import os
import subprocess
import sys
import time

from evaluator import protocol as _protocol
from evaluator import service as _evaluator_service

import risk as _risk
import transfer as _transfer

DEFAULT_VERSIONS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "versions")

EPOCHS_FILENAME = "epochs.json"

DECISION_PROMOTE = "promote"
DECISION_REJECT = "reject"

EVENT_PROMOTED = "candidate_promoted"
EVENT_REJECTED = "candidate_rejected"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _versions_dir(path=None):
    return path or DEFAULT_VERSIONS_DIR


def _safe_name(capability_id):
    """Map a capability id to a safe single-path-segment filename stem."""
    text = str(capability_id)
    cleaned = "".join(
        c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in text)
    return cleaned.strip(".") or "capability"


def _capability_path(versions_dir, capability_id):
    text = str(capability_id)
    stem = _safe_name(text)
    if stem != text:
        # QA-05: ids that do not round-trip through _safe_name would
        # collide ("a/b" vs "a_b" shared one file and merged
        # histories). Qualify such stems with a hash of the full id so
        # distinct ids always map to distinct files. Clean ids keep
        # their plain "<id>.json" name.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        stem = "%s__%s" % (stem, digest)
    return os.path.join(versions_dir, stem + ".json")


def _epochs_path(versions_dir):
    return os.path.join(versions_dir, EPOCHS_FILENAME)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json_atomic(path, document):
    """Write JSON atomically (temp file + rename) so readers never see halves."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp-%d" % os.getpid()
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)


def _load_capability_doc(versions_dir, capability_id):
    doc = _read_json(_capability_path(versions_dir, capability_id), None)
    if not isinstance(doc, dict):
        return None
    if not isinstance(doc.get("versions"), dict):
        return None
    return doc


def _load_epoch(versions_dir):
    doc = _read_json(_epochs_path(versions_dir), {})
    if not isinstance(doc, dict):
        return 0
    epoch = doc.get("epoch", 0)
    return epoch if isinstance(epoch, int) and epoch >= 0 else 0


def _read_json_checked(path):
    """Read JSON, distinguishing "missing" from "corrupt".

    Returns ``(present, value, error)``: ``present`` is False only when
    the file does not exist; otherwise ``error`` names the problem
    (unreadable bytes, invalid JSON) and ``value`` is None.
    """
    if not os.path.exists(path):
        return False, None, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return True, json.load(handle), None
    except (OSError, ValueError) as exc:
        return True, None, "unreadable %r: %s" % (
            os.path.basename(path), exc)


def _load_capability_doc_checked(versions_dir, capability_id):
    """Checked capability-doc load: ``(doc, error)``.

    ``(None, None)`` means never promoted (no file). Any other error
    means the file exists but is unreadable or invalid -- callers must
    fail closed, never silently restart versioning (QA-09).
    """
    path = _capability_path(versions_dir, capability_id)
    present, raw, error = _read_json_checked(path)
    if not present:
        return None, None
    if error is not None:
        return None, error
    if not isinstance(raw, dict) or not isinstance(raw.get("versions"), dict):
        return None, "corrupt %r: expected a dict with a dict 'versions'" % (
            os.path.basename(path),)
    try:
        int(raw.get("current_version", 0))
    except (TypeError, ValueError):
        return None, "corrupt %r: 'current_version' is not an integer" % (
            os.path.basename(path),)
    return raw, None


def _load_epoch_checked(versions_dir):
    """Checked epoch load: ``(epoch, error)``.

    ``(0, None)`` means nothing promoted yet (no file). Any other error
    means the file exists but is unreadable or invalid -- callers must
    fail closed, never silently restart at 1 (QA-09).
    """
    path = _epochs_path(versions_dir)
    present, raw, error = _read_json_checked(path)
    if not present:
        return 0, None
    if error is not None:
        return 0, error
    if not isinstance(raw, dict):
        return 0, "corrupt %r: expected a dict" % (
            os.path.basename(path),)
    epoch = raw.get("epoch", 0)
    if (not isinstance(epoch, int) or isinstance(epoch, bool)
            or epoch < 0):
        return 0, "corrupt %r: 'epoch' is not a non-negative integer" % (
            os.path.basename(path),)
    return epoch, None


def _gates_passed_of(evidence):
    """Collect the satisfied-gate names claimed by the evidence."""
    passed = evidence.get("gates_passed")
    if passed is None:
        test_evidence = evidence.get("test_evidence")
        if isinstance(test_evidence, dict):
            passed = test_evidence.get("gates_passed",
                                       test_evidence.get("passed_gates"))
    if isinstance(passed, (list, tuple, set)):
        return [str(g) for g in passed]
    return []


def _transfer_outcomes_of(evidence):
    """Return transfer outcome rows from the evidence, or None if absent."""
    transfer_evidence = evidence.get("transfer")
    if transfer_evidence is None:
        transfer_evidence = evidence.get("transfer_evidence")
    if transfer_evidence is None:
        return None
    if isinstance(transfer_evidence, dict):
        outcomes = transfer_evidence.get("outcomes",
                                         transfer_evidence.get("rows"))
        if outcomes is None:
            return None
    else:
        outcomes = transfer_evidence
    if isinstance(outcomes, list):
        return outcomes
    return None


def _call_evaluator(candidate_id, outputs, thresholds=None, timeout=60,
                    service_path=None):
    """Ask the hidden evaluator for a verdict over its subprocess protocol.

    Returns the decoded envelope dict. Raises RuntimeError (or
    propagates OSError) when the service is unreachable or misbehaves;
    callers MUST treat that as "do not promote".
    """
    request = _protocol.make_request(candidate_id, outputs,
                                     thresholds=thresholds)
    if service_path is None:
        return _evaluator_service.evaluate_in_subprocess(
            request, timeout=timeout)
    proc = subprocess.run(
        [sys.executable, service_path],
        input=json.dumps(request),
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(
            "evaluator subprocess returned no JSON (exit=%d, stderr=%r)"
            % (proc.returncode, proc.stderr[-500:]))


def _append_ledger(ledger, event_type, candidate_id, capability_id, version,
                   generation, payload):
    if ledger is None:
        return None
    return ledger.append_event(
        event_type,
        generation=generation,
        candidate_id=candidate_id,
        capability_id=capability_id,
        capability_version=version,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Promotion authority
# ---------------------------------------------------------------------------

def evaluate_promotion(candidate_id, evidence, *, generation, ledger=None,
                       versions_dir=None, service_path=None,
                       evaluator_timeout=60):
    """Decide whether *candidate_id* may be promoted.

    *evidence* carries the promotion inputs (plan.md section 28)::

        {
            "source_generation": int,   # generation the candidate ran against
            "capability_id": str,       # defaults to candidate_id
            "version": int | None,      # proposed version; None auto-assigns
            "risk_level": "R0".."R6",
            "gates_passed": [...],      # satisfied rehearsal gates
            "outputs": {...},           # candidate outputs for hidden eval
            "thresholds": {...},        # optional explicit eval thresholds
            "transfer": {"outcomes": [...]} | [...],  # optional reuse rows
        }

    Returns ``{"decision": "promote"|"reject", "reasons": [...],
    "version": int|None, "epoch": int|None}``. Rejections always carry
    at least one reason and never write version state.
    """
    reasons = []
    vdir = _versions_dir(versions_dir)

    def reject(reason, capability_id=None, version=None):
        reasons.append(reason)
        try:
            _append_ledger(ledger, EVENT_REJECTED, candidate_id,
                           capability_id, version, generation,
                           {"reasons": list(reasons)})
        except Exception as exc:  # QA-06: ledger outage must not escape;
            reasons.append("ledger append failed: %s" % (exc,))  # fail closed, audibly
        return {"decision": DECISION_REJECT, "reasons": list(reasons),
                "version": None, "epoch": None}

    # -- basic shape ------------------------------------------------------
    if not isinstance(candidate_id, str) or not candidate_id:
        return reject("candidate_id must be a non-empty string")
    if not isinstance(evidence, dict):
        return reject("evidence must be a dict")
    capability_id = evidence.get("capability_id", candidate_id)
    if not isinstance(capability_id, str) or not capability_id:
        return reject("capability_id must be a non-empty string")

    # -- gate (a): generation still current --------------------------------
    source_generation = evidence.get("source_generation")
    if source_generation is None:
        source_generation = evidence.get("generation")
    if source_generation is None:
        return reject("missing source generation: cannot prove currency",
                      capability_id)
    if source_generation != generation:
        return reject(
            "stale generation: candidate ran at %r, current is %r"
            % (source_generation, generation), capability_id)

    # -- gate (b1): risk-level gates satisfied ------------------------------
    risk_level = evidence.get("risk_level", evidence.get("risk"))
    if risk_level not in _risk.GATES:
        return reject(
            "unknown or missing risk level: %r (want one of R0-R6)"
            % (risk_level,), capability_id)
    if risk_level == "R6":
        return reject(
            "risk level R6: trusted-kernel mutation is promotion-forbidden; "
            "external release only", capability_id)
    required_gates = list(_risk.GATES[risk_level])
    passed = set(_gates_passed_of(evidence))
    missing = [g for g in required_gates if g not in passed]
    if missing:
        return reject(
            "risk gates unsatisfied for %s: missing %s"
            % (risk_level, ", ".join(missing)), capability_id)

    # -- gate (b2): transfer evidence, when present, must pass --------------
    outcomes = _transfer_outcomes_of(evidence)
    transfer_metrics = None
    if outcomes is not None:
        transfer_result = _transfer.promote_or_hold(
            candidate_id, outcomes,
            baseline_success=(evidence.get("transfer_baseline_success", 0.0)
                              if isinstance(evidence.get("transfer"), dict)
                              else 0.0))
        transfer_metrics = transfer_result.get("metrics")
        if transfer_result.get("decision") != _transfer.DECISION_PROMOTE:
            detail = "; ".join(transfer_result.get("reasons", []))
            return reject(
                "transfer gate not satisfied: %s" % (detail or "held"),
                capability_id)

    # -- gate (c): hidden-evaluator verdict REQUIRED (fail closed) -----------
    outputs = evidence.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        return reject("missing evaluator outputs: no hidden verdict possible",
                      capability_id)
    thresholds = evidence.get("thresholds")
    try:
        envelope = _call_evaluator(candidate_id, outputs,
                                   thresholds=thresholds,
                                   timeout=evaluator_timeout,
                                   service_path=service_path)
    except Exception as exc:  # unreachable / crashed / timed out: no verdict
        return reject("evaluator unreachable or failed: %s" % exc,
                      capability_id)
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        error = (envelope or {}).get("error", {}) if isinstance(
            envelope, dict) else {}
        return reject(
            "evaluator returned no verdict: %s"
            % (error.get("message", error) if isinstance(error, dict)
               else envelope), capability_id)
    if envelope.get("candidate_id") != candidate_id:
        return reject(
            "evaluator verdict candidate mismatch: %r != %r"
            % (envelope.get("candidate_id"), candidate_id), capability_id)
    if envelope.get("verdict") != "pass":
        summary = {}
        evidence_block = envelope.get("evidence")
        if isinstance(evidence_block, dict):
            summary = evidence_block.get("summary", {})
        return reject(
            "hidden evaluator verdict is %r (passed %s/%s checks)"
            % (envelope.get("verdict"),
               summary.get("passed_checks", "?"),
               summary.get("total_checks", "?")), capability_id)

    # -- version assignment + immutability -----------------------------------
    proposed = evidence.get("version")
    doc, state_error = _load_capability_doc_checked(vdir, capability_id)
    if state_error is not None:
        return reject(
            "corrupt capability state: %s; refusing to promote without "
            "an audited reset" % (state_error,), capability_id)
    epoch_base, epoch_error = _load_epoch_checked(vdir)
    if epoch_error is not None:
        return reject(
            "corrupt epoch state: %s; refusing to promote without an "
            "audited reset" % (epoch_error,), capability_id)
    current_version = 0
    recorded = {}
    if doc is not None:
        current_version = int(doc.get("current_version", 0))
        recorded = doc.get("versions", {})
    if proposed is None:
        version = current_version + 1
    else:
        if isinstance(proposed, bool) or not isinstance(proposed, int):
            return reject("version must be an integer", capability_id)
        version = proposed
    if version <= 0:
        return reject("version must be a positive integer", capability_id)
    if str(version) in recorded:
        return reject("version %d already recorded: version records are "
                      "immutable" % version, capability_id, version)
    if version <= current_version:
        return reject("version %d does not advance current version %d"
                      % (version, current_version), capability_id, version)

    # -- commit: version record + monotonic epoch -----------------------------
    epoch = epoch_base + 1
    record = {
        "capability_id": capability_id,
        "version": version,
        "parent_version": current_version or None,
        "candidate_id": candidate_id,
        "generation": generation,
        "epoch": epoch,
        "risk_level": risk_level,
        "gates_passed": sorted(passed),
        "evaluator_verdict": "pass",
        "transfer_metrics": transfer_metrics,
        "promoted_at": time.time(),
    }
    new_doc = {
        "capability_id": capability_id,
        "current_version": version,
        "versions": dict(recorded),
    }
    new_doc["versions"][str(version)] = record
    _write_json_atomic(_capability_path(vdir, capability_id), new_doc)
    _write_json_atomic(_epochs_path(vdir), {"epoch": epoch})

    promote_reasons = ["all gates passed"]
    try:
        _append_ledger(ledger, EVENT_PROMOTED, candidate_id, capability_id,
                       version, generation,
                       {"version": version, "epoch": epoch,
                        "risk_level": risk_level, "evaluator_verdict": "pass"})
    except Exception as exc:  # QA-06: never let a ledger outage escape;
        promote_reasons.append(  # the committed version record stands, audibly
            "ledger append failed: %s" % (exc,))
    return {"decision": DECISION_PROMOTE, "reasons": promote_reasons,
            "version": version, "epoch": epoch}


def get_current_version(capability_id, versions_dir=None):
    """Return the current promoted version, or None if never promoted."""
    doc = _load_capability_doc(_versions_dir(versions_dir), capability_id)
    if doc is None:
        return None
    try:
        return int(doc.get("current_version", 0)) or None
    except (TypeError, ValueError):
        return None


def get_current_epoch(versions_dir=None):
    """Return the current promotion epoch (0 when nothing promoted yet)."""
    return _load_epoch(_versions_dir(versions_dir))


def set_current_version(capability_id, version, versions_dir=None):
    """Roll back the dispatch pointer for *capability_id* to *version*.

    Moves the pointer BACK only: *version* must already be recorded and
    strictly below the current version. Version records are never
    mutated; only ``current_version`` moves. Returns
    ``{"capability_id": ..., "current_version": ..., "previous_version":
    ...}``. Raises ``KeyError`` for unknown capabilities/versions and
    ``ValueError`` for forward (or no-op) moves.
    """
    vdir = _versions_dir(versions_dir)
    doc = _load_capability_doc(vdir, capability_id)
    if doc is None:
        raise KeyError("unknown capability: %r" % (capability_id,))
    try:
        current = int(doc.get("current_version", 0))
    except (TypeError, ValueError):
        raise KeyError("capability %r has no current version"
                       % (capability_id,))
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("version must be an integer")
    if str(version) not in doc.get("versions", {}):
        raise KeyError("version %r of %r was never promoted"
                       % (version, capability_id))
    if version >= current:
        raise ValueError(
            "rollback moves back only: %r is not below current %r"
            % (version, current))
    doc["current_version"] = version
    _write_json_atomic(_capability_path(vdir, capability_id), doc)
    return {"capability_id": capability_id, "current_version": version,
            "previous_version": current}
