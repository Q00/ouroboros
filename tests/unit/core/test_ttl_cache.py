"""Tests for the stdlib TTL cache."""

from __future__ import annotations

from unittest.mock import patch

from ouroboros.core.ttl_cache import TTLCache


def test_ttl_cache_expires_entries() -> None:
    cache: TTLCache[str, str] = TTLCache(maxsize=2, ttl=5)

    with patch("ouroboros.core.ttl_cache.time.monotonic", return_value=100.0):
        cache["a"] = "one"

    with patch("ouroboros.core.ttl_cache.time.monotonic", return_value=104.0):
        assert "a" in cache
        assert cache["a"] == "one"

    with patch("ouroboros.core.ttl_cache.time.monotonic", return_value=106.0):
        assert "a" not in cache


def test_ttl_cache_evicts_oldest_live_entry_when_full() -> None:
    cache: TTLCache[str, str] = TTLCache(maxsize=2, ttl=100)
    values = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 3.0])

    def fake_monotonic() -> float:
        return next(values, 3.0)

    with patch("ouroboros.core.ttl_cache.time.monotonic", side_effect=fake_monotonic):
        cache["a"] = "one"
        cache["b"] = "two"
        cache["c"] = "three"
        assert "a" not in cache
        assert cache["b"] == "two"
        assert cache["c"] == "three"
