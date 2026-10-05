"""Append-only canonical event ledger + model-call log.

Implements plan.md sections 41-42 (event ledger, world model),
76 (event schema), and 77 (model call record), plus the
"Build event logging" items from todos.md.

Storage: SQLite (stdlib ``sqlite3``). Hash-chained events so any
post-hoc mutation is detectable via :meth:`EventLedger.verify_chain`.

Append-only by design: this module exposes NO update/delete
functions for ledger rows.
"""

import hashlib
import json
import sqlite3
import time

SCHEMA_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    generation INTEGER,
    task_id TEXT,
    candidate_id TEXT,
    capability_id TEXT,
    capability_version INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    hash TEXT NOT NULL,
    previous_hash TEXT
);
"""

SCHEMA_MODEL_CALLS = """
CREATE TABLE IF NOT EXISTS model_calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    request_timestamp REAL NOT NULL,
    response_timestamp REAL,
    latency_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    context_hash TEXT,
    prompt_version TEXT,
    generation INTEGER,
    candidate_id TEXT,
    result TEXT,
    cost REAL
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_events_generation ON events(generation)",
    "CREATE INDEX IF NOT EXISTS idx_model_calls_generation ON model_calls(generation)",
]


def _canonical(value):
    """Render a field deterministically for hashing (None -> '')."""
    if value is None:
        return ""
    return str(value)


def compute_event_hash(previous_hash, timestamp, event_type, generation,
                       task_id, candidate_id, capability_id,
                       capability_version, payload):
    """Compute the sha256 chain hash for one event row."""
    material = "\x1f".join([
        _canonical(previous_hash),
        _canonical(timestamp),
        _canonical(event_type),
        _canonical(generation),
        _canonical(task_id),
        _canonical(candidate_id),
        _canonical(capability_id),
        _canonical(capability_version),
        _canonical(payload),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_event(row):
    keys = ("event_id", "timestamp", "event_type", "generation", "task_id",
            "candidate_id", "capability_id", "capability_version",
            "payload", "hash", "previous_hash")
    return dict(zip(keys, row))


def _row_to_model_call(row):
    keys = ("call_id", "provider", "model", "request_timestamp",
            "response_timestamp", "latency_ms", "input_tokens",
            "output_tokens", "context_hash", "prompt_version",
            "generation", "candidate_id", "result", "cost")
    return dict(zip(keys, row))


class EventLedger:
    """SQLite-backed append-only event ledger and model-call log."""

    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(SCHEMA_EVENTS)
        self.conn.execute(SCHEMA_MODEL_CALLS)
        for stmt in INDEXES:
            self.conn.execute(stmt)
        self.conn.commit()

    # -- lifecycle --------------------------------------------------
    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- events -----------------------------------------------------
    def append_event(self, event_type, generation=None, task_id=None,
                     candidate_id=None, capability_id=None,
                     capability_version=None, payload=None, timestamp=None):
        """Append one event; returns the stored row as a dict."""
        if not event_type:
            raise ValueError("event_type is required")
        if timestamp is None:
            timestamp = time.time()
        if payload is None:
            payload_text = "{}"
        elif isinstance(payload, str):
            payload_text = payload
        else:
            payload_text = json.dumps(payload, sort_keys=True)
        cur = self.conn.execute(
            "SELECT hash FROM events ORDER BY event_id DESC LIMIT 1")
        last = cur.fetchone()
        previous_hash = last[0] if last else None
        digest = compute_event_hash(
            previous_hash, timestamp, event_type, generation, task_id,
            candidate_id, capability_id, capability_version, payload_text)
        cur = self.conn.execute(
            """INSERT INTO events (timestamp, event_type, generation,
                                   task_id, candidate_id, capability_id,
                                   capability_version, payload, hash,
                                   previous_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, event_type, generation, task_id, candidate_id,
             capability_id, capability_version, payload_text, digest,
             previous_hash))
        self.conn.commit()
        return self.get_event(cur.lastrowid)

    def get_event(self, event_id):
        cur = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = cur.fetchone()
        return _row_to_event(row) if row else None

    def get_events_by_task(self, task_id):
        cur = self.conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY event_id",
            (task_id,))
        return [_row_to_event(r) for r in cur.fetchall()]

    def get_events_by_type(self, event_type):
        cur = self.conn.execute(
            "SELECT * FROM events WHERE event_type = ? ORDER BY event_id",
            (event_type,))
        return [_row_to_event(r) for r in cur.fetchall()]

    def get_recent_events(self, limit=50):
        cur = self.conn.execute(
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,))
        return [_row_to_event(r) for r in cur.fetchall()]

    def verify_chain(self):
        """Recompute every hash/link; True iff the chain is intact."""
        cur = self.conn.execute("SELECT * FROM events ORDER BY event_id")
        expected_prev = None
        for row in cur.fetchall():
            event = _row_to_event(row)
            if event["previous_hash"] != expected_prev:
                return False
            recomputed = compute_event_hash(
                event["previous_hash"], event["timestamp"],
                event["event_type"], event["generation"],
                event["task_id"], event["candidate_id"],
                event["capability_id"], event["capability_version"],
                event["payload"])
            if recomputed != event["hash"]:
                return False
            expected_prev = event["hash"]
        return True

    # -- model calls ------------------------------------------------
    def log_model_call(self, provider, model, input_tokens=None,
                       output_tokens=None, context_hash=None,
                       prompt_version=None, generation=None,
                       candidate_id=None, result=None, cost=None,
                       latency_ms=None, request_timestamp=None,
                       response_timestamp=None):
        """Append one model-call record; returns the stored row as a dict."""
        if not provider:
            raise ValueError("provider is required")
        if not model:
            raise ValueError("model is required")
        if request_timestamp is None:
            request_timestamp = time.time()
        if (latency_ms is None and response_timestamp is not None):
            latency_ms = (response_timestamp - request_timestamp) * 1000.0
        cur = self.conn.execute(
            """INSERT INTO model_calls (provider, model, request_timestamp,
                                        response_timestamp, latency_ms,
                                        input_tokens, output_tokens,
                                        context_hash, prompt_version,
                                        generation, candidate_id, result,
                                        cost)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (provider, model, request_timestamp, response_timestamp,
             latency_ms, input_tokens, output_tokens, context_hash,
             prompt_version, generation, candidate_id, result, cost))
        self.conn.commit()
        return self.get_model_call(cur.lastrowid)

    def get_model_call(self, call_id):
        cur = self.conn.execute(
            "SELECT * FROM model_calls WHERE call_id = ?", (call_id,))
        row = cur.fetchone()
        return _row_to_model_call(row) if row else None

    def list_model_calls(self, limit=100, generation=None):
        if generation is None:
            cur = self.conn.execute(
                "SELECT * FROM model_calls ORDER BY call_id DESC LIMIT ?",
                (limit,))
        else:
            cur = self.conn.execute(
                """SELECT * FROM model_calls WHERE generation = ?
                   ORDER BY call_id DESC LIMIT ?""",
                (generation, limit))
        return [_row_to_model_call(r) for r in cur.fetchall()]


def init_ledger(path=":memory:"):
    """Open (creating if needed) the ledger store at *path*."""
    return EventLedger(path)
