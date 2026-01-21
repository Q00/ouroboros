"""Simple migration runner for SQLite.

This module provides a basic migration system for applying SQL scripts
to the database in order. Tracks applied migrations to avoid re-running.
"""

import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).parent / "scripts"

# SQL for migration tracking table
CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS _migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


async def _get_applied_migrations(engine: AsyncEngine) -> set[str]:
    """Get set of already applied migration names.

    Args:
        engine: SQLAlchemy async engine.

    Returns:
        Set of applied migration names.
    """
    async with engine.begin() as conn:
        # Ensure migrations table exists
        await conn.execute(text(CREATE_MIGRATIONS_TABLE))

        # Get applied migrations
        result = await conn.execute(text("SELECT name FROM _migrations"))
        return {row[0] for row in result.fetchall()}


async def _read_migration_file(migration_file: Path) -> str:
    """Read migration file content using asyncio.to_thread to avoid blocking.

    Args:
        migration_file: Path to the migration SQL file.

    Returns:
        Content of the migration file.
    """
    return await asyncio.to_thread(migration_file.read_text)


async def run_migrations(engine: AsyncEngine) -> list[str]:
    """Run all pending migrations.

    Migrations are SQL files in the scripts/ directory, named with a
    numeric prefix (e.g., 001_initial.sql). They are executed in order.
    Already applied migrations are tracked and skipped.

    Note: This is a simple migration system. For production, consider
    using Alembic.

    Args:
        engine: SQLAlchemy async engine.

    Returns:
        List of newly applied migration names.
    """
    applied: list[str] = []

    # Get already applied migrations
    already_applied = await _get_applied_migrations(engine)

    # Get all .sql files sorted by name
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    for migration_file in migration_files:
        if migration_file.name in already_applied:
            continue

        # Read file content using asyncio.to_thread to avoid blocking
        sql_content = await _read_migration_file(migration_file)

        async with engine.begin() as conn:
            # Split by semicolon and execute each statement
            for statement in sql_content.split(";"):
                statement = statement.strip()
                if statement and not statement.startswith("--"):
                    await conn.execute(text(statement))

            # Record this migration as applied
            await conn.execute(
                text("INSERT INTO _migrations (name) VALUES (:name)"),
                {"name": migration_file.name},
            )

        applied.append(migration_file.name)

    return applied
