"""Tests for process-level effect isolation (effects.py). Stdlib only."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import effects


PLAN_SECTION_22_EFFECTS = frozenset({
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


class EffectGrantTest(unittest.TestCase):
    def test_effect_vocabulary_matches_plan_section_22(self):
        self.assertEqual(effects.EFFECTS, PLAN_SECTION_22_EFFECTS)

    def test_allowed_is_stored_as_frozenset(self):
        grant = effects.EffectGrant(["pure", "pure", "clock"])
        self.assertIsInstance(grant.allowed, frozenset)
        self.assertEqual(grant.allowed, frozenset({"pure", "clock"}))

    def test_declared_effect_passes_check(self):
        grant = effects.EffectGrant(frozenset({"filesystem-read"}))
        self.assertTrue(grant.check("filesystem-read"))
        self.assertTrue(grant.granted("filesystem-read"))
        self.assertIn("filesystem-read", grant)

    def test_undeclared_effect_denied_with_name(self):
        grant = effects.EffectGrant(frozenset({"filesystem-read"}))
        with self.assertRaises(effects.EffectDenied) as ctx:
            grant.check("network-write")
        self.assertIn("network-write", str(ctx.exception))
        self.assertFalse(grant.granted("network-write"))

    def test_empty_grant_denies_everything(self):
        grant = effects.EffectGrant()
        for name in PLAN_SECTION_22_EFFECTS:
            with self.assertRaises(effects.EffectDenied):
                grant.check(name)


class OverlayFSTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fs = effects.OverlayFS(os.path.join(self.tmp.name, "overlay"))

    def test_write_read_round_trip_bytes_and_text(self):
        self.fs.write("blob.bin", b"\x00\xffbinary")
        self.assertEqual(self.fs.read("blob.bin"), b"\x00\xffbinary")
        self.fs.write("sub/note.txt", "hello overlay")
        self.assertEqual(self.fs.read("sub/note.txt"), b"hello overlay")
        self.assertEqual(self.fs.read_text("sub/note.txt"), "hello overlay")
        self.assertTrue(self.fs.exists("sub/note.txt"))
        self.assertFalse(self.fs.exists("sub/missing.txt"))

    def test_dotdot_read_escape_rejected(self):
        attempts = [
            "..",
            "../evil.txt",
            "sub/../../evil.txt",
            os.path.join("sub", "..", "..", "evil.txt"),
        ]
        for attempt in attempts:
            with self.assertRaises(ValueError, msg=attempt):
                self.fs.read(attempt)
            with self.assertRaises(effects.EffectDenied, msg=attempt):
                self.fs.read(attempt)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp.name, "evil.txt")))

    def test_dotdot_write_escape_rejected(self):
        with self.assertRaises(ValueError):
            self.fs.write("../escape.txt", b"nope")
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp.name, "escape.txt")))

    def test_absolute_path_outside_root_rejected(self):
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "wb") as handle:
            handle.write(b"canonical")
        with self.assertRaises(ValueError):
            self.fs.read(outside)
        with self.assertRaises(ValueError):
            self.fs.write(outside, b"pwned")
        with open(outside, "rb") as handle:
            self.assertEqual(handle.read(), b"canonical")

    def test_absolute_path_inside_root_allowed(self):
        target = os.path.join(self.fs.root, "inner.txt")
        self.fs.write(target, b"in")
        self.assertEqual(self.fs.read(target), b"in")

    def test_diff_reports_only_in_root_writes(self):
        before = self.fs.snapshot()
        self.assertEqual(before, {})
        self.fs.write("kept.txt", b"v1")
        self.fs.write("sub/nested.txt", "text")
        with self.assertRaises(ValueError):
            self.fs.write("../escape.txt", b"nope")
        self.assertEqual(self.fs.diff(before), {
            "kept.txt": "added",
            "sub/nested.txt": "added",
        })
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp.name, "escape.txt")))
        mid = self.fs.snapshot()
        self.fs.write("kept.txt", b"v2")
        os.remove(self.fs.resolve("sub/nested.txt"))
        self.assertEqual(self.fs.diff(mid), {
            "kept.txt": "modified",
            "sub/nested.txt": "deleted",
        })
        self.assertEqual(self.fs.diff(mid), self.fs.diff(dict(mid)))


class DenyNetworkTest(unittest.TestCase):
    def test_connect_refused_by_default(self):
        net = effects.DenyNetwork()
        with self.assertRaises(effects.NetworkDenied):
            net.connect("example.com", 80)
        # A denied dial-out is also a denied effect, and a connection error.
        with self.assertRaises(effects.EffectDenied):
            net.connect("anything.test")
        with self.assertRaises(ConnectionError):
            net.connect("anything.test")
        with self.assertRaises(effects.NetworkDenied):
            net.connect(None)

    def test_allowlisted_test_double_permitted(self):
        net = effects.DenyNetwork(allow={"stub.internal"})
        conn = net.connect("stub.internal", 9999)
        self.assertEqual(conn.host, "stub.internal")
        self.assertEqual(conn.port, 9999)
        conn.close()
        with self.assertRaises(effects.NetworkDenied):
            net.connect("other.internal")

    def test_allowlist_spellings_and_runtime_add(self):
        net = effects.DenyNetwork(
            allowed_hosts=["a.test"], allowlist=["b.test"])
        net.allow_host("c.test")
        for host in ("a.test", "b.test", "c.test"):
            self.assertEqual(net.connect(host).host, host)
        with self.assertRaises(effects.NetworkDenied):
            net.connect("d.test")


class RecordedTransportTest(unittest.TestCase):
    def test_replays_recorded_responses(self):
        key = ("GET", "/users/7")
        response = {"id": 7, "name": "ada"}
        transport = effects.RecordedTransport({key: response})
        self.assertEqual(transport.request(key), response)
        self.assertEqual(transport.read(key), response)
        self.assertEqual(transport(key), response)
        self.assertEqual(transport.requests, [key, key, key])

    def test_unrecorded_request_raises_key_error(self):
        transport = effects.RecordedTransport({"ping": "pong"})
        with self.assertRaises(KeyError) as ctx:
            transport.request("missing-key")
        self.assertIn("missing-key", str(ctx.exception))

    def test_get_with_default(self):
        transport = effects.RecordedTransport({"ping": "pong"})
        self.assertEqual(transport.get("ping"), "pong")
        self.assertEqual(transport.get("absent", default=None), None)

    def test_pkr_alias_is_same_class(self):
        self.assertIs(effects.RecordedTransportPKR, effects.RecordedTransport)


class StateSandboxTest(unittest.TestCase):
    def setUp(self):
        self.db = effects.StateSandbox()
        self.addCleanup(self.db.close)
        self.db.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")

    def names(self, db=None):
        return [row[0] for row in (db or self.db).query(
            "SELECT name FROM items ORDER BY id")]

    def test_no_raw_connection_escape_hatch(self):
        # Issue 79: the raw sqlite3 connection must not be exposed;
        # external COMMIT/ROLLBACK would desync savepoint bookkeeping.
        self.assertFalse(hasattr(self.db, "connection"))

    def test_rollback_leaves_no_trace(self):
        self.db.begin()
        self.db.execute("INSERT INTO items (name) VALUES (?)", ("ghost",))
        self.assertEqual(self.names(), ["ghost"])
        self.db.rollback()
        self.assertEqual(self.names(), [])
        self.assertEqual(self.db.depth, 0)

    def test_commit_persists(self):
        self.db.begin()
        self.db.execute("INSERT INTO items (name) VALUES (?)", ("kept",))
        self.db.commit()
        self.assertEqual(self.names(), ["kept"])
        self.assertEqual(self.db.depth, 0)

    def test_nested_begin_rolls_back_to_savepoint(self):
        self.db.begin()
        self.db.execute("INSERT INTO items (name) VALUES (?)", ("outer",))
        self.db.begin()
        self.db.execute("INSERT INTO items (name) VALUES (?)", ("inner",))
        self.assertEqual(self.names(), ["outer", "inner"])
        self.db.rollback()
        self.assertEqual(self.db.depth, 1)
        self.db.commit()
        self.assertEqual(self.names(), ["outer"])

    def test_idle_commit_and_rollback_are_noops(self):
        self.assertFalse(self.db.commit())
        self.assertFalse(self.db.rollback())
        self.assertEqual(self.db.depth, 0)

    def test_fork_isolates_branches(self):
        self.db.begin()
        self.db.execute("INSERT INTO items (name) VALUES (?)", ("base",))
        self.db.commit()
        branch = self.db.fork()
        self.addCleanup(branch.close)
        branch.begin()
        branch.execute(
            "INSERT INTO items (name) VALUES (?)", ("from-branch",))
        branch.commit()
        self.db.begin()
        self.db.execute(
            "INSERT INTO items (name) VALUES (?)", ("from-parent",))
        self.db.commit()
        self.assertEqual(self.names(), ["base", "from-parent"])
        self.assertEqual(self.names(branch), ["base", "from-branch"])


class FailedExperimentTest(unittest.TestCase):
    """End-to-end: a failed experiment leaves canonical state byte-identical."""

    def _snapshot_dir(self, root):
        snap = {}
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                with open(full, "rb") as handle:
                    snap[os.path.relpath(full, root)] = handle.read()
        return snap

    def _read_ledger_rows(self, db_path):
        # NOTE: `with sqlite3.connect(...)` does NOT close the connection
        # (context exit only commits/rolls back); conn<->cursor cycles can
        # then keep the file locked on Windows past cleanup. Close explicitly.
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT * FROM ledger ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_failed_experiment_leaves_canonical_state_byte_identical(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        canonical_dir = os.path.join(tmp.name, "canonical")
        os.makedirs(os.path.join(canonical_dir, "sub"))
        with open(os.path.join(canonical_dir, "app.cfg"), "wb") as handle:
            handle.write(b"mode=prod\n")
        with open(os.path.join(canonical_dir, "sub", "data.bin"), "wb") as fh:
            fh.write(bytes(range(256)))
        canonical_db = os.path.join(tmp.name, "canonical.db")
        seed = sqlite3.connect(canonical_db)
        try:
            seed.execute(
                "CREATE TABLE ledger (id INTEGER PRIMARY KEY, entry TEXT)")
            seed.executemany(
                "INSERT INTO ledger (entry) VALUES (?)",
                [("e1",), ("e2",)])
            seed.commit()
        finally:
            seed.close()

        dir_before = self._snapshot_dir(canonical_dir)
        with open(canonical_db, "rb") as handle:
            db_bytes_before = handle.read()
        rows_before = self._read_ledger_rows(canonical_db)

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        overlay = effects.OverlayFS(os.path.join(scratch.name, "overlay"))
        proof = {}

        def failed_experiment():
            # All speculative writes go to the overlay + a forked branch.
            overlay.write("scratch/attempt.txt", b"speculative junk")
            sandbox = effects.StateSandbox(canonical_db)
            try:
                branch = sandbox.fork()
                try:
                    branch.begin()
                    branch.execute(
                        "INSERT INTO ledger (entry) VALUES (?)",
                        ("X-corrupt",))
                    branch.commit()
                    proof["branch_rows"] = branch.query(
                        "SELECT entry FROM ledger ORDER BY id")
                finally:
                    branch.close()
            finally:
                sandbox.close()
            proof["overlay_diff"] = overlay.diff({})
            raise RuntimeError("boom: candidate failed verification")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            failed_experiment()

        # The experiment really ran inside the sandbox ...
        self.assertEqual(
            proof["overlay_diff"], {"scratch/attempt.txt": "added"})
        self.assertIn(("X-corrupt",), proof["branch_rows"])
        # ... and canonical state is byte-identical afterwards.
        self.assertEqual(self._snapshot_dir(canonical_dir), dir_before)
        with open(canonical_db, "rb") as handle:
            self.assertEqual(handle.read(), db_bytes_before)
        self.assertEqual(self._read_ledger_rows(canonical_db), rows_before)


if __name__ == "__main__":
    unittest.main()
