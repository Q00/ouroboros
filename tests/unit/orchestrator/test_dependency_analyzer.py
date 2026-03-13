"""Unit tests for structured AC dependency analysis."""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.orchestrator.dependency_analyzer import (
    ACDependencySpec,
    ACNode,
    ACSharedRuntimeResource,
    DependencyAnalyzer,
    DependencyGraph,
    ExecutionPlanningError,
    ExecutionStage,
    HybridExecutionPlanner,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


class StubLLMAdapter:
    """Minimal LLM stub for dependency analyzer tests."""

    def __init__(self, content: str | None = None, error: ProviderError | None = None) -> None:
        self._content = content
        self._error = error

    async def complete(
        self, messages: list[Any], config: Any
    ) -> Result[CompletionResponse, ProviderError]:
        if self._error is not None:
            return Result.err(self._error)

        return Result.ok(
            CompletionResponse(
                content=self._content or '{"dependencies": []}',
                model="test-model",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def _empty_dependency_response(ac_count: int) -> str:
    items = ",".join(f'{{"ac_index": {index}, "depends_on": []}}' for index in range(ac_count))
    return f'{{"dependencies": [{items}]}}'


class TestDependencyAnalyzer:
    """Tests for the structured dependency analyzer."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        (
            "specs",
            "expected_dependencies",
            "expected_levels",
            "expected_independent",
            "expected_serialized",
        ),
        [
            pytest.param(
                (
                    ACDependencySpec(
                        index=0, content="Create runtime scaffolding", metadata={"id": "runtime"}
                    ),
                    ACDependencySpec(
                        index=1, content="Add session persistence", prerequisites=("runtime",)
                    ),
                    ACDependencySpec(
                        index=2, content="Implement resume flow", prerequisites=("AC 2",)
                    ),
                ),
                {0: (), 1: (0,), 2: (1,)},
                ((0,), (1,), (2,)),
                (0,),
                (1, 2),
                id="fully-serial",
            ),
            pytest.param(
                (
                    ACDependencySpec(index=0, content="Document adapter lifecycle"),
                    ACDependencySpec(index=1, content="Outline permission handling"),
                    ACDependencySpec(index=2, content="Summarize audit events"),
                ),
                {0: (), 1: (), 2: ()},
                ((0, 1, 2),),
                (0, 1, 2),
                (),
                id="fully-parallel",
            ),
            pytest.param(
                (
                    ACDependencySpec(
                        index=0,
                        content="Create OpenCode session runtime",
                        metadata={"id": "runtime"},
                    ),
                    ACDependencySpec(
                        index=1, content="Add streaming bridge", prerequisites=("runtime",)
                    ),
                    ACDependencySpec(
                        index=2, content="Add tool normalization", prerequisites=("runtime",)
                    ),
                    ACDependencySpec(
                        index=3,
                        content="Reconcile hybrid execution",
                        prerequisites=("AC 2", "AC 3"),
                    ),
                ),
                {0: (), 1: (0,), 2: (0,), 3: (1, 2)},
                ((0,), (1, 2), (3,)),
                (0,),
                (1, 2, 3),
                id="mixed-hybrid",
            ),
        ],
    )
    async def test_analyze_infers_graph_shapes_for_serial_parallel_and_mixed_cases(
        self,
        specs: tuple[ACDependencySpec, ...],
        expected_dependencies: dict[int, tuple[int, ...]],
        expected_levels: tuple[tuple[int, ...], ...],
        expected_independent: tuple[int, ...],
        expected_serialized: tuple[int, ...],
    ) -> None:
        analyzer = DependencyAnalyzer(
            llm_adapter=StubLLMAdapter(_empty_dependency_response(len(specs)))
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == expected_levels
        assert graph.independent_indices == expected_independent
        assert graph.serialized_indices == expected_serialized
        for ac_index, expected in expected_dependencies.items():
            assert graph.get_dependencies(ac_index) == expected

    @pytest.mark.asyncio
    async def test_analyze_uses_prerequisites_and_metadata_dependencies(self) -> None:
        analyzer = DependencyAnalyzer(llm_adapter=StubLLMAdapter(_empty_dependency_response(3)))
        specs = (
            ACDependencySpec(index=0, content="Create data model", metadata={"id": "model"}),
            ACDependencySpec(index=1, content="Build API", prerequisites=("model",)),
            ACDependencySpec(index=2, content="Update docs", metadata={"depends_on": ["AC 2"]}),
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == ((0,), (1,), (2,))
        assert graph.independent_indices == (0,)
        assert graph.serialized_indices == (1, 2)
        assert graph.get_dependencies(1) == (0,)
        assert graph.get_dependencies(2) == (1,)
        assert graph.get_node(1).serialization_reasons == ("prerequisite AC 1",)
        assert graph.get_node(2).serialization_reasons == ("metadata dependency on AC 2",)

    @pytest.mark.asyncio
    async def test_analyze_uses_context_for_explicit_ordering_and_shared_prerequisites(
        self,
    ) -> None:
        analyzer = DependencyAnalyzer(llm_adapter=StubLLMAdapter(_empty_dependency_response(4)))
        specs = (
            ACDependencySpec(
                index=0,
                content="Create OpenCode runtime contract",
                metadata={"id": "runtime"},
                context={"provides": ["session_state", {"name": "tool_catalog"}]},
            ),
            ACDependencySpec(
                index=1,
                content="Add session resume flow",
                context={"prerequisites": ["session_state"]},
            ),
            ACDependencySpec(
                index=2,
                content="Normalize built-in and MCP tools",
                metadata={"context": {"shared_prerequisites": ["tool_catalog"]}},
            ),
            ACDependencySpec(
                index=3,
                content="Wire coordinator reconciliation",
                context={"after": [{"reference": "AC 3"}]},
            ),
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == ((0,), (1, 2), (3,))
        assert graph.independent_indices == (0,)
        assert graph.serialized_indices == (1, 2, 3)
        assert graph.get_dependencies(1) == (0,)
        assert graph.get_dependencies(2) == (0,)
        assert graph.get_dependencies(3) == (2,)
        assert graph.get_node(1).serialization_reasons == ("context dependency on AC 1",)
        assert graph.get_node(2).serialization_reasons == (
            "metadata context shared prerequisite AC 1",
        )
        assert graph.get_node(3).serialization_reasons == ("context dependency on AC 3",)

    @pytest.mark.asyncio
    async def test_analyze_serializes_shared_runtime_resource_conflicts(self) -> None:
        analyzer = DependencyAnalyzer(llm_adapter=StubLLMAdapter(_empty_dependency_response(3)))
        specs = (
            ACDependencySpec(
                index=0,
                content="Refactor shared router",
                shared_runtime_resources=(ACSharedRuntimeResource(name="workspace/router.py"),),
            ),
            ACDependencySpec(
                index=1,
                content="Add auth middleware",
                metadata={
                    "shared_runtime_resources": [
                        {"name": "workspace/router.py", "mode": "write"},
                    ]
                },
            ),
            ACDependencySpec(index=2, content="Add CLI docs"),
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == ((0, 2), (1,))
        assert graph.independent_indices == (2,)
        assert graph.serialized_indices == (0, 1)
        assert graph.get_dependencies(1) == (0,)
        assert graph.get_node(0).serialization_reasons == (
            "shared runtime resource 'workspace/router.py'",
        )
        assert graph.get_node(1).serialization_reasons == (
            "shared runtime resource 'workspace/router.py'",
        )

    @pytest.mark.asyncio
    async def test_analyze_collects_shared_runtime_resources_from_context(self) -> None:
        analyzer = DependencyAnalyzer(llm_adapter=StubLLMAdapter(_empty_dependency_response(3)))
        specs = (
            ACDependencySpec(
                index=0,
                content="Update runtime adapter",
                context={
                    "shared_runtime_resources": [{"name": "workspace/runtime.py", "mode": "write"}]
                },
            ),
            ACDependencySpec(
                index=1,
                content="Add resume persistence",
                metadata={"context": {"resources": ["workspace/runtime.py"]}},
            ),
            ACDependencySpec(index=2, content="Document retry audit flow"),
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == ((0, 2), (1,))
        assert graph.get_dependencies(1) == (0,)
        assert graph.get_node(0).serialization_reasons == (
            "shared runtime resource 'workspace/runtime.py'",
        )
        assert graph.get_node(1).serialization_reasons == (
            "shared runtime resource 'workspace/runtime.py'",
        )

    @pytest.mark.asyncio
    async def test_analyze_falls_back_to_structured_dependencies_when_llm_fails(self) -> None:
        analyzer = DependencyAnalyzer(
            llm_adapter=StubLLMAdapter(error=ProviderError("llm unavailable", provider="test"))
        )
        specs = (
            ACDependencySpec(index=0, content="Create base runtime"),
            ACDependencySpec(
                index=1,
                content="Add resume handling",
                metadata={"requires_serial_execution": True},
                prerequisites=("AC 1",),
            ),
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == ((0,), (1,))
        assert graph.serialized_indices == (1,)
        assert graph.get_dependencies(1) == (0,)
        assert set(graph.get_node(1).serialization_reasons) == {
            "metadata requires serialized execution",
            "prerequisite AC 1",
        }

    @pytest.mark.asyncio
    async def test_analyze_exposes_staged_execution_plan(self) -> None:
        analyzer = DependencyAnalyzer(llm_adapter=StubLLMAdapter(_empty_dependency_response(3)))
        specs = (
            ACDependencySpec(index=0, content="Create data model", metadata={"id": "model"}),
            ACDependencySpec(index=1, content="Build API", prerequisites=("model",)),
            ACDependencySpec(index=2, content="Publish docs"),
        )

        result = await analyzer.analyze(specs)

        assert result.is_ok
        plan = result.value.to_execution_plan()
        assert plan.execution_levels == ((0, 2), (1,))
        assert plan.total_stages == 2
        assert plan.stages == (
            ExecutionStage(index=0, ac_indices=(0, 2), depends_on_stages=()),
            ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
        )
        assert plan.get_dependencies(1) == (0,)

    @pytest.mark.asyncio
    async def test_analyze_preserves_sparse_single_ac_identity(self) -> None:
        analyzer = DependencyAnalyzer(llm_adapter=StubLLMAdapter())
        specs = (ACDependencySpec(index=7, content="Reopen only the failed resume AC"),)

        result = await analyzer.analyze(specs)

        assert result.is_ok
        graph = result.value
        assert graph.execution_levels == ((7,),)
        assert graph.to_runtime_execution_plan().execution_levels == ((7,),)


class TestHybridExecutionPlanner:
    """Tests for dependency graph to execution plan conversion."""

    @pytest.mark.parametrize(
        ("graph", "expected_levels", "expected_stages", "is_parallelizable"),
        [
            pytest.param(
                DependencyGraph(
                    nodes=(
                        ACNode(index=0, content="Create runtime"),
                        ACNode(index=1, content="Persist session", depends_on=(0,)),
                        ACNode(index=2, content="Resume session", depends_on=(1,)),
                    ),
                    execution_levels=(),
                ),
                ((0,), (1,), (2,)),
                (
                    ExecutionStage(index=0, ac_indices=(0,), depends_on_stages=()),
                    ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
                    ExecutionStage(index=2, ac_indices=(2,), depends_on_stages=(1,)),
                ),
                False,
                id="fully-serial",
            ),
            pytest.param(
                DependencyGraph(
                    nodes=(
                        ACNode(index=0, content="Document runtime"),
                        ACNode(index=1, content="Document permissions"),
                        ACNode(index=2, content="Document event model"),
                    ),
                    execution_levels=(),
                ),
                ((0, 1, 2),),
                (ExecutionStage(index=0, ac_indices=(0, 1, 2), depends_on_stages=()),),
                True,
                id="fully-parallel",
            ),
            pytest.param(
                DependencyGraph(
                    nodes=(
                        ACNode(index=0, content="Create runtime"),
                        ACNode(index=1, content="Add streaming bridge", depends_on=(0,)),
                        ACNode(index=2, content="Normalize tool calls", depends_on=(0,)),
                        ACNode(index=3, content="Reconcile workspace", depends_on=(1, 2)),
                    ),
                    execution_levels=(),
                ),
                ((0,), (1, 2), (3,)),
                (
                    ExecutionStage(index=0, ac_indices=(0,), depends_on_stages=()),
                    ExecutionStage(index=1, ac_indices=(1, 2), depends_on_stages=(0,)),
                    ExecutionStage(index=2, ac_indices=(3,), depends_on_stages=(1,)),
                ),
                True,
                id="mixed-hybrid",
            ),
        ],
    )
    def test_create_plan_covers_serial_parallel_and_mixed_dependency_graphs(
        self,
        graph: DependencyGraph,
        expected_levels: tuple[tuple[int, ...], ...],
        expected_stages: tuple[ExecutionStage, ...],
        is_parallelizable: bool,
    ) -> None:
        planner = HybridExecutionPlanner()

        plan = planner.create_plan(graph)

        assert plan.execution_levels == expected_levels
        assert plan.stages == expected_stages
        assert plan.total_stages == len(expected_stages)
        assert plan.is_parallelizable is is_parallelizable

    def test_create_plan_recomputes_levels_when_graph_levels_are_missing(self) -> None:
        planner = HybridExecutionPlanner()
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="Create runtime"),
                ACNode(index=1, content="Add resume flow", depends_on=(0,)),
                ACNode(index=2, content="Add telemetry"),
                ACNode(index=3, content="Integrate retries", depends_on=(1, 2)),
            ),
            execution_levels=(),
        )

        plan = planner.create_plan(graph)

        assert plan.execution_levels == ((0, 2), (1,), (3,))
        assert plan.stages == (
            ExecutionStage(index=0, ac_indices=(0, 2), depends_on_stages=()),
            ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ExecutionStage(index=2, ac_indices=(3,), depends_on_stages=(0, 1)),
        )
        assert plan.get_stage_for_ac(3) == plan.stages[2]

    def test_create_plan_rejects_same_stage_dependency_conflicts(self) -> None:
        planner = HybridExecutionPlanner()
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="Create runtime"),
                ACNode(index=1, content="Add resume flow", depends_on=(0,)),
            ),
            execution_levels=((0, 1),),
        )

        with pytest.raises(ExecutionPlanningError, match="assigned to stage 1"):
            planner.create_plan(graph)

    def test_build_runtime_plan_groups_sparse_ac_ids_into_staged_batches(self) -> None:
        planner = HybridExecutionPlanner()
        graph = DependencyGraph(
            nodes=(
                ACNode(index=1, content="Reopen auth runtime"),
                ACNode(index=4, content="Refresh docs"),
                ACNode(index=7, content="Repair resume handling", depends_on=(1,)),
                ACNode(index=9, content="Re-run evaluator", depends_on=(7,)),
            ),
            execution_levels=(),
        )

        plan = planner.build_runtime_plan(graph)

        assert plan.execution_levels == ((1, 4), (7,), (9,))
        assert plan.stages == (
            ExecutionStage(index=0, ac_indices=(1, 4), depends_on_stages=()),
            ExecutionStage(index=1, ac_indices=(7,), depends_on_stages=(0,)),
            ExecutionStage(index=2, ac_indices=(9,), depends_on_stages=(1,)),
        )
        assert graph.to_runtime_execution_plan() == plan

    def test_build_runtime_plan_rejects_missing_sparse_dependencies(self) -> None:
        planner = HybridExecutionPlanner()
        graph = DependencyGraph(
            nodes=(ACNode(index=3, content="Repair runtime state", depends_on=(8,)),),
            execution_levels=(),
        )

        with pytest.raises(ExecutionPlanningError, match="depends on missing AC 8"):
            planner.build_runtime_plan(graph)
