"""Admission regressions for the durable decomposition-depth boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.coordinator import LevelCoordinator, _collect_file_modifications
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
    _VerifyGateOutcome,
)
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from tests.unit.orchestrator.parallel_executor_test_support import ProcessLocalTestExecutor


@pytest.mark.parametrize("invalid_depth", [-1, True])
def test_executor_rejects_invalid_decomposition_depth_before_effects(
    invalid_depth: object,
) -> None:
    """The historical public boundary accepts integers but not negative/bool values."""
    adapter = MagicMock()

    with pytest.raises(ValueError, match="non-negative integer"):
        ProcessLocalTestExecutor(
            adapter=adapter,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
            max_decomposition_depth=invalid_depth,  # type: ignore[arg-type]
        )

    adapter.execute_task.assert_not_called()


def test_executor_accepts_larger_legacy_depth_without_durable_routing_claim() -> None:
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        max_decomposition_depth=MAX_DECOMPOSITION_DEPTH + 1,
    )

    assert executor._max_decomposition_depth == 5
    assert executor._durable_decomposition_replay_enabled is False
    assert executor._bounded_route_escalation_enabled is False


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
    write_file_per_leaf: bool = False,
) -> ACExecutionResult:
    """Build the full live population admitted at the public maximum."""

    content = f"AC {path}"
    if depth == MAX_DECOMPOSITION_DEPTH:
        return ACExecutionResult(
            ac_index=ac_index,
            ac_content=content,
            success=True,
            messages=(
                AgentMessage(
                    type="tool",
                    content=f"wrote {path}",
                    tool_name="Write",
                    data={"tool_input": {"file_path": f"/tmp/project/leaf-{path}.py"}},
                ),
            )
            if write_file_per_leaf
            else (),
            final_message=f"completed {path}",
            depth=depth,
        )

    child_paths = tuple(f"{path}.{index}" for index in range(MAX_DECOMPOSITION_CHILDREN))
    children = tuple(
        _complete_five_way_result_tree(
            depth=depth + 1,
            path=child_path,
            ac_index=ac_index * 10 + index + 1,
            write_file_per_leaf=write_file_per_leaf,
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


def test_depth_four_complete_tree_preserves_all_625_leaf_conflict_paths() -> None:
    """Node-local projections avoid the former 512-path aggregate overflow."""

    completed = _complete_five_way_result_tree(
        depth=0,
        path="root",
        ac_index=0,
        write_file_per_leaf=True,
    )
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

    file_to_acs: dict[str, set[int]] = {}
    _collect_file_modifications(restored, file_to_acs)
    shared_path = "/tmp/project/leaf-root.0.0.0.0.py"
    other_writer = ACExecutionResult(
        ac_index=1,
        ac_content="touch one deep leaf",
        success=True,
        messages=(
            AgentMessage(
                type="tool",
                content="edited shared leaf",
                tool_name="Edit",
                data={"tool_input": {"file_path": shared_path}},
            ),
        ),
    )
    live_conflicts = LevelCoordinator.detect_file_conflicts([completed, other_writer])
    replay_conflicts = LevelCoordinator.detect_file_conflicts([restored, other_writer])

    assert len(file_to_acs) == MAX_DECOMPOSITION_CHILDREN**MAX_DECOMPOSITION_DEPTH == 625
    assert all(ac_indices == {0} for ac_indices in file_to_acs.values())
    assert restored.conflict_files == ()
    assert all(
        len(node.conflict_files or ()) == 1
        for node in _walk_results(restored)
        if not node.sub_results
    )
    assert live_conflicts == replay_conflicts
    assert [(conflict.file_path, conflict.ac_indices) for conflict in replay_conflicts] == [
        (shared_path, (0, 1))
    ]


def test_composite_replay_preserves_all_admitted_free_form_result_evidence() -> None:
    """Post-effect sealing must not invent smaller bounds than producer contracts."""

    long_error = "e" * 4_001
    long_session_id = "provider-" + "s" * 2_048
    missing_artifacts = tuple(f"missing-{index}.txt" for index in range(129))
    children = (
        ACExecutionResult(ac_index=1, ac_content="first", success=True, depth=1),
        ACExecutionResult(
            ac_index=2,
            ac_content="second",
            success=False,
            error=long_error,
            session_id=long_session_id,
            depth=1,
            outcome=ACExecutionOutcome.FAILED,
        ),
    )
    decision = legacy_unverified_split_decision(
        node_id="node:root",
        source=DecompositionSource.PREFLIGHT,
        child_descriptions=tuple(child.ac_content for child in children),
    )
    verify_outcome = _VerifyGateOutcome(
        passed=False,
        reason="r" * 2_001,
        output_tail="",
        missing_artifacts=missing_artifacts,
    )
    completed = ACExecutionResult(
        ac_index=0,
        ac_content="root",
        success=False,
        error=long_error,
        session_id=long_session_id,
        is_decomposed=True,
        sub_results=children,
        outcome=ACExecutionOutcome.FAILED,
        verify_gate_outcome=verify_outcome,
        decomposition_decision=decision,
    )

    result_data, _decision_data, _fingerprint = _serialize_composite_completion_result(
        completed,
        workspace_root="/tmp/project",
    )
    restored = _deserialize_composite_completion_result(
        result_data,
        ac_index=completed.ac_index,
        ac_content=completed.ac_content,
        decomposition_decision=decision,
    )

    assert restored.error == long_error
    assert restored.session_id == long_session_id
    assert restored.verify_gate_outcome == verify_outcome
    assert restored.sub_results[1].error == long_error
    assert restored.sub_results[1].session_id == long_session_id


def _walk_results(result: ACExecutionResult) -> tuple[ACExecutionResult, ...]:
    return (result, *(node for child in result.sub_results for node in _walk_results(child)))
