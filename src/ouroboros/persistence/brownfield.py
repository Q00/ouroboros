"""BrownfieldStore for managing brownfield repository registrations.

Provides async CRUD operations for brownfield repos using SQLAlchemy Core
with aiosqlite backend. Follows the same patterns as EventStore.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ouroboros.core.errors import PersistenceError
from ouroboros.persistence.schema import brownfield_repos_table, metadata


@dataclass(frozen=True)
class BrownfieldRepo:
    """Immutable representation of a registered brownfield repository.

    This class replaces the old ``BrownfieldEntry`` from
    ``ouroboros.bigbang.brownfield`` and provides the same
    ``to_dict()`` / ``from_dict()`` interface for backward compatibility.
    """

    path: str
    name: str
    desc: str | None = None
    is_default: bool = False
    id: int | None = None  # SQLite rowid

    # ── Serialization (backward-compat with BrownfieldEntry) ─────

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain dict for API / template consumption."""
        d = {
            "path": self.path,
            "name": self.name,
            "desc": self.desc or "",
            "is_default": self.is_default,
        }
        if self.id is not None:
            d["id"] = self.id
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrownfieldRepo:
        """Create a BrownfieldRepo from a dict.

        Accepts the same dict shape as the old ``BrownfieldEntry.from_dict()``.

        Args:
            data: Dictionary with at least ``path`` and ``name`` keys.

        Returns:
            Validated BrownfieldRepo.

        Raises:
            ValueError: If required keys are missing or empty.
        """
        required = {"path", "name"}
        missing = required - set(data.keys())
        if missing:
            raise ValueError(f"Missing required keys: {sorted(missing)}")

        path = str(data["path"]).strip()
        name = str(data["name"]).strip()

        if not path:
            raise ValueError("'path' must not be empty")
        if not name:
            raise ValueError("'name' must not be empty")

        return cls(
            path=path,
            name=name,
            desc=str(data.get("desc", "")) or None,
            is_default=bool(data.get("is_default", False)),
        )

    # ── Database row mapping ─────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict) -> BrownfieldRepo:
        """Create a BrownfieldRepo from a database row mapping."""
        return cls(
            path=row["path"],
            name=row["name"],
            desc=row.get("desc"),
            is_default=bool(row.get("is_default", False)),
            id=row.get("rowid") or row.get("id"),
        )


class BrownfieldStore:
    """Store for managing brownfield repository registrations.

    Uses SQLAlchemy Core with aiosqlite for async database operations.
    All operations are transactional for atomicity.

    Usage:
        store = BrownfieldStore("sqlite+aiosqlite:///ouroboros.db")
        await store.initialize()

        # Register a repo
        await store.register("/path/to/repo", "my-repo", "A cool project")

        # List all repos
        repos = await store.list()

        # Get default repo
        default = await store.get_default()

        # Set single default (clears others)
        await store.set_single_default("/path/to/repo")

        # Remove a repo
        await store.remove("/path/to/repo")

        await store.close()
    """

    def __init__(self, database_url: str | None = None) -> None:
        """Initialize BrownfieldStore with database URL.

        Args:
            database_url: SQLAlchemy database URL.
                         For async SQLite: "sqlite+aiosqlite:///path/to/db.sqlite"
                         If not provided, defaults to ~/.ouroboros/ouroboros.db
        """
        if database_url is None:
            db_path = Path.home() / ".ouroboros" / "ouroboros.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+aiosqlite:///{db_path}"
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

    @classmethod
    def from_engine(cls, engine: AsyncEngine) -> BrownfieldStore:
        """Create a BrownfieldStore from an existing async engine.

        Useful when sharing a database connection with EventStore.

        Args:
            engine: An existing SQLAlchemy AsyncEngine instance.

        Returns:
            A BrownfieldStore that uses the provided engine.
        """
        store = cls.__new__(cls)
        store._database_url = str(engine.url)
        store._engine = engine
        return store

    async def initialize(self) -> None:
        """Initialize the database connection and create tables if needed.

        This method is idempotent - calling it multiple times is safe.
        """
        if self._engine is None:
            self._engine = create_async_engine(
                self._database_url,
                echo=False,
            )

        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        # Apply any pending migrations for schema evolution
        from ouroboros.persistence.migrations.runner import run_migrations

        await run_migrations(self._engine)

    def _ensure_initialized(self, operation: str) -> AsyncEngine:
        """Check that the store is initialized and return the engine.

        Raises:
            PersistenceError: If the store is not initialized.
        """
        if self._engine is None:
            raise PersistenceError(
                "BrownfieldStore not initialized. Call initialize() first.",
                operation=operation,
            )
        return self._engine

    async def register(
        self,
        path: str,
        name: str,
        desc: str | None = None,
        is_default: bool = False,
    ) -> BrownfieldRepo:
        """Register a brownfield repository.

        If a repo with the same path already exists, it is updated (upsert).
        If is_default is True, the target repo is marked as default WITHOUT
        clearing other defaults (multi-default is supported).

        Args:
            path: Absolute filesystem path to the repository.
            name: Human-readable repository name.
            desc: One-line description of the repository.
            is_default: Whether this repo should be the default brownfield context.

        Returns:
            The registered BrownfieldRepo.

        Raises:
            PersistenceError: If the operation fails.
        """
        engine = self._ensure_initialized("register")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                # Check if repo already exists
                existing = await conn.execute(
                    select(literal_column("rowid"), t).where(t.c.path == path)
                )
                row = existing.mappings().first()

                if row is not None:
                    # Existing repo — only update fields that were explicitly provided
                    updates: dict[str, Any] = {"name": name}
                    if desc is not None:
                        updates["desc"] = desc
                    if is_default:
                        updates["is_default"] = True
                    await conn.execute(update(t).where(t.c.path == path).values(**updates))

                    # Return with preserved values
                    result_row = (
                        (
                            await conn.execute(
                                select(literal_column("rowid"), t).where(t.c.path == path)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    return BrownfieldRepo.from_row(dict(result_row))  # type: ignore[arg-type]

                # New repo — insert
                await conn.execute(
                    t.insert().values(
                        path=path,
                        name=name,
                        desc=desc,
                        is_default=is_default,
                        registered_at=datetime.now(UTC),
                    )
                )

            return BrownfieldRepo(path=path, name=name, desc=desc, is_default=is_default)
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(
                f"Failed to register brownfield repo: {e}",
                operation="insert",
                table="brownfield_repos",
                details={"path": path, "name": name},
            ) from e

    async def bulk_register(
        self,
        repos: list[dict[str, str]],
    ) -> int:
        """Bulk-insert scanned repositories (is_default=False, desc='').

        Each dict must have ``path`` and ``name`` keys.  Existing rows
        with the same ``path`` are replaced (SQLite ``INSERT OR REPLACE``).

        Args:
            repos: List of ``{"path": ..., "name": ...}`` dicts from scan.

        Returns:
            Number of rows inserted/replaced.

        Raises:
            PersistenceError: If the operation fails.
        """
        if not repos:
            return 0

        engine = self._ensure_initialized("bulk_register")
        t = brownfield_repos_table

        try:
            registered = 0
            async with engine.begin() as conn:
                for r in repos:
                    # Check if already exists — preserve metadata
                    existing = await conn.execute(select(t).where(t.c.path == r["path"]))
                    if existing.first() is not None:
                        # Already registered — skip to preserve desc/is_default
                        continue
                    await conn.execute(
                        t.insert().values(
                            path=r["path"],
                            name=r["name"],
                            desc="",
                            is_default=False,
                            registered_at=datetime.now(UTC),
                        )
                    )
                    registered += 1
            return registered
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(
                f"Failed to bulk register brownfield repos: {e}",
                operation="bulk_insert",
                table="brownfield_repos",
                details={"count": len(repos)},
            ) from e

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> Sequence[BrownfieldRepo]:
        """List registered brownfield repositories with optional pagination.

        Args:
            offset: Number of rows to skip (default 0).
            limit: Maximum number of rows to return. ``None`` means no limit.

        Returns:
            List of BrownfieldRepo instances, ordered by name.

        Raises:
            PersistenceError: If the query fails.
        """
        engine = self._ensure_initialized("list")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                stmt = select(literal_column("rowid"), t).order_by(literal_column("rowid"))
                if offset > 0:
                    stmt = stmt.offset(offset)
                if limit is not None:
                    stmt = stmt.limit(limit)
                result = await conn.execute(stmt)
                rows = result.mappings().all()
                return [BrownfieldRepo.from_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to list brownfield repos: {e}",
                operation="select",
                table="brownfield_repos",
            ) from e

    async def count(self) -> int:
        """Return the total number of registered brownfield repositories.

        Useful together with :meth:`list` for pagination metadata.

        Raises:
            PersistenceError: If the query fails.
        """
        engine = self._ensure_initialized("count")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                from sqlalchemy import func

                result = await conn.execute(select(func.count()).select_from(t))
                return result.scalar() or 0
        except Exception as e:
            raise PersistenceError(
                f"Failed to count brownfield repos: {e}",
                operation="select",
                table="brownfield_repos",
            ) from e

    async def get_default(self) -> BrownfieldRepo | None:
        """Get the default brownfield repository.

        Returns:
            The default BrownfieldRepo, or None if no default is set.

        Raises:
            PersistenceError: If the query fails.
        """
        engine = self._ensure_initialized("get_default")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    select(literal_column("rowid"), t)
                    .where(t.c.is_default.is_(True))
                    .order_by(t.c.path)
                    .limit(1)
                )
                row = result.mappings().first()
                if row is None:
                    return None
                return BrownfieldRepo.from_row(dict(row))
        except Exception as e:
            raise PersistenceError(
                f"Failed to get default brownfield repo: {e}",
                operation="select",
                table="brownfield_repos",
            ) from e

    async def get_defaults(self) -> Sequence[BrownfieldRepo]:
        """Get all brownfield repositories marked as default.

        Unlike :meth:`get_default` which returns only the first match,
        this returns every repo with ``is_default=True`` — needed for
        multi-default support.

        Returns:
            List of default BrownfieldRepo instances (may be empty).

        Raises:
            PersistenceError: If the query fails.
        """
        engine = self._ensure_initialized("get_defaults")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    select(literal_column("rowid"), t)
                    .where(t.c.is_default.is_(True))
                    .order_by(t.c.path)
                )
                rows = result.mappings().all()
                return [BrownfieldRepo.from_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get default brownfield repos: {e}",
                operation="select",
                table="brownfield_repos",
            ) from e

    async def set_single_default(self, path: str) -> BrownfieldRepo | None:
        """Set a repository as the sole default brownfield context.

        Clears the default flag on **all** other repos and sets it on the
        specified repo.  Use this only when exactly one default is desired
        (e.g. the legacy CLI ``set_default_repo`` flow).  For multi-default
        support, use :meth:`update_is_default` instead.

        The repo must already be registered.

        Args:
            path: Absolute filesystem path of the repo to set as default.

        Returns:
            The updated BrownfieldRepo, or None if the path is not registered.

        Raises:
            PersistenceError: If the operation fails.
        """
        engine = self._ensure_initialized("set_single_default")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                # Validate the target path exists BEFORE clearing defaults
                check = await conn.execute(select(t.c.path).where(t.c.path == path))
                if check.first() is None:
                    return None
                # Clear all defaults (only after validation)
                await conn.execute(
                    update(t).where(t.c.is_default.is_(True)).values(is_default=False)
                )
                # Set the new default
                result = await conn.execute(
                    update(t).where(t.c.path == path).values(is_default=True)
                )

                # Fetch and return the updated row
                result = await conn.execute(
                    select(literal_column("rowid"), t).where(t.c.path == path)
                )
                row = result.mappings().first()
                if row is None:
                    return None
                return BrownfieldRepo.from_row(dict(row))
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(
                f"Failed to set default brownfield repo: {e}",
                operation="update",
                table="brownfield_repos",
                details={"path": path},
            ) from e

    async def update_is_default(self, path: str, *, is_default: bool) -> BrownfieldRepo | None:
        """Update is_default for a single repo WITHOUT clearing others."""
        engine = self._ensure_initialized("update_is_default")
        t = brownfield_repos_table
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    update(t).where(t.c.path == path).values(is_default=is_default)
                )
                if result.rowcount == 0:
                    return None
                row = (
                    (await conn.execute(select(literal_column("rowid"), t).where(t.c.path == path)))
                    .mappings()
                    .first()
                )
                return BrownfieldRepo.from_row(dict(row)) if row else None
        except Exception as e:
            raise PersistenceError(
                f"Failed to update is_default: {e}",
                operation="update",
                table="brownfield_repos",
                details={"path": path},
            ) from e

    async def set_defaults_by_ids(self, ids: Sequence[int]) -> Sequence[BrownfieldRepo]:
        """Replace all defaults with repos matching the given rowids.

        Clears all existing defaults, then sets is_default=True for the
        specified rowids. Returns the new default repos.
        """
        engine = self._ensure_initialized("set_defaults_by_ids")
        t = brownfield_repos_table
        try:
            async with engine.begin() as conn:
                # Validate all requested rowids exist BEFORE mutating state
                if ids:
                    for rid in ids:
                        check = await conn.execute(
                            select(t.c.path).where(literal_column("rowid") == rid)
                        )
                        if check.first() is None:
                            raise PersistenceError(
                                f"Rowid {rid} does not exist in brownfield_repos",
                                operation="set_defaults",
                                table="brownfield_repos",
                                details={"invalid_rowid": rid},
                            )
                # Clear all defaults (only after validation passes)
                await conn.execute(
                    update(t).where(t.c.is_default.is_(True)).values(is_default=False)
                )
                # Set new defaults by rowid
                if ids:
                    for rid in ids:
                        await conn.execute(
                            update(t).where(literal_column("rowid") == rid).values(is_default=True)
                        )
                # Return new defaults
                result = await conn.execute(
                    select(literal_column("rowid"), t)
                    .where(t.c.is_default.is_(True))
                    .order_by(t.c.name)
                )
                rows = result.mappings().all()
                return [BrownfieldRepo.from_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to set defaults by ids: {e}",
                operation="update",
                table="brownfield_repos",
                details={"ids": ids},
            ) from e

    async def update_desc(self, path: str, desc: str) -> BrownfieldRepo | None:
        """Update the description of a registered repository.

        Args:
            path: Absolute filesystem path of the repo to update.
            desc: New one-line description.

        Returns:
            The updated BrownfieldRepo, or None if the path is not registered.

        Raises:
            PersistenceError: If the operation fails.
        """
        engine = self._ensure_initialized("update_desc")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                result = await conn.execute(update(t).where(t.c.path == path).values(desc=desc))
                if result.rowcount == 0:
                    return None

                result = await conn.execute(
                    select(literal_column("rowid"), t).where(t.c.path == path)
                )
                row = result.mappings().first()
                if row is None:
                    return None
                return BrownfieldRepo.from_row(dict(row))
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(
                f"Failed to update brownfield repo desc: {e}",
                operation="update",
                table="brownfield_repos",
                details={"path": path},
            ) from e

    async def remove(self, path: str) -> bool:
        """Remove a brownfield repository registration.

        Args:
            path: Absolute filesystem path of the repo to remove.

        Returns:
            True if a repo was removed, False if no repo with that path existed.

        Raises:
            PersistenceError: If the operation fails.
        """
        engine = self._ensure_initialized("remove")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                result = await conn.execute(delete(t).where(t.c.path == path))
                return result.rowcount > 0
        except Exception as e:
            raise PersistenceError(
                f"Failed to remove brownfield repo: {e}",
                operation="delete",
                table="brownfield_repos",
                details={"path": path},
            ) from e

    async def clear_all(self) -> int:
        """Delete all rows from the brownfield_repos table.

        Used by ``ooo setup`` to reset the table before a fresh re-scan.

        Returns:
            Number of rows deleted.

        Raises:
            PersistenceError: If the operation fails.
        """
        engine = self._ensure_initialized("clear_all")
        t = brownfield_repos_table

        try:
            async with engine.begin() as conn:
                result = await conn.execute(delete(t))
                return result.rowcount
        except Exception as e:
            raise PersistenceError(
                f"Failed to clear brownfield repos: {e}",
                operation="delete",
                table="brownfield_repos",
            ) from e

    async def close(self) -> None:
        """Close the database connection."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
