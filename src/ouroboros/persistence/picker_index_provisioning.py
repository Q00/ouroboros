"""Best-effort writable provisioning for dashboard picker indexes."""

from __future__ import annotations

import logging

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from ouroboros.persistence.picker_indexes import (
    PICKER_INDEX_DDL,
    PICKER_INDEX_DDL_BY_NAME,
    PICKER_INDEX_NAMES,
    normalize_index_ddl,
)


def provision_picker_indexes(connection: Connection) -> None:
    """Repair/install the versioned picker index contract transactionally."""
    placeholders = ",".join("?" for _ in PICKER_INDEX_NAMES)
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        f"AND lower(name) IN ({placeholders})",
        PICKER_INDEX_NAMES,
    ).fetchall()
    installed = {str(row[0]).lower(): (str(row[0]), row[1]) for row in rows}
    for name, expected_sql in PICKER_INDEX_DDL_BY_NAME.items():
        actual_name, actual_sql = installed.get(name, (name, None))
        if isinstance(actual_sql, str) and normalize_index_ddl(actual_sql) != normalize_index_ddl(
            expected_sql
        ):
            connection.exec_driver_sql(f'DROP INDEX "{actual_name}"')
    for statement in PICKER_INDEX_DDL:
        connection.exec_driver_sql(statement)


async def provision_picker_indexes_best_effort(
    engine: AsyncEngine,
    logger: logging.Logger,
) -> None:
    """Provision optional indexes without blocking durable writer startup."""
    try:
        async with engine.begin() as connection:
            await connection.run_sync(provision_picker_indexes)
    except OperationalError as exc:
        logger.warning("Dashboard picker index provisioning deferred: %s", exc)


__all__ = ["provision_picker_indexes", "provision_picker_indexes_best_effort"]
