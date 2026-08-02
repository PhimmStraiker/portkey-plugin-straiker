"""Per-session event dedup.

Claude Code resends the entire transcript on every model call, so without this a single
prompt and every prior tool call would be rescored on each of the 4-30 calls a task fans
into. Dedup is therefore not an optimisation; it is what keeps one developer action from
producing dozens of duplicate Console events.

It is also the main latency lever: a claim that returns False costs one dict lookup and
skips the detect call entirely, which is the common case deep into a session.

In-memory is correct for a single-instance demo. For the bolt-on deployment, swap in
Redis behind the same ``claim`` signature so the state survives restarts and scales
across replicas.
"""

from __future__ import annotations

import time
from threading import Lock


class TTLDedup:
    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 100_000) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[str, float] = {}
        self._lock = Lock()

    def claim(self, session_id: str, dedup_key: str) -> bool:
        """Claim an event for emission. True on first sight, False if already emitted."""
        key = f"{session_id}:{dedup_key}"
        now = time.monotonic()
        with self._lock:
            expires = self._entries.get(key)
            if expires is not None and expires > now:
                return False
            if len(self._entries) >= self._max_entries:
                self._evict(now)
            self._entries[key] = now + self._ttl
            return True

    def _evict(self, now: float) -> None:
        live = {key: expires for key, expires in self._entries.items() if expires > now}
        self._entries = live if len(live) < self._max_entries else {}
