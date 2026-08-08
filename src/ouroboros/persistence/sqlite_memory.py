"""SQLite in-memory URL identity and connection configuration."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any
from urllib.parse import unquote_to_bytes, urlencode
from uuid import uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.pool import AsyncAdaptedQueuePool

from ouroboros.core.errors import PersistenceError

_STANDARD_SHARED_MEMORY_TARGET = "file::memory:"
_STANDARD_SHARED_MEMORY_QUERIES = frozenset({"cache=shared&uri=true", "uri=true&cache=shared"})
_CANONICAL_NAMED_MEMDB_PREFIX = "file:/ouroboros-named-"
_CANONICAL_NAMED_MEMDB_QUERIES = frozenset({"uri=true&vfs=memdb", "vfs=memdb&uri=true"})
_MAX_RESERVED_NAME_DECODE_DEPTH = 8


def _has_exact_standard_shared_memory_spelling(database_url: str) -> bool:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return False
    raw_target = database_url[len(prefix) :]
    raw_database, separator, raw_query = raw_target.partition("?")
    return (
        raw_database == _STANDARD_SHARED_MEMORY_TARGET
        and separator == "?"
        and raw_query in _STANDARD_SHARED_MEMORY_QUERIES
    )


def _has_exact_canonical_named_memdb_spelling(database_url: str) -> bool:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return False
    raw_target = database_url[len(prefix) :]
    raw_database, separator, raw_query = raw_target.partition("?")
    identity = raw_database.removeprefix(_CANONICAL_NAMED_MEMDB_PREFIX)
    return (
        raw_database.startswith(_CANONICAL_NAMED_MEMDB_PREFIX)
        and len(identity) == 64
        and all(character in "0123456789abcdef" for character in identity)
        and separator == "?"
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


def is_anonymous_in_memory_sqlite_url(database_url: str) -> bool:
    """Return whether a SQLite URL names an anonymous in-memory database."""
    parsed = make_url(database_url)
    return parsed.database in (None, "", ":memory:")


def is_named_memory_sqlite_url(database_url: str) -> bool:
    """Return whether a URL explicitly requests a named in-memory database."""
    parsed = make_url(database_url)
    return parsed.database not in (None, "", ":memory:") and (
        _is_standard_shared_memory_uri(database_url, parsed)
        or str(parsed.query.get("mode", "")) == "memory"
        or str(parsed.query.get("vfs", "")) == "memdb"
    )


def is_canonical_named_memdb_url(database_url: str) -> bool:
    """Return whether a URL is an internal deterministic named-memdb URI."""
    parsed = make_url(database_url)
    return _is_canonical_named_memdb_uri(database_url, parsed)


def canonicalize_named_memory_sqlite_url(database_url: str) -> str:
    """Map a logical named-memory identity to a deterministic memdb VFS URI."""
    validate_standard_shared_memory_sqlite_url(database_url)
    validate_canonical_named_memdb_sqlite_url(database_url)
    parsed = make_url(database_url)
    database = parsed.database or ""
    mode = str(parsed.query.get("mode", ""))
    vfs = str(parsed.query.get("vfs", ""))
    if database in ("", ":memory:") or (
        not _is_standard_shared_memory_uri(database_url, parsed)
        and mode != "memory"
        and vfs != "memdb"
    ):
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
    "validate_canonical_named_memdb_sqlite_url",
    "validate_standard_shared_memory_sqlite_url",
]
