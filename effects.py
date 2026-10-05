"""Process-level effect isolation for the GrayGoo Python harness (stdlib only).

BOUNDARY -- READ THIS FIRST: this module is *process-level* containment for
the Python rehearsal harness. It gates declared effects, roots scratch file
I/O under a temp dir, default-denies network dial-out, replays recorded
responses for rehearsal reads, and runs speculative state work in
transactional SQLite sandboxes.

It does NOT sandbox raw SBCL child *syscalls*. This box has no containers
and no Windows job-object integration, so a hostile worker process could
still reach the OS directly. Treat this module as a harness-side guardrail
for cooperative speculative code -- not a security boundary. Real worker
isolation (job objects / containers / separate hosts) is future work.

Spec: plan.md section 22 (effect system) and section 23 (effect
virtualization); task list: todos.md "Add effect isolation".
"""

from __future__ import annotations

import hashlib
import os
import sqlite3

__all__ = [
    "EFFECTS",
    "AllowlistedConnection",
    "DenyNetwork",
    "EffectDenied",
    "EffectGrant",
    "NetworkDenied",
    "OverlayFS",
    "PathEscape",
    "RecordedTransport",
    "RecordedTransportPKR",
    "StateSandbox",
    "UnrecordedRequest",
]


# ---------------------------------------------------------------------------
# Effect declarations (plan.md section 22)
# ---------------------------------------------------------------------------

EFFECTS = frozenset({
    "pure",
    "state-read",
    "state-write",
    "filesystem-read",
    "filesystem-write",
    "network-read",
    "network-write",
    "queue-read",
    "queue-write",
    "email",
    "payment",
    "process",
    "clock",
    "randomness",
    "ffi",
})


class EffectDenied(Exception):
    """Raised when code attempts an effect outside its grant."""


class NetworkDenied(EffectDenied, ConnectionError):
    """Network dial-out refused: default-deny, no allowlisted test double."""


class PathEscape(EffectDenied, ValueError):
    """Filesystem path escapes the overlay root (a denied filesystem effect)."""


class UnrecordedRequest(KeyError):
    """No recorded response exists for this rehearsal request."""


class EffectGrant:
    """Declared effect set for one unit of speculative work.

    ``allowed`` is stored as a frozenset of effect names (see :data:`EFFECTS`
    for the plan.md section 22 vocabulary). :meth:`check` raises
    :class:`EffectDenied` naming the missing grant.
    """

    def __init__(self, allowed=()):
        self.allowed = frozenset(allowed)

    def __contains__(self, name):
        return name in self.allowed

    def granted(self, name):
        """Return True when ``name`` is within this grant."""
        return name in self.allowed

    def check(self, name):
        """Return True when granted; otherwise raise EffectDenied naming it."""
        if name not in self.allowed:
            raise EffectDenied(
                "effect %r is not granted (allowed: %s)"
                % (name, sorted(self.allowed))
            )
        return True

    def __repr__(self):
        return "%s(allowed=%r)" % (type(self).__name__, sorted(self.allowed))


# ---------------------------------------------------------------------------
# Overlay filesystem (plan.md section 23: "filesystem -> overlay filesystem")
# ---------------------------------------------------------------------------

class OverlayFS:
    """Sandboxed scratch filesystem rooted at ``root``.

    Every path is resolved against ``root``; anything escaping it (``..``,
    an absolute path outside the root, a symlink pointing out) raises
    :class:`PathEscape`. Writes create missing parent dirs inside the root.
    :meth:`snapshot`/:meth:`diff` support state audit. Containment is
    check-then-act (best effort, no TOCTOU-proof file-descriptor pinning).
    """

    def __init__(self, root):
        real = os.path.realpath(os.path.abspath(os.fspath(root)))
        os.makedirs(real, exist_ok=True)
        self.root = real

    def resolve(self, path):
        """Return the absolute in-root path for ``path`` (else PathEscape)."""
        candidate = os.path.realpath(os.path.join(self.root, os.fspath(path)))
        root_norm = os.path.normcase(self.root)
        cand_norm = os.path.normcase(candidate)
        try:
            shared = os.path.commonpath([root_norm, cand_norm])
        except ValueError:
            raise PathEscape(
                "path escapes overlay root %r: %r" % (self.root, path)
            ) from None
        if shared != root_norm:
            raise PathEscape(
                "path escapes overlay root %r: %r" % (self.root, path)
            )
        return candidate

    def read(self, path):
        """Return file bytes; ``path`` must resolve inside the root."""
        with open(self.resolve(path), "rb") as handle:
            return handle.read()

    def read_text(self, path, encoding="utf-8"):
        """Return file text decoded with ``encoding`` (default utf-8)."""
        return self.read(path).decode(encoding)

    def write(self, path, data):
        """Write bytes (or utf-8 str) inside the root; return absolute path."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        target = self.resolve(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        return target

    def exists(self, path):
        """True when ``path`` resolves in-root and exists (escape -> False)."""
        try:
            target = self.resolve(path)
        except PathEscape:
            return False
        return os.path.exists(target)

    def snapshot(self):
        """Map every in-root file's '/'-joined relpath to its sha256 hex."""
        snap = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                digest = hashlib.sha256()
                with open(full, "rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                snap[rel] = digest.hexdigest()
        return snap

    def diff(self, before=None):
        """Diff current state against snapshot ``before`` (None = empty).

        Returns ``{relative_path: change}`` with change in "added",
        "modified", "deleted". Only in-root files are ever reported.
        """
        old = {} if before is None else dict(before)
        new = self.snapshot()
        changes = {}
        for rel in sorted(set(old) | set(new)):
            old_hash, new_hash = old.get(rel), new.get(rel)
            if old_hash is None:
                changes[rel] = "added"
            elif new_hash is None:
                changes[rel] = "deleted"
            elif old_hash != new_hash:
                changes[rel] = "modified"
        return changes


# ---------------------------------------------------------------------------
# Network stubs (plan.md section 23: reads recorded/stubbed, writes journaled)
# ---------------------------------------------------------------------------

class AllowlistedConnection:
    """Stub connection handle returned for allowlisted test doubles."""

    def __init__(self, host, port=None):
        self.host = host
        self.port = port

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __repr__(self):
        return "%s(host=%r, port=%r)" % (
            type(self).__name__, self.host, self.port)


class DenyNetwork:
    """Default-deny transport stub.

    :meth:`connect` raises :class:`NetworkDenied` unless ``host`` exactly
    matches (case-insensitively) an explicitly allowlisted test-double host.
    The ``allow``/``allowed_hosts``/``allowlist``/... spellings are merged so
    doubles can be declared whichever way reads best at the call site.
    """

    def __init__(self, allow=(), *, allowed=(), allowed_hosts=(),
                 allowlist=(), allowlisted=()):
        merged = (set(allow) | set(allowed) | set(allowed_hosts)
                  | set(allowlist) | set(allowlisted))
        self.allowed_hosts = frozenset(merged)
        self._match = frozenset(
            host.lower() for host in merged if isinstance(host, str))

    def allow_host(self, host):
        """Add one test-double host to the allowlist at runtime."""
        self.allowed_hosts = self.allowed_hosts | {host}
        if isinstance(host, str):
            self._match = self._match | {host.lower()}

    def connect(self, host, port=None, **kwargs):
        """Return a stub connection for an allowlisted double, else deny."""
        if not isinstance(host, str) or host.lower() not in self._match:
            raise NetworkDenied(
                "network connect to host %r denied: not an explicitly "
                "allowlisted test double (requires a network grant)"
                % (host,)
            )
        return AllowlistedConnection(host, port)


# ---------------------------------------------------------------------------
# Recorded transport (plan.md section 23: "network reads -> recorded/stubbed")
# ---------------------------------------------------------------------------

_MISSING = object()


class RecordedTransport:
    """Recorded request -> response map for rehearsal reads.

    :meth:`request` replays the recorded response for ``key`` (any hashable
    request description) and journals the request in :attr:`requests`.
    Unrecorded keys raise :class:`UnrecordedRequest` (a KeyError) -- rehearsal
    must never silently invent a response.
    """

    def __init__(self, recording=None, *, recorded=None, responses=None,
                 mapping=None, table=None):
        merged = {}
        for part in (recording, recorded, responses, mapping, table):
            if part:
                merged.update(part)
        self.recording = dict(merged)
        self.requests = []

    def request(self, key):
        """Replay the recorded response for ``key`` (else UnrecordedRequest)."""
        self.requests.append(key)
        try:
            return self.recording[key]
        except KeyError:
            raise UnrecordedRequest(
                "no recorded response for request %r" % (key,)
            ) from None

    def read(self, key):
        """Alias of :meth:`request` for read-shaped rehearsal calls."""
        return self.request(key)

    def get(self, key, default=_MISSING):
        """Replay ``key``; return ``default`` when given and unrecorded."""
        if default is _MISSING:
            return self.request(key)
        self.requests.append(key)
        return self.recording.get(key, default)

    def __call__(self, key):
        return self.request(key)


#: Alias for the "RecordedTransport PKR" spelling in the task brief.
RecordedTransportPKR = RecordedTransport


# ---------------------------------------------------------------------------
# Transactional state sandbox (plan.md sections 23-24: forked/transactional DB)
# ---------------------------------------------------------------------------

class StateSandbox:
    """SQLite-backed transactional scratch state.

    Runs in autocommit mode with explicit nesting tracked in-process:
    :meth:`begin` opens a transaction (outer) or a savepoint (nested),
    :meth:`commit`/:meth:`rollback` close the innermost level, and idle
    commit/rollback are harmless no-ops. :meth:`fork` returns an independent
    :class:`StateSandbox` holding a copy of the *committed* state -- an open
    transaction's uncommitted rows are not carried over.
    """

    def __init__(self, path=":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._savepoints = []
        self._sp_counter = 0

    @property
    def depth(self):
        """Current transaction nesting depth (0 = idle)."""
        return len(self._savepoints)

    @property
    def connection(self):
        """The underlying sqlite3 connection (escape hatch for power users)."""
        return self._conn

    def begin(self):
        """Open a transaction (or nested savepoint); return the new depth."""
        if not self._savepoints:
            self._conn.execute("BEGIN")
            self._savepoints.append(None)
        else:
            self._sp_counter += 1
            name = "graygoo_sp_%d" % self._sp_counter
            self._conn.execute('SAVEPOINT "%s"' % name)
            self._savepoints.append(name)
        return self.depth

    def commit(self):
        """Commit the innermost level; idle commit is a no-op (False)."""
        if not self._savepoints:
            return False
        name = self._savepoints.pop()
        if name is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute('RELEASE "%s"' % name)
        return True

    def rollback(self):
        """Roll back the innermost level; idle rollback is a no-op (False)."""
        if not self._savepoints:
            return False
        name = self._savepoints.pop()
        if name is None:
            self._conn.execute("ROLLBACK")
        else:
            self._conn.execute('ROLLBACK TO "%s"' % name)
            self._conn.execute('RELEASE "%s"' % name)
        return True

    def execute(self, sql, parameters=()):
        """Execute one statement; return the cursor (for fetch* calls)."""
        return self._conn.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        """Execute one statement against many parameter rows."""
        return self._conn.executemany(sql, seq_of_parameters)

    def executescript(self, script):
        """Execute a multi-statement SQL script; return the cursor."""
        return self._conn.executescript(script)

    def query(self, sql, parameters=()):
        """Execute a SELECT-style statement; return all rows as a list."""
        return self._conn.execute(sql, parameters).fetchall()

    def fork(self):
        """Return an independent sandbox copy of the committed state."""
        child = StateSandbox(":memory:")
        self._conn.backup(child._conn)
        return child

    def close(self):
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
