"""Admission regressions for the durable decomposition-depth boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator.parallel_executor import (
    MAX_DECOMPOSITION_DEPTH,
    ParallelACExecutor,
)
from tests.unit.orchestrator.parallel_executor_test_support import ProcessLocalTestExecutor


@pytest.mark.parametrize("invalid_depth", [-1, 3, 100, True])
def test_executor_rejects_unpersistable_decomposition_depth_before_effects(
    invalid_depth: object,
) -> None:
    """No live tree may outgrow the complete 64-node replay projection."""
    adapter = MagicMock()

    with pytest.raises(ValueError, match="completed trees remain replayable"):
        ProcessLocalTestExecutor(
            adapter=adapter,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
            max_decomposition_depth=invalid_depth,  # type: ignore[arg-type]
        )

    adapter.execute_task.assert_not_called()


def test_executor_accepts_the_persistable_maximum_depth() -> None:
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        max_decomposition_depth=MAX_DECOMPOSITION_DEPTH,
    )

    assert executor._max_decomposition_depth == 2
