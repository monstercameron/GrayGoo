"""Pre-rehearsal policy gates + worker sandbox wiring (stdlib only).

BOUNDARY -- READ THIS FIRST: nothing here OS-isolates the SBCL rehearsal
worker. This module provides three harness-side layers, each honest about
what it cannot do:

* :func:`refuse_to_rehearse` -- static policy gate over candidate text plus
  its :mod:`risk` classification. R6 (promotion-forbidden) is always
  refused; process-spawn and network-write payloads are always refused;
  other R4 payloads are refused unless explicitly approved. Anything it
  cannot parse is refused as unclassifiable. It cannot see through
  obfuscation (computed symbols, ``intern``, macros that expand to effect
  calls); the rehearsal worker stays hostile territory.
* ``WORKER_PRELUDE`` / :func:`build_prelude` -- Lisp-level containment
  installed inside each worker before candidate code runs. The mechanism
  lives in ``src/worker/worker.lisp`` (``install-worker-sandbox``): file /
  process / foreign-module operations are replaced with denials, REQUIRE
  is gated against a module denylist, and the implementation packages are
  re-locked. Verified working on SBCL 2.6.9 (Windows). Bypassable in-image
  via SB-UNIX / SB-IMPL internals and SB-ALIEN routines on already-loaded
  libraries -- a speed bump, not a security boundary.
* :class:`WorkerJail` -- a scratch root (an :class:`effects.OverlayFS`) plus
  a transactional :class:`effects.StateSandbox` wrapped around worker runs
  for cwd-pinning and write auditing. It does NOT restrict what the worker
  OS process can reach (same user, full rights); OS enforcement (separate
  worker user, deny ACLs, job objects) is still future work.

Spec: plan.md sections 19 (rehearsal workers), 60 (resource limits);
adversarial findings: documents/adversarial-report.md.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid

import effects
import s_expr

__all__ = [
    "WORKER_PRELUDE",
    "WorkerJail",
    "build_prelude",
    "refuse_to_rehearse",
]

#: Worker prelude: install Lisp-level containment (mechanism owned by
#: src/worker/worker.lisp). Double colon because packages.lisp does not
#: export the installer (additive lane constraint); the package itself is
#: always loaded by the driver before the prelude runs.
WORKER_PRELUDE = "(evo.worker::install-worker-sandbox)"

_RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4", "R5", "R6")

# Payload heads that are always refused (no approval path in this gate).
# Aligned with the risk.py R4 process / network-write signals.
_HARD_PROCESS_HEADS = frozenset({
    "run-program", "launch-program", "execute-program", "make-process",
    "spawn", "exec", "shell", "fork", "waitpid",
})
_HARD_NETWRITE_HEADS = frozenset({
    "socket-connect", "socket-send", "socket-send-to",
    "http-post", "http-put", "http-delete", "http-request",
})
# Whole packages whose mere presence is a process/network capability
# (covers helpers like get-host-by-name that do egress without a
# network-write head of their own).
_HARD_PROCESS_PACKAGES = frozenset({"sb-posix"})
_HARD_NETWORK_PACKAGES = frozenset({"sb-bsd-sockets", "drakma", "dex"})

_MAX_HARD_REASONS = 8


def build_prelude(jail=None):
    """Return the sandbox prelude text to evaluate before candidate code.

    Always installs worker containment; when ``jail`` (a :class:`WorkerJail`
    or a path) is given, also pins ``*default-pathname-defaults*`` to the
    jail root so relative pathnames land inside it (absolute paths still
    bypass -- audited, not enforced).
    """
    parts = [WORKER_PRELUDE]
    if jail is not None:
        if hasattr(jail, "root"):
            root = jail.root
        else:
            root = os.fspath(jail)
        if not isinstance(root, str):
            raise TypeError("jail root must be str, got %s"
                            % type(root).__name__)
        text = root.replace("\\", "/")
        if not text.endswith("/"):
            text += "/"
        parts.append('(setf *default-pathname-defaults* #P"%s")'
                     % text.replace('"', '\\"'))
    return " ".join(parts)


def _payload_atom(atom, hits):
    """Record hard-deny (kind, reason) hits for one symbol/keyword."""
    if not isinstance(atom, str) or isinstance(atom, s_expr.SString):
        return
    if not atom:
        return
    if atom.startswith(":"):
        body = atom[1:].lower().replace("_", "-")
        if body in _HARD_PROCESS_PACKAGES:
            hits.append(("process", "refused: candidate payload requires "
                                    "module %r (process capability; process "
                                    "effects are never rehearsed)"
                         % atom))
        elif body in _HARD_NETWORK_PACKAGES:
            hits.append(("network-write", "refused: candidate payload "
                                          "requires module %r (network "
                                          "capability; network-write "
                                          "effects are never rehearsed)"
                         % atom))
        return
    segments = atom.split(":")
    base = segments[-1].lower() or ""
    package = segments[0].lower().replace("_", "-") if len(segments) > 1 else ""
    if base in _HARD_PROCESS_HEADS or package in _HARD_PROCESS_PACKAGES:
        hits.append(("process", "refused: candidate payload invokes process "
                                "operation %r (no approval path in this "
                                "gate; process effects are never rehearsed)"
                     % atom))
    elif base in _HARD_NETWRITE_HEADS \
            or package in _HARD_NETWORK_PACKAGES:
        hits.append(("network-write", "refused: candidate payload performs "
                                      "network-write operation %r (no "
                                      "approval path in this gate; "
                                      "network-write effects are never "
                                      "rehearsed)" % atom))


def _payload_walk(node, hits):
    """Walk parsed candidate nodes for hard-deny payload signals.

    String contents and quoted subtrees are data, never scanned (same
    hygiene as risk.py).
    """
    if isinstance(node, s_expr.SString):
        return
    if isinstance(node, str):
        _payload_atom(node, hits)
        return
    if isinstance(node, (int, float, bool)) or node is None:
        return
    if not isinstance(node, (list, tuple)):
        return
    if not node:
        return
    head = node[0]
    if isinstance(head, str) and not isinstance(head, s_expr.SString) \
            and head.lower() == "quote":
        return
    for element in node:
        _payload_walk(element, hits)


def refuse_to_rehearse(candidate_text, risk, approved=False):
    """Pre-rehearsal policy gate over candidate text plus its risk rating.

    :param candidate_text: a single ``(candidate ...)`` form (source text).
    :param risk: the :func:`risk.classify` dict for that candidate.
    :param approved: True only when the R4 approval-policy gate already
        granted explicit approval for this rehearsal.
    :returns: a list of refusal reasons; an empty list means ALLOWED.
    :raises TypeError: ``candidate_text`` is not a string (API misuse).

    Policy: R6 is always refused (promotion-forbidden, external release
    only); process-spawn and network-write payloads are always refused
    (no approval path here); other R4 payloads are refused unless
    ``approved``; unparseable or unclassifiable input is refused. R5 is
    allowed by this gate (its benchmark-shaped gates are enforced
    elsewhere, not at rehearsal-admission time).
    """
    if not isinstance(candidate_text, str):
        raise TypeError("candidate_text must be str, got %s"
                        % type(candidate_text).__name__)
    level = risk.get("level") if isinstance(risk, dict) else None
    if level not in _RISK_LEVELS:
        return ["refused: risk assessment is missing or unclassifiable; "
                "rehearsal requires a valid R0-R6 classification"]
    if level == "R6":
        return ["refused R6: trust-root mutation is promotion-forbidden "
                "and must never be rehearsed (external release only)"]
    try:
        parsed = s_expr.parse_candidate(candidate_text)
    except s_expr.SExprError as exc:
        return ["refused: candidate text is not a valid (candidate ...) "
                "form (%s); unclassifiable input is never rehearsed"
                % (exc,)]
    found = []
    _payload_walk(parsed["definition"], found)
    _payload_atom(parsed["target"], found)
    reasons = []
    seen = set()
    for _kind, text in found:
        if text not in seen:
            seen.add(text)
            reasons.append(text)
    if len(reasons) > _MAX_HARD_REASONS:
        reasons = reasons[:_MAX_HARD_REASONS]
        reasons.append("(%d more payload signals omitted)"
                       % (len(seen) - _MAX_HARD_REASONS))
    if level == "R4" and not approved:
        reasons.append("refused R4: external effect requires the "
                       "approval-policy gate; unapproved rehearsal is "
                       "blocked")
        if isinstance(risk, dict):
            shown = 0
            for reason in risk.get("reasons", []):
                if isinstance(reason, str) and reason.startswith("R4:"):
                    reasons.append("R4 evidence: " + reason)
                    shown += 1
                    if shown >= 3:
                        break
    return reasons


class WorkerJail:
    """Scratch jail root + harness-side guardrails around worker runs.

    Owns a unique temp dir (or wraps ``root``) exposed as an
    :class:`effects.OverlayFS` (:attr:`overlay` / :attr:`root`), plus a
    transactional :class:`effects.StateSandbox` (:attr:`state`) for
    harness-side speculative work. As a context manager it snapshots on
    entry and, on exit, rolls back open state transactions, records the
    file :attr:`changes` since entry, closes state, and removes owned
    temp dirs (best effort).

    This pins the worker's cwd, gives relative paths a place to land, and
    audits writes -- it does NOT OS-confine the worker process, which
    still runs as the user with full rights. Treat :attr:`changes` as
    audit evidence, not as proof of containment.
    """

    def __init__(self, root=None):
        if root is None:
            leaf = "graygoo-jail-%s-%s" % (os.getpid(),
                                           uuid.uuid4().hex[:12])
            root = os.path.join(tempfile.gettempdir(), leaf)
            self._owned = True
        else:
            root = os.fspath(root)
            self._owned = False
        self.overlay = effects.OverlayFS(root)
        #: Absolute jail root (all overlay paths resolve under it).
        self.root = self.overlay.root
        #: Transactional harness-side scratch state (never canonical).
        self.state = effects.StateSandbox()
        self._snapshot = None
        #: ``{relpath: change}`` since ``__enter__`` (after ``__exit__``).
        self.changes = {}

    def __enter__(self):
        self._snapshot = self.overlay.snapshot()
        self.state.begin()
        return self

    def __exit__(self, *exc_info):
        try:
            while self.state.rollback():
                pass
        finally:
            try:
                self.changes = self.overlay.diff(self._snapshot)
            finally:
                self.state.close()
                if self._owned:
                    shutil.rmtree(self.root, ignore_errors=True)
        return False

    def diff(self):
        """Diff current jail files against the entry snapshot."""
        return self.overlay.diff(self._snapshot)

    def __repr__(self):
        return "%s(root=%r)" % (type(self).__name__, self.root)
