"""Evaluator subprocess protocol: request/response schema + validation.

The hidden-test evaluator (plan.md section 36, Domain D) runs as a SEPARATE
process. The trusted kernel speaks to it over stdio with one JSON document
per evaluation: the request on stdin, the verdict envelope on stdout.

Request schema (all keys required except ``thresholds``)::

    {
      "protocol_version": "1.0",
      "request_id": "<non-empty string>",
      "candidate_id": "<non-empty string>",
      "outputs": {"<case_id>:<check_index>": "<candidate output text>"},
      "thresholds": {"min_case_pass_rate": 1.0, "min_check_pass_rate": 1.0}
    }

Success response::

    {
      "protocol_version": "1.0",
      "request_id": "<echoed>",
      "candidate_id": "<echoed>",
      "ok": True,
      "verdict": "pass" | "fail",
      "evidence": {...}   # per-case results + thresholds, see service.py
    }

Error response (malformed request; never a verdict)::

    {
      "protocol_version": "1.0",
      "request_id": "<echoed or None>",
      "ok": False,
      "error": {"code": "<machine code>", "message": "<human detail>"}
    }

Validation is strict: unknown keys, wrong types, and out-of-range
thresholds are all rejected. A rejected request yields NO verdict, and
(see service.py) promotion may not proceed without a ``pass`` verdict.

Stdlib only.
"""

import uuid

PROTOCOL_VERSION = "1.0"

_REQUEST_KEYS = frozenset(
    {"protocol_version", "request_id", "candidate_id", "outputs", "thresholds"}
)
_REQUIRED_KEYS = frozenset({"request_id", "candidate_id", "outputs"})
_THRESHOLD_KEYS = frozenset({"min_case_pass_rate", "min_check_pass_rate"})


class ProtocolError(Exception):
    """A request (or envelope field) violates the protocol schema."""

    def __init__(self, message, code="bad_request"):
        super().__init__(message)
        self.code = code


def make_request(candidate_id, outputs, request_id=None, thresholds=None):
    """Build a protocol request dict (client-side helper)."""
    request = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id if request_id is not None else uuid.uuid4().hex,
        "candidate_id": candidate_id,
        "outputs": dict(outputs),
    }
    if thresholds is not None:
        request["thresholds"] = dict(thresholds)
    return request


def validate_request(obj):
    """Validate a decoded JSON request; return the normalized dict.

    Raises ProtocolError on any schema violation. The returned dict
    always contains ``thresholds`` (defaulted to ``{}`` when absent;
    service.py merges service-side defaults over it).
    """
    if not isinstance(obj, dict):
        raise ProtocolError(
            "request must be a JSON object, got %s" % type(obj).__name__
        )
    unknown = set(obj) - _REQUEST_KEYS
    if unknown:
        raise ProtocolError(
            "unknown request key(s): %s" % ", ".join(sorted(unknown))
        )
    missing = _REQUIRED_KEYS - set(obj)
    if missing:
        raise ProtocolError(
            "missing required key(s): %s" % ", ".join(sorted(missing))
        )
    version = obj.get("protocol_version", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported protocol_version: %r (want %r)"
            % (version, PROTOCOL_VERSION),
            code="bad_version",
        )
    request_id = obj["request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("request_id must be a non-empty string")
    candidate_id = obj["candidate_id"]
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ProtocolError("candidate_id must be a non-empty string")
    outputs = obj["outputs"]
    if not isinstance(outputs, dict):
        raise ProtocolError("outputs must be an object mapping case keys to text")
    for key in outputs:
        if not isinstance(key, str):
            raise ProtocolError("outputs keys must be strings")
    thresholds = obj.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ProtocolError("thresholds must be an object")
    unknown_t = set(thresholds) - _THRESHOLD_KEYS
    if unknown_t:
        raise ProtocolError(
            "unknown thresholds key(s): %s" % ", ".join(sorted(unknown_t))
        )
    for name, value in thresholds.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError("threshold %r must be a number" % name)
        if not 0.0 <= value <= 1.0:
            raise ProtocolError("threshold %r out of range [0, 1]" % name)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "candidate_id": candidate_id,
        "outputs": dict(outputs),
        "thresholds": dict(thresholds),
    }


def success_response(request, result):
    """Wrap an evaluate() result dict into a success envelope."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request["request_id"],
        "candidate_id": request["candidate_id"],
        "ok": True,
        "verdict": result["verdict"],
        "evidence": result["evidence"],
    }


def error_response(request_id, error):
    """Build an error envelope for a rejected request (no verdict)."""
    if isinstance(error, ProtocolError):
        code, message = error.code, str(error)
    else:
        code, message = "internal_error", str(error)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
