"""Task numbering stays consistent across report sections."""

from __future__ import annotations

import json

import pytest

from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionResult,
    ParallelExecutionResult,
)
from ouroboros.orchestrator.parallel_reports import render_parallel_verification_report


@pytest.mark.parametrize("indices", [(4,), (4, 1)])
def test_depth_warning_paths_use_task_indices_for_partial_or_reordered_reports(
    indices: tuple[int, ...],
) -> None:
    results = tuple(
        ACExecutionResult(
            ac_index=index,
            ac_content=f"task {index + 1}",
            success=True,
            decomposition_depth_warning=True,
        )
        for index in indices
    )
    report = render_parallel_verification_report(
        ParallelExecutionResult(results=results, success_count=len(results), failure_count=0),
        total_acceptance_criteria=5,
    )
    metadata_line = next(
        line.removeprefix("Feedback Metadata JSON: ")
        for line in report.splitlines()
        if line.startswith("Feedback Metadata JSON: ")
    )
    paths = json.loads(metadata_line)["feedback_metadata"][0]["details"]["affected_ac_paths"]

    assert paths == [str(index + 1) for index in indices]


def test_nested_warning_path_retains_child_number_under_actual_task_index() -> None:
    result = ACExecutionResult(
        ac_index=4,
        ac_content="task 5",
        success=True,
        is_decomposed=True,
        sub_results=(
            ACExecutionResult(
                ac_index=400,
                ac_content="child 1",
                success=True,
                decomposition_depth_warning=True,
            ),
        ),
    )
    report = render_parallel_verification_report(
        ParallelExecutionResult(results=(result,), success_count=1, failure_count=0),
        total_acceptance_criteria=5,
    )

    assert '"affected_ac_paths": ["5.1"]' in report
