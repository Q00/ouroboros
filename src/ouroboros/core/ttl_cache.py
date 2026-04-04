"""Small stdlib-only TTL cache.

This replaces cachetools.TTLCache for the handful of semantics used in this
codebase: membership checks, item lookup, assignment, clearing, and max-size
eviction.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
import time


class TTLCache[K, V](MutableMapping[K, V]):
    """A small TTL cache with FIFO eviction among live items."""

    def __init__(
        self,
        *,
        maxsize: int,
        ttl: float,
    ) -> None:
        if maxsize <= 0:
            msg = "maxsize must be > 0"
            raise ValueError(msg)
        if ttl <= 0:
            msg = "ttl must be > 0"
            raise ValueError(msg)

        self.maxsize = maxsize
        self.ttl = ttl
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def _expires_at(self) -> float:
        return time.monotonic() + self.ttl

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired_keys = [key for key, (expires_at, _) in self._entries.items() if expires_at <= now]
        for key in expired_keys:
            self._entries.pop(key, None)

    def __getitem__(self, key: K) -> V:
        self._purge_expired()
        expires_at, value = self._entries[key]
        if expires_at <= time.monotonic():
            self._entries.pop(key, None)
            raise KeyError(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        self._purge_expired()
        if key in self._entries:
            self._entries.pop(key)
        elif len(self._entries) >= self.maxsize:
            self._entries.popitem(last=False)
        self._entries[key] = (self._expires_at(), value)

    def __delitem__(self, key: K) -> None:
        del self._entries[key]

    def __iter__(self) -> Iterator[K]:
        self._purge_expired()
        return iter(self._entries)

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        self._purge_expired()
        return key in self._entries

    def clear(self) -> None:
        self._entries.clear()
