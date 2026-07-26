"""Admission regressions for the durable decomposition-depth boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator.decomposition_limits import (
    MAX_DECOMPOSITION_CHILDREN,
    MAX_DECOMPOSITION_DEPTH,
    MAX_DECOMPOSITION_REPLAY_NODES,
)
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionSource,
    legacy_unverified_split_decision,
)
from ouroboros.orchestrator.parallel_executor import (
    ParallelACExecutor,
    _deserialize_composite_completion_result,
    _serialize_composite_completion_result,
)
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from tests.unit.orchestrator.parallel_executor_test_support import ProcessLocalTestExecutor


@pytest.mark.parametrize("invalid_depth", [-1, MAX_DECOMPOSITION_DEPTH + 1, 100, True])
def test_executor_rejects_unpersistable_decomposition_depth_before_effects(
    invalid_depth: object,
) -> None:
    """No live tree may outgrow the shared complete-tree replay projection."""
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

    assert executor._max_decomposition_depth == MAX_DECOMPOSITION_DEPTH == 4


def _complete_five_way_result_tree(
    *,
    depth: int,
    path: str,
    ac_index: int,
) -> ACExecutionResult:
    """Build the full live population admitted at the public maximum."""

    content = f"AC {path}"
    if depth == MAX_DECOMPOSITION_DEPTH:
        return ACExecutionResult(
            ac_index=ac_index,
            ac_content=content,
            success=True,
            final_message=f"completed {path}",
            depth=depth,
        )

    child_paths = tuple(f"{path}.{index}" for index in range(MAX_DECOMPOSITION_CHILDREN))
    children = tuple(
        _complete_five_way_result_tree(
            depth=depth + 1,
            path=child_path,
            ac_index=ac_index * 10 + index + 1,
        )
        for index, child_path in enumerate(child_paths)
    )
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=content,
        success=True,
        final_message=f"completed composite {path}",
        is_decomposed=True,
        sub_results=children,
        depth=depth,
        decomposition_decision=legacy_unverified_split_decision(
            node_id=f"node:{path}",
            source=DecompositionSource.PREFLIGHT,
            child_descriptions=tuple(child.ac_content for child in children),
        ),
    )


def _count_descendants(result: ACExecutionResult) -> int:
    return sum(1 + _count_descendants(child) for child in result.sub_results)


def test_supported_maximum_complete_tree_round_trips_through_durable_replay() -> None:
    """Every effect population admitted at depth four must be persistable."""

    completed = _complete_five_way_result_tree(depth=0, path="root", ac_index=0)

    assert MAX_DECOMPOSITION_REPLAY_NODES == 780
    assert _count_descendants(completed) == MAX_DECOMPOSITION_REPLAY_NODES

    result_data, _decision_data, _fingerprint = _serialize_composite_completion_result(
        completed,
        workspace_root="/tmp/project",
    )
    assert completed.decomposition_decision is not None
    restored = _deserialize_composite_completion_result(
        result_data,
        ac_index=completed.ac_index,
        ac_content=completed.ac_content,
        decomposition_decision=completed.decomposition_decision,
    )

    assert _count_descendants(restored) == MAX_DECOMPOSITION_REPLAY_NODES
    assert max(child.depth for child in _walk_results(restored)) == MAX_DECOMPOSITION_DEPTH


def _walk_results(result: ACExecutionResult) -> tuple[ACExecutionResult, ...]:
    return (result, *(node for child in result.sub_results for node in _walk_results(child)))
