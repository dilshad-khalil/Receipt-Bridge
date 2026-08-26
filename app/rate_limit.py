"""Simple in-memory rate limiting for /print/* endpoints (see
app/main.py's `enforce_rate_limit` dependency).

No external dependency (e.g. Redis) - Print Bridge only ever runs as a
single local process on one machine (see app/main.py's `BridgeServer`), so
a process-local counter is all a token-bucket/sliding-window limiter needs
to be, and it disappears cleanly on restart along with everything else
in-memory.

Sliding-window counter per key: each key keeps a deque of the timestamps of
its recent requests; a request is allowed if fewer than `limit` of them
fall within the trailing 60-second window. Memory is naturally bounded -
old timestamps are trimmed off the front of the deque on every check, and
the number of distinct keys is bounded by the number of distinct callers
(one shared auth token, or a handful of browser Origins), which is small
for a local bridge like this one.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60.0


class RateLimiter:
    """One sliding-window counter per key. Thread-safe - the FastAPI
    dependency using this (see app/main.py) is called concurrently from
    multiple request threads (uvicorn's threaded request handling)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit_per_minute: int) -> tuple[bool, float]:
        """Record one request attempt for `key` and report whether it's
        allowed under `limit_per_minute`.

        :returns: `(allowed, retry_after_seconds)` - `retry_after_seconds`
            is `0.0` when allowed, otherwise how long (rounded up to a
            whole second, since that's what an HTTP `Retry-After` header
            takes) until the oldest request in the current window ages out
            and a new one would be allowed again. A request that is
            *not* allowed is not counted against the window itself, so a
            caller that's already over the limit doesn't dig itself
            deeper by continuing to hammer the endpoint.
        """
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            cutoff = now - WINDOW_SECONDS
            while hits and hits[0] < cutoff:
                hits.popleft()

            if len(hits) >= max(0, limit_per_minute):
                retry_after = hits[0] + WINDOW_SECONDS - now
                return False, max(1.0, round(retry_after + 0.5))

            hits.append(now)
            return True, 0.0


# One shared limiter for the whole process, imported by app.main - a
# module-level singleton (rather than one per create_app() call) so the
# window survives a tray "Restart server" the same way job_log's SQLite
# file does, instead of quietly resetting every caller's count.
limiter = RateLimiter()
