"""Short-lived in-process cache for the dashboard's heavy analytics queries.

Momentum and Insights aggregate the whole 30-day video_stats_history table on
every request (engagement_activity alone is ~4s on production-sized data and
no index can help it: the cost is computing snapshot-to-snapshot deltas across
every row). The underlying rows only change when a stats poller writes, every
30 minutes by default, so recomputing per page view is pure waste. Entries
live for config.ANALYTICS_CACHE_SECONDS.

Gunicorn runs the dashboard as one process with a few threads, so a module
dict is shared by every request. A per-key lock makes concurrent misses wait
for the one thread that is computing instead of each re-running the same 4s
query -- the "requests taking 100+ seconds under concurrency on a 2-vCPU box"
failure mode described in db.video_stats_growth. Values are deep-copied on the
way in and out because the routes annotate the returned rows in place.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Callable, Hashable, TypeVar

from app import config

T = TypeVar("T")

_MAX_ENTRIES = 256
_registry_lock = threading.Lock()
_entries: dict[Hashable, tuple[float, object]] = {}
_key_locks: dict[Hashable, threading.Lock] = {}


def cached(key: Hashable, compute: Callable[[], T], *, ttl: float | None = None) -> T:
    """Return compute()'s result, reusing a copy stored under key for ttl seconds."""
    ttl = config.ANALYTICS_CACHE_SECONDS if ttl is None else ttl
    if ttl <= 0:
        return compute()
    with _registry_lock:
        hit = _entries.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return copy.deepcopy(hit[1])
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        with _registry_lock:
            hit = _entries.get(key)
            if hit is not None and hit[0] > time.monotonic():
                return copy.deepcopy(hit[1])
        value = compute()
        with _registry_lock:
            if len(_entries) >= _MAX_ENTRIES:
                _evict_locked()
            _entries[key] = (time.monotonic() + ttl, copy.deepcopy(value))
        return value


def _evict_locked() -> None:
    now = time.monotonic()
    for stale in [k for k, (expires, _) in _entries.items() if expires <= now]:
        del _entries[stale]
        _key_locks.pop(stale, None)
    if len(_entries) >= _MAX_ENTRIES:
        oldest = min(_entries, key=lambda k: _entries[k][0])
        del _entries[oldest]
        _key_locks.pop(oldest, None)


def clear() -> None:
    """Drop every entry. Tests call this so one test's rows never serve another."""
    with _registry_lock:
        _entries.clear()
        _key_locks.clear()


def size() -> int:
    with _registry_lock:
        return len(_entries)
