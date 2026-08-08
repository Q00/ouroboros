"""SQLite in-memory URL identity and connection configuration."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any
from urllib.parse import unquote_to_bytes, urlencode
from uuid import uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util import asbool

from ouroboros.core.errors import PersistenceError

_STANDARD_SHARED_MEMORY_TARGET = "file::memory:"
_STANDARD_SHARED_MEMORY_QUERIES = frozenset({"cache=shared&uri=true", "uri=true&cache=shared"})
_CANONICAL_NAMED_MEMDB_PREFIX = "file:/ouroboros-named-"
_CANONICAL_NAMED_MEMDB_QUERIES = frozenset({"uri=true&vfs=memdb", "vfs=memdb&uri=true"})
_EXTERNAL_MODE_MEMORY_QUERY_PARTS = frozenset({"mode=memory", "cache=shared", "uri=true"})
_EXTERNAL_MEMDB_QUERY_PARTS = frozenset({"vfs=memdb", "uri=true"})
_MAX_RESERVED_NAME_DECODE_DEPTH = 8


def _raw_sqlite_target(database_url: str) -> tuple[str, str | None]:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return "", None
    raw_target = database_url[len(prefix) :]
    raw_database, separator, raw_query = raw_target.partition("?")
    return raw_database, raw_query if separator else None


def _has_exact_query_parts(raw_query: str | None, expected: frozenset[str]) -> bool:
    if raw_query is None:
        return False
    parts = raw_query.split("&")
    return len(parts) == len(expected) and frozenset(parts) == expected


def _has_named_memory_query_intent(raw_query: str | None) -> bool:
    """Recognize encoded/case-drifted memory intent so it can fail closed."""
    if raw_query is None:
        return False
    for part in raw_query.split("&"):
        raw_key, separator, raw_value = part.partition("=")
        if not separator:
            continue
        key = unquote_to_bytes(raw_key)
        value = unquote_to_bytes(raw_value)
        # Every successful percent-decoding pass shortens at least one
        # component, so the combined input length is a finite upper bound that
        # reaches a stable spelling without a fixed-depth alias bypass.
        for _ in range(len(key) + len(value) + 1):
            decoded_key = unquote_to_bytes(key)
            decoded_value = unquote_to_bytes(value)
            if decoded_key == key and decoded_value == value:
                break
            key, value = decoded_key, decoded_value
        else:
            # Defensive fail-closed fallback if a future decoder ever violates
            # the strictly shrinking invariant above.
            return True
        if (key.lower(), value.lower()) in ((b"mode", b"memory"), (b"vfs", b"memdb")):
            return True
    return False


def _has_exact_external_named_memory_spelling(database_url: str) -> bool:
    raw_database, raw_query = _raw_sqlite_target(database_url)
    if not raw_database.startswith("file:") or raw_database == "file:":
        return False
    return _has_exact_query_parts(
        raw_query, _EXTERNAL_MODE_MEMORY_QUERY_PARTS
    ) or _has_exact_query_parts(raw_query, _EXTERNAL_MEMDB_QUERY_PARTS)


def _has_exact_standard_shared_memory_spelling(database_url: str) -> bool:
    raw_database, raw_query = _raw_sqlite_target(database_url)
    return (
        raw_database == _STANDARD_SHARED_MEMORY_TARGET
        and raw_query in _STANDARD_SHARED_MEMORY_QUERIES
    )


def _has_exact_canonical_named_memdb_spelling(database_url: str) -> bool:
    raw_database, raw_query = _raw_sqlite_target(database_url)
    identity = raw_database.removeprefix(_CANONICAL_NAMED_MEMDB_PREFIX)
    return (
        raw_database.startswith(_CANONICAL_NAMED_MEMDB_PREFIX)
        and len(identity) == 64
        and all(character in "0123456789abcdef" for character in identity)
        and raw_query in _CANONICAL_NAMED_MEMDB_QUERIES
    )


def _is_standard_shared_memory_uri(database_url: str, parsed: Any) -> bool:
    """Return whether SQLAlchemy parsed SQLite's exact shared-memory URI."""
    return _has_exact_standard_shared_memory_spelling(database_url) and parsed.database == (
        _STANDARD_SHARED_MEMORY_TARGET
    )


def _is_canonical_named_memdb_uri(database_url: str, parsed: Any) -> bool:
    return (
        _has_exact_canonical_named_memdb_spelling(database_url)
        and parsed.database == (database_url.removeprefix("sqlite+aiosqlite:///").partition("?")[0])
    )


def _collides_with_reserved_shared_memory_name(database: str) -> bool:
    candidate = unquote_to_bytes(database)
    for _ in range(_MAX_RESERVED_NAME_DECODE_DEPTH + 1):
        lowered = candidate.lower()
        if lowered.startswith(b"file::memory") or b"\x00" in candidate:
            return True
        decoded = unquote_to_bytes(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def _collides_with_canonical_named_memdb_namespace(database: str) -> bool:
    candidate = unquote_to_bytes(database)
    reserved_prefix = _CANONICAL_NAMED_MEMDB_PREFIX.encode()
    for _ in range(_MAX_RESERVED_NAME_DECODE_DEPTH + 1):
        if candidate.lower().startswith(reserved_prefix):
            return True
        decoded = unquote_to_bytes(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def validate_standard_shared_memory_sqlite_url(database_url: str) -> None:
    """Reject reserved ``file::memory:`` spellings outside the supported contract."""
    parsed = make_url(database_url)
    database = parsed.database or ""
    if _collides_with_reserved_shared_memory_name(database) and not (
        _is_standard_shared_memory_uri(database_url, parsed)
    ):
        raise ValueError(
            "Unsupported SQLite shared-memory URI; use exactly "
            "'file::memory:?cache=shared&uri=true'."
        )


def validate_canonical_named_memdb_sqlite_url(database_url: str) -> None:
    """Reject malformed or meaning-changing uses of the canonical memdb namespace."""
    parsed = make_url(database_url)
    database = parsed.database or ""
    if _collides_with_canonical_named_memdb_namespace(database) and not (
        _is_canonical_named_memdb_uri(database_url, parsed)
    ):
        raise ValueError(
            "Unsupported canonical named-memory SQLite URI; use only 'uri=true&vfs=memdb'."
        )


def validate_external_named_memory_sqlite_url(database_url: str) -> None:
    """Reject named-memory intent that would change meaning during canonicalization."""
    _, raw_query = _raw_sqlite_target(database_url)
    if _has_named_memory_query_intent(raw_query) and not (
        _has_exact_external_named_memory_spelling(database_url)
        or _has_exact_canonical_named_memdb_spelling(database_url)
    ):
        raise ValueError(
            "Unsupported named-memory SQLite URI; use exactly "
            "'mode=memory&cache=shared&uri=true' or 'vfs=memdb&uri=true'."
        )


def is_anonymous_in_memory_sqlite_url(database_url: str) -> bool:
    """Return whether a SQLite URL names an anonymous in-memory database."""
    parsed = make_url(database_url)
    return parsed.database in (None, "", ":memory:")


def sqlite_uri_is_enabled(database_url: str) -> bool:
    """Match SQLAlchemy's boolean coercion for the DBAPI ``uri`` argument."""
    parsed = make_url(database_url)
    value = parsed.query.get("uri", False)
    return bool(asbool(value))


def is_named_memory_sqlite_url(database_url: str) -> bool:
    """Return whether a URL explicitly requests a named in-memory database."""
    parsed = make_url(database_url)
    return parsed.database not in (None, "", ":memory:") and (
        _is_standard_shared_memory_uri(database_url, parsed)
        or _is_canonical_named_memdb_uri(database_url, parsed)
        or _has_exact_external_named_memory_spelling(database_url)
    )


def is_canonical_named_memdb_url(database_url: str) -> bool:
    """Return whether a URL is an internal deterministic named-memdb URI."""
    parsed = make_url(database_url)
    return _is_canonical_named_memdb_uri(database_url, parsed)


def canonicalize_named_memory_sqlite_url(database_url: str) -> str:
    """Map a logical named-memory identity to a deterministic memdb VFS URI."""
    validate_standard_shared_memory_sqlite_url(database_url)
    validate_canonical_named_memdb_sqlite_url(database_url)
    validate_external_named_memory_sqlite_url(database_url)
    parsed = make_url(database_url)
    database = parsed.database or ""
    if database in ("", ":memory:") or not is_named_memory_sqlite_url(database_url):
        return database_url
    if not is_canonical_named_memdb_url(database_url):
        logical_name = unquote_to_bytes(database.removeprefix("file:"))
        if b"\x00" in logical_name:
            raise ValueError("Named in-memory SQLite URLs cannot contain NUL bytes")
        identity_material = (
            b"ouroboros.named-memory.v1\x00" + len(logical_name).to_bytes(8, "big") + logical_name
        )
        identity = hashlib.sha256(identity_material).hexdigest()
        parsed = parsed.set(database=f"{_CANONICAL_NAMED_MEMDB_PREFIX}{identity}")
    parsed = parsed.set(query={"uri": "true", "vfs": "memdb"})
    return parsed.render_as_string(hide_password=False)


def named_memory_keepalive_uri(database_url: str) -> str | None:
    """Return the sqlite3 URI anchoring a canonical named-memory database."""
    parsed = make_url(database_url)
    database = parsed.database or ""
    if database in ("", ":memory:") or not is_canonical_named_memdb_url(database_url):
        return None
    query = {key: value for key, value in parsed.query.items() if key != "uri"}
    return f"{database}?{urlencode(query, doseq=True)}"


def configure_named_memory_engine(
    database_url: str,
    engine_kwargs: dict[str, Any],
) -> sqlite3.Connection | None:
    """Configure pooled named-memdb connections and return their lifetime anchor."""
    keepalive_uri = named_memory_keepalive_uri(database_url)
    if keepalive_uri is None:
        return None
    if sqlite3.sqlite_version_info < (3, 36):
        raise PersistenceError(
            "Named in-memory EventStore URLs require SQLite 3.36 or newer.",
            operation="initialize",
            details={"sqlite_version": sqlite3.sqlite_version},
        )
    engine_kwargs["poolclass"] = AsyncAdaptedQueuePool
    engine_kwargs["pool_size"] = 5
    engine_kwargs["max_overflow"] = 10
    return sqlite3.connect(keepalive_uri, uri=True, check_same_thread=False)


def configure_anonymous_memory_engine(
    database_url: str,
    engine_kwargs: dict[str, Any],
) -> tuple[str, sqlite3.Connection | None]:
    """Give anonymous memory stores isolated transaction-owning connections.

    Modern SQLite uses one unique memdb VFS identity per EventStore. The
    ancient fallback serializes checkout of its one connection; it cannot make
    replacement connections survive cancellation invalidation.
    """
    if sqlite3.sqlite_version_info >= (3, 36):
        shared_name = f"/ouroboros-mem-{uuid4().hex}"
        engine_url = f"sqlite+aiosqlite:///file:{shared_name}?vfs=memdb&uri=true"
        anchor = sqlite3.connect(
            f"file:{shared_name}?vfs=memdb",
            uri=True,
            check_same_thread=False,
        )
        return engine_url, anchor
    engine_kwargs["poolclass"] = AsyncAdaptedQueuePool
    engine_kwargs["pool_size"] = 1
    engine_kwargs["max_overflow"] = 0
    return database_url, None


__all__ = [
    "canonicalize_named_memory_sqlite_url",
    "configure_anonymous_memory_engine",
    "configure_named_memory_engine",
    "is_anonymous_in_memory_sqlite_url",
    "is_canonical_named_memdb_url",
    "is_named_memory_sqlite_url",
    "named_memory_keepalive_uri",
    "sqlite_uri_is_enabled",
    "validate_canonical_named_memdb_sqlite_url",
    "validate_external_named_memory_sqlite_url",
    "validate_standard_shared_memory_sqlite_url",
]
