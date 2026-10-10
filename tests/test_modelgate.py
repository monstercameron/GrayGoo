"""Tests for the adaptive model-call gate (modelgate.py). Stdlib only.

All timing-sensitive behaviour runs on a fake clock; the only real waits are
short thread hand-offs (well under 5 seconds for the whole file).
"""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import modelgate  # noqa: E402
from modelgate import ModelGate  # noqa: E402


class FakeClock(object):
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeStatusError(Exception):
    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class FakeResponse(object):
    def __init__(self, headers):
        self.headers = headers


class UnstringableError(Exception):
    def __str__(self):
        raise RuntimeError("no text for you")


class ConcurrencyTest(unittest.TestCase):
    def test_never_more_than_limit_bodies_run_at_once(self):
        gate = ModelGate(start=3, ceiling=8, clock=FakeClock())
        lock = threading.Lock()
        seen = []

        def body():
            with gate.slot():
                with lock:
                    seen.append(gate.active)
                time.sleep(0.02)

        threads = [threading.Thread(target=body) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(seen), 8)
        self.assertLessEqual(max(seen), 3)
        self.assertLessEqual(gate.peak, 3)
        self.assertGreaterEqual(gate.peak, 2)
        self.assertEqual(gate.active, 0)

    def test_slot_releases_when_body_raises(self):
        gate = ModelGate(clock=FakeClock())
        with self.assertRaises(ValueError):
            with gate.slot():
                self.assertEqual(gate.active, 1)
                raise ValueError("boom")
        self.assertEqual(gate.active, 0)
        self.assertEqual(gate.peak, 1)


class ThrottleTest(unittest.TestCase):
    def test_throttle_halves_and_floors_at_one(self):
        clock = FakeClock()
        gate = ModelGate(start=4, ceiling=8, floor=1, clock=clock)
        self.assertEqual(gate.throttled(0), 2)
        clock.now += 2
        self.assertEqual(gate.throttled(0), 1)
        clock.now += 2
        self.assertEqual(gate.throttled(0), 1)
        self.assertEqual(gate.limit, 1)
        self.assertEqual(gate.throttles, 3)

    def test_burst_halves_once_but_extends_pause_to_longest_wait(self):
        clock = FakeClock()
        gate = ModelGate(start=4, clock=clock)
        self.assertEqual(gate.throttled(2.0), 2)
        self.assertEqual(gate.throttled(5.0), 2)
        self.assertEqual(gate.limit, 2)
        self.assertEqual(gate.throttles, 2)
        self.assertEqual(gate.snapshot()["paused_s"], 5.0)

    def test_burst_pause_keeps_longest_wait_in_either_order(self):
        clock = FakeClock()
        gate = ModelGate(start=4, clock=clock)
        gate.throttled(5.0)
        gate.throttled(2.0)
        self.assertEqual(gate.snapshot()["paused_s"], 5.0)


class PauseTest(unittest.TestCase):
    def test_pause_blocks_new_slots_until_deadline(self):
        clock = FakeClock()
        gate = ModelGate(start=4, clock=clock)
        gate.throttled(5)
        entered = threading.Event()

        def body():
            with gate.slot():
                entered.set()

        t = threading.Thread(target=body, daemon=True)
        t.start()
        time.sleep(0.15)
        self.assertFalse(entered.is_set())
        self.assertGreater(gate.snapshot()["paused_s"], 0.0)

        clock.now += 6
        self.assertTrue(entered.wait(timeout=1.0))
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(gate.snapshot()["paused_s"], 0.0)


class GrowthTest(unittest.TestCase):
    def test_growth_waits_for_quiet_period_then_steps_by_one(self):
        clock = FakeClock()
        gate = ModelGate(start=2, ceiling=4, floor=1, grow_after=3,
                         quiet_s=20.0, burst_s=1.0, clock=clock)
        gate.throttled(0)
        self.assertEqual(gate.limit, 1)

        clock.now += 10
        for _ in range(3):
            gate.succeeded()
        self.assertEqual(gate.limit, 1, "must not grow inside quiet_s")

        clock.now += 15  # 25 s since the throttle, past quiet_s
        for _ in range(3):
            gate.succeeded()
        self.assertEqual(gate.limit, 2, "grows by exactly one")

        for _ in range(3):
            gate.succeeded()
        self.assertEqual(gate.limit, 3)

    def test_growth_never_exceeds_ceiling(self):
        clock = FakeClock()
        gate = ModelGate(start=3, ceiling=4, grow_after=2, quiet_s=0.0,
                         clock=clock)
        for _ in range(50):
            gate.succeeded()
        self.assertEqual(gate.limit, 4)


class ResetAndSnapshotTest(unittest.TestCase):
    def test_reset_restores_start_values(self):
        clock = FakeClock()
        gate = ModelGate(start=4, ceiling=8, grow_after=3, quiet_s=20.0,
                         clock=clock)
        gate.throttled(10)
        self.assertEqual(gate.limit, 2)
        clock.now += 30
        gate.succeeded()
        gate.succeeded()
        gate.reset()

        snap = gate.snapshot()
        self.assertEqual(snap["limit"], 4)
        self.assertEqual(snap["peak"], 0)
        self.assertEqual(snap["throttles"], 0)
        self.assertEqual(snap["paused_s"], 0.0)
        self.assertEqual(snap["active"], 0)

        # Success count was cleared: two more successes must not grow the
        # limit (a leftover count of 2 would have reached grow_after=3).
        gate.succeeded()
        gate.succeeded()
        self.assertEqual(gate.limit, 4)

    def test_snapshot_reports_pause_remaining_rounded(self):
        clock = FakeClock()
        gate = ModelGate(start=4, clock=clock)
        gate.throttled(3.0)
        clock.now += 0.004
        self.assertEqual(gate.snapshot()["paused_s"], 3.0)
        clock.now += 1.0
        self.assertEqual(gate.snapshot()["paused_s"], 2.0)


class ClassificationTest(unittest.TestCase):
    def test_status_429_is_rate_limited_and_transient(self):
        exc = FakeStatusError("slow down", status_code=429)
        self.assertTrue(modelgate.rate_limited(exc))
        self.assertTrue(modelgate.transient(exc))

    def test_429_text_is_rate_limited(self):
        exc = RuntimeError("Error code: 429 - too_many_requests_error")
        self.assertTrue(modelgate.rate_limited(exc))
        self.assertTrue(modelgate.transient(exc))

    def test_generate_is_not_rate_limited_or_transient(self):
        exc = RuntimeError("failed to generate reply")
        self.assertFalse(modelgate.rate_limited(exc))
        self.assertFalse(modelgate.transient(exc))

    def test_timeout_is_transient_but_not_rate_limited(self):
        exc = RuntimeError("Request timed out.")
        self.assertFalse(modelgate.rate_limited(exc))
        self.assertTrue(modelgate.transient(exc))

    def test_server_statuses_are_transient_not_rate_limited(self):
        exc = FakeStatusError("upstream", status_code=503)
        self.assertFalse(modelgate.rate_limited(exc))
        self.assertTrue(modelgate.transient(exc))

    def test_client_error_is_neither(self):
        exc = FakeStatusError("bad request", status_code=400)
        self.assertFalse(modelgate.rate_limited(exc))
        self.assertFalse(modelgate.transient(exc))

    def test_unstringable_exception_does_not_raise(self):
        exc = UnstringableError()
        self.assertFalse(modelgate.rate_limited(exc))
        self.assertFalse(modelgate.transient(exc))

    def test_retry_after_parses_and_clamps(self):
        def err(headers):
            return FakeStatusError("x", response=FakeResponse(headers))

        self.assertEqual(modelgate.retry_after(err({"retry-after": "7"}), 5),
                         7.0)
        self.assertEqual(modelgate.retry_after(err({"retry-after": "0.2"}), 5),
                         1.0)
        self.assertEqual(modelgate.retry_after(err({"Retry-After": "900"}), 5),
                         60.0)

    def test_retry_after_falls_back_to_default(self):
        bad = FakeStatusError("x", response=FakeResponse({"retry-after": "soon"}))
        self.assertEqual(modelgate.retry_after(bad, 5), 5.0)
        none = RuntimeError("no response at all")
        result = modelgate.retry_after(none, 3)
        self.assertIsInstance(result, float)
        self.assertEqual(result, 3.0)

    def test_module_shared_instance_exists(self):
        self.assertIsInstance(modelgate.GATE, ModelGate)


if __name__ == "__main__":
    unittest.main()
