"""Adaptive concurrency gate for live model API calls.

ModelGate lets several model calls run at once. When the API answers HTTP 429
(rate limited) the caller reports it with ``throttled(wait_s)``: the number of
calls allowed in flight is halved and no new call starts until the pause ends.
After a quiet period of successful calls the limit creeps back up by one.

Typical use at a live call site::

    with GATE.slot():
        try:
            reply = client.chat.completions.create(...)
        except Exception as exc:
            if rate_limited(exc):
                GATE.throttled(retry_after(exc, default=5.0))
            raise
        GATE.succeeded()

The module-level functions classify exceptions from OpenAI-SDK-style clients
by duck typing (``status_code``, ``response.headers``, message text). The SDK
is never imported.

Stdlib only. Thread-safe: every counter is guarded by one Condition.
"""

import math
import threading
import time

_POLL_S = 0.05

_RATE_MARKERS = ("429", "rate limit", "rate_limit", "ratelimit", "too_many",
                 "too many requests", "quota", "queue_exceeded",
                 "high traffic")
_TRANSIENT_STATUS = (408, 409, 500, 502, 503, 504, 529)
_TRANSIENT_MARKERS = ("timed out", "timeout", "502", "503", "504",
                      "overloaded", "connection", "temporarily unavailable",
                      "traffic", "queue")


class _Slot(object):
    """Context manager returned by ModelGate.slot()."""

    def __init__(self, gate):
        self._gate = gate

    def __enter__(self):
        self._gate._acquire()
        return None

    def __exit__(self, exc_type, exc, tb):
        self._gate._release()
        return False


class ModelGate(object):
    """Limits concurrent model calls and adapts the limit to 429 responses."""

    def __init__(self, start=4, ceiling=8, floor=1, grow_after=8,
                 quiet_s=20.0, burst_s=1.0, clock=time.monotonic):
        self._floor = max(1, int(floor))
        self._ceiling = max(self._floor, int(ceiling))
        self._start = min(self._ceiling, max(self._floor, int(start)))
        self._grow_after = max(1, int(grow_after))
        self._quiet_s = float(quiet_s)
        self._burst_s = float(burst_s)
        self._clock = clock
        self._cond = threading.Condition()
        self._active = 0
        self._limit = self._start
        self._peak = 0
        self._throttles = 0
        self._successes = 0
        self._pause_until = None
        self._last_throttle = None

    # -- read-only views -------------------------------------------------

    @property
    def limit(self):
        with self._cond:
            return self._limit

    @property
    def active(self):
        with self._cond:
            return self._active

    @property
    def peak(self):
        with self._cond:
            return self._peak

    @property
    def throttles(self):
        with self._cond:
            return self._throttles

    # -- concurrency slot ------------------------------------------------

    def slot(self):
        """Return a context manager that holds one call slot."""
        return _Slot(self)

    def _acquire(self):
        with self._cond:
            while True:
                now = self._clock()
                paused = (self._pause_until is not None
                          and now < self._pause_until)
                if not paused and self._active < self._limit:
                    break
                self._cond.wait(timeout=_POLL_S)
            self._active += 1
            if self._active > self._peak:
                self._peak = self._active

    def _release(self):
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    # -- feedback from calls ---------------------------------------------

    def succeeded(self):
        """Record one successful call; maybe raise the limit by one."""
        with self._cond:
            self._successes = min(self._successes + 1, self._grow_after)
            now = self._clock()
            quiet = (self._last_throttle is None
                     or now - self._last_throttle >= self._quiet_s)
            if (self._successes >= self._grow_after and quiet
                    and self._limit < self._ceiling):
                self._limit += 1
                self._successes = 0
                self._cond.notify_all()

    def throttled(self, wait_s):
        """Record a rate-limit answer; return the new limit.

        The pause deadline only ever moves later. The limit is halved once per
        burst: a throttle arriving within burst_s of the previous one extends
        the pause but does not halve again.
        """
        with self._cond:
            now = self._clock()
            deadline = now + max(0.0, float(wait_s))
            if self._pause_until is None or deadline > self._pause_until:
                self._pause_until = deadline
            in_burst = (self._last_throttle is not None
                        and now - self._last_throttle < self._burst_s)
            if not in_burst:
                self._limit = max(self._floor, self._limit // 2)
            self._throttles += 1
            self._successes = 0
            self._last_throttle = now
            self._cond.notify_all()
            return self._limit

    # -- inspection and reset --------------------------------------------

    def snapshot(self):
        """Return the current state as a plain dict."""
        with self._cond:
            now = self._clock()
            left = 0.0
            if self._pause_until is not None:
                left = max(0.0, self._pause_until - now)
            return {
                "limit": self._limit,
                "active": self._active,
                "peak": self._peak,
                "throttles": self._throttles,
                "paused_s": round(left, 2),
            }

    def reset(self):
        """Return to the constructor's start values. `active` is kept."""
        with self._cond:
            self._limit = self._start
            self._peak = 0
            self._throttles = 0
            self._successes = 0
            self._pause_until = None
            self._last_throttle = None
            self._cond.notify_all()


def _status(exc):
    try:
        return getattr(exc, "status_code", None)
    except Exception:
        return None


def _text(exc):
    try:
        return str(exc).lower()
    except Exception:
        return ""


def rate_limited(exc):
    """True when the exception says the API rate-limited the request."""
    if _status(exc) == 429:
        return True
    text = _text(exc)
    return any(marker in text for marker in _RATE_MARKERS)


def transient(exc):
    """True when retrying the same request later may succeed."""
    if rate_limited(exc):
        return True
    if _status(exc) in _TRANSIENT_STATUS:
        return True
    text = _text(exc)
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _header(exc, names):
    try:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    except Exception:
        return None
    if headers is None:
        return None
    for name in names:
        try:
            value = headers.get(name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def retry_after(exc, default):
    """Seconds to wait before retrying, from a Retry-After header.

    A numeric header is clamped into [1.0, 60.0]. A missing, non-numeric or
    non-finite header yields float(default).
    """
    value = _header(exc, ("retry-after", "Retry-After"))
    if value is not None:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None and math.isfinite(seconds):
            return min(60.0, max(1.0, seconds))
    return float(default)


GATE = ModelGate()
