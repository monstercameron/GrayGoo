"""Hidden-test evaluator service (plan.md section 36, Domain D).

The evaluator runs as a SEPARATE process outside the mutable runtime. It
owns the hidden test corpus (``hidden_cases.json``), the promotion
thresholds, and the single mandatory promotion gate::

    evaluate(candidate_id, outputs)
        -> {"verdict": "pass" | "fail", "evidence": {...}}

GATE RULE: promotion may not proceed without a ``pass`` verdict from this
service. There is no other path to promotion: a ``fail`` verdict, a
protocol rejection (no verdict at all), a crashed evaluator process, or a
verdict for a different candidate id MUST all block promotion. The
promotion authority enforces this; this module's job is to make the
verdict unambiguous — exactly one of ``"pass"`` / ``"fail"`` per
evaluation, with per-case evidence and the thresholds applied.

Process protocol (see protocol.py): the kernel spawns::

    python -m evaluator.service   (or: python evaluator/service.py)

writes one JSON request document to stdin, and reads one JSON verdict
envelope from stdout. Exit status is 0 when a verdict was produced and 2
when the request was rejected (a JSON error envelope is still printed).

Scoring mirrors benchmarks/runner.py: ``exact`` compares modulo trailing
newlines, ``json`` compares parsed deep-equality. Every hidden check must
be answered: outputs are keyed ``"<case_id>:<check_index>"``; a missing
key fails that check. Unknown keys never pass anything — they are listed
under ``evidence["unexpected_outputs"]``.

Stdlib only.
"""

import json
import os
import subprocess
import sys

try:
    from evaluator import protocol as _protocol
except ImportError:  # running as a script: evaluator/service.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import protocol as _protocol

ProtocolError = _protocol.ProtocolError

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hidden_cases.json")

# The hidden gate is strict by default: every hidden case and every hidden
# check must pass. plan.md section 30 allows looser transfer thresholds,
# but those govern promotion *scope* deliberation, not this gate — any
# relaxation must be an explicit, auditable caller choice.
DEFAULT_THRESHOLDS = {
    "min_case_pass_rate": 1.0,
    "min_check_pass_rate": 1.0,
}

COMPARE_MODES = ("exact", "json")


# --------------------------------------------------------------------------
# Corpus loading
# --------------------------------------------------------------------------

def load_hidden_cases(path=None):
    """Load and validate the hidden corpus. Raises ValueError if corrupt."""
    with open(path or CASES_PATH, "r", encoding="utf-8") as handle:
        cases = json.load(handle)
    if not isinstance(cases, list) or not cases:
        raise ValueError("hidden corpus must be a non-empty JSON array")
    seen = set()
    for i, case in enumerate(cases):
        where = "hidden case %d" % i
        if not isinstance(case, dict):
            raise ValueError("%s: must be an object" % where)
        for key in ("id", "checks"):
            if key not in case:
                raise ValueError("%s: missing key %r" % (where, key))
        if case["id"] in seen:
            raise ValueError("duplicate hidden case id: %r" % case["id"])
        seen.add(case["id"])
        checks = case["checks"]
        if not isinstance(checks, list) or not checks:
            raise ValueError("%s: checks must be a non-empty array" % where)
        for j, check in enumerate(checks):
            cwhere = "%s check %d" % (where, j)
            if not isinstance(check, dict):
                raise ValueError("%s: must be an object" % cwhere)
            for key in ("input", "expected", "compare"):
                if key not in check:
                    raise ValueError("%s: missing key %r" % (cwhere, key))
                if not isinstance(check[key], str):
                    raise ValueError("%s: key %r must be a string"
                                     % (cwhere, key))
            if check["compare"] not in COMPARE_MODES:
                raise ValueError("%s: bad compare %r" % (cwhere,
                                                         check["compare"]))
            if check["compare"] == "json":
                try:
                    json.loads(check["expected"])
                except ValueError:
                    raise ValueError("%s: expected is not valid JSON" % cwhere)
    return cases


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def compare_check(actual, expected, mode):
    """Score one output. Returns (passed: bool, error: str | None)."""
    if not isinstance(actual, str):
        return False, "non_string_output"
    if mode == "exact":
        passed = actual.strip("\n") == expected.strip("\n")
        return passed, None if passed else "mismatch"
    if mode == "json":
        try:
            actual_doc = json.loads(actual)
        except ValueError:
            return False, "invalid_json_output"
        passed = actual_doc == json.loads(expected)
        return passed, None if passed else "mismatch"
    raise ValueError("unknown compare mode: %r" % mode)


def _resolve_thresholds(thresholds):
    merged = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        for name, value in thresholds.items():
            if name not in merged:
                raise ValueError("unknown threshold: %r" % name)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0.0 <= value <= 1.0):
                raise ValueError("threshold %r must be a number in [0, 1]"
                                 % name)
            merged[name] = float(value)
    return merged


def evaluate(candidate_id, outputs, cases=None, thresholds=None):
    """Score candidate outputs against the hidden corpus.

    ``outputs`` maps ``"<case_id>:<check_index>"`` to output text.
    Returns exactly ``{"verdict": "pass"|"fail", "evidence": {...}}``
    where evidence carries per-case results plus the applied thresholds.
    """
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be a non-empty string")
    if not isinstance(outputs, dict):
        raise ValueError("outputs must be a dict")
    if cases is None:
        cases = load_hidden_cases()
    applied = _resolve_thresholds(thresholds)

    case_results = []
    total_checks = 0
    passed_checks = 0
    for case in cases:
        check_results = []
        case_passed = True
        for index, check in enumerate(case["checks"]):
            total_checks += 1
            key = "%s:%d" % (case["id"], index)
            if key not in outputs:
                passed, error = False, "missing_output"
            else:
                passed, error = compare_check(outputs[key], check["expected"],
                                              check["compare"])
            if passed:
                passed_checks += 1
            else:
                case_passed = False
            entry = {"index": index, "key": key, "passed": passed,
                     "compare": check["compare"]}
            if error is not None:
                entry["error"] = error
            check_results.append(entry)
        case_results.append({"id": case["id"], "passed": case_passed,
                             "checks": check_results})

    passed_cases = sum(1 for c in case_results if c["passed"])
    case_rate = passed_cases / len(case_results) if case_results else 0.0
    check_rate = passed_checks / total_checks if total_checks else 0.0
    verdict = ("pass"
               if (case_rate >= applied["min_case_pass_rate"]
                   and check_rate >= applied["min_check_pass_rate"])
               else "fail")
    known_keys = {"%s:%d" % (c["id"], i)
                  for c in case_results for i in range(len(c["checks"]))}
    evidence = {
        "thresholds": applied,
        "summary": {
            "candidate_id": candidate_id,
            "total_cases": len(case_results),
            "passed_cases": passed_cases,
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "case_pass_rate": case_rate,
            "check_pass_rate": check_rate,
        },
        "cases": case_results,
        "unexpected_outputs": sorted(set(outputs) - known_keys),
    }
    return {"verdict": verdict, "evidence": evidence}


# --------------------------------------------------------------------------
# Subprocess entry point + client helper
# --------------------------------------------------------------------------

def handle_request_json(raw):
    """Parse + validate + evaluate one raw request document.

    Returns (envelope_dict, exit_code). Never raises on bad input: all
    request failures become an ``ok: False`` envelope with exit code 2.
    """
    try:
        request = _protocol.validate_request(json.loads(raw))
    except ValueError as exc:
        return _protocol.error_response(None, ProtocolError(
            "request is not valid JSON: %s" % exc, code="bad_json")), 2
    except ProtocolError as exc:
        request_id = None
        try:
            maybe = json.loads(raw)
            if isinstance(maybe, dict) and isinstance(
                    maybe.get("request_id"), str):
                request_id = maybe.get("request_id")
        except ValueError:
            pass
        return _protocol.error_response(request_id, exc), 2
    try:
        result = evaluate(request["candidate_id"], request["outputs"],
                          thresholds=request["thresholds"])
    except ValueError as exc:
        return _protocol.error_response(
            request["request_id"], ProtocolError(str(exc))), 2
    return _protocol.success_response(request, result), 0


def main(argv=None):
    del argv
    raw = sys.stdin.read()
    if not raw.strip():
        envelope = _protocol.error_response(
            None, ProtocolError("empty request", code="bad_json"))
        sys.stdout.write(json.dumps(envelope))
        return 2
    envelope, exit_code = handle_request_json(raw)
    sys.stdout.write(json.dumps(envelope))
    return exit_code


SERVICE_PATH = os.path.abspath(__file__)


def evaluate_in_subprocess(request, timeout=60, python=None):
    """Send a request dict to a FRESH evaluator process; return the envelope.

    This is the supported client path: scoring always happens across the
    process boundary so the candidate side can never observe hidden state.
    Raises RuntimeError if the service misbehaves (timeout, no JSON).
    """
    proc = subprocess.run(
        [python or sys.executable, SERVICE_PATH],
        input=json.dumps(request),
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(
            "evaluator subprocess returned no JSON (exit=%d, stderr=%r)"
            % (proc.returncode, proc.stderr[-500:]))


if __name__ == "__main__":
    sys.exit(main())
