"""Regressions for ATOMIC decomposition judgments in ``ParallelACExecutor``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
)
from ouroboros.orchestrator.parallel_executor import (
    MAX_DECOMPOSITION_DEPTH,
    ACExecutionResult,
    ParallelACExecutor,
)


class _AtomicDecompositionRuntime:
    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        del prompt, tools, system_prompt, resume_handle, resume_session_id
        yield AgentMessage(type="result", content="ATOMIC")


@pytest.mark.asyncio
async def test_try_decompose_ac_treats_atomic_response_as_terminal() -> None:
    """Claude's explicit ATOMIC verdict should suppress further decomposition."""
    executor = ParallelACExecutor(
        adapter=_AtomicDecompositionRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    result = await executor._try_decompose_ac(
        ac_content="Implement one focused leaf task.",
        ac_index=0,
        seed_goal="Preserve ATOMIC termination",
        tools=["Read"],
        system_prompt="system",
    )

    assert result.disposition is DecompositionDisposition.ATOMIC
    assert result.source is DecompositionSource.PREFLIGHT
    assert result.children == ()
    assert result.reasons == ("explicit_atomic",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth",
    range(MAX_DECOMPOSITION_DEPTH),
    ids=lambda depth: f"depth_{depth}",
)
async def test_atomic_judgment_stops_single_ac_recursion_at_any_analyzed_depth(
    depth: int,
) -> None:
    """Nested AC execution should stop recursing once decomposition returns ATOMIC."""
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )
    executor._emit_subtask_event = AsyncMock()
    executor._try_decompose_ac = AsyncMock(
        return_value=DecompositionDecisionRecord(
            node_id=f"exec_atomic_depth_{depth}:ac:{depth + 1}",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.ATOMIC,
            reasons=("explicit_atomic",),
        )
    )
    executor._execute_atomic_ac = AsyncMock(
        return_value=ACExecutionResult(
            ac_index=depth + 1,
            ac_content=f"Atomic at depth {depth}",
            success=True,
            final_message="leaf complete",
            depth=depth,
        )
    )

    with patch.object(
        executor,
        "_execute_single_ac",
        wraps=executor._execute_single_ac,
    ) as execute_single_ac_spy:
        result = await executor._execute_single_ac(
            ac_index=depth + 1,
            ac_content=f"Atomic at depth {depth}",
            session_id=f"sess_atomic_depth_{depth}",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Preserve ATOMIC termination",
            depth=depth,
            execution_id=f"exec_atomic_depth_{depth}",
        )

    assert result.success is True
    assert result.is_decomposed is False
    assert result.depth == depth
    executor._try_decompose_ac.assert_awaited_once()
    executor._execute_atomic_ac.assert_awaited_once()
    assert len(execute_single_ac_spy.await_args_list) == 1
    assert execute_single_ac_spy.await_args.kwargs["depth"] == depth


class _CapturingDecompositionRuntime:
    def __init__(self, content: str = "ATOMIC") -> None:
        self.content = content
        self.prompt: str | None = None
        self.system_prompt: str | None = None

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        del tools, resume_handle, resume_session_id
        self.prompt = prompt
        self.system_prompt = system_prompt
        yield AgentMessage(type="result", content=self.content)


@pytest.mark.asyncio
async def test_try_decompose_ac_uses_profile_axis_when_profile_is_configured() -> None:
    """Profile-aware decomposition should use axis/min_unit from ExecutionProfile."""
    from ouroboros.orchestrator.profile_loader import load_profile

    runtime = _CapturingDecompositionRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
        execution_profile=load_profile("research"),
    )

    result = await executor._try_decompose_ac(
        ac_content="Compare three runtime designs with citations.",
        ac_index=0,
        seed_goal="Produce a sourced design memo",
        tools=["Read"],
        system_prompt="legacy system prompt",
    )

    assert result.disposition is DecompositionDisposition.ATOMIC
    assert result.source is DecompositionSource.PREFLIGHT
    assert result.children == ()
    assert result.reasons == ("explicit_atomic",)
    assert runtime.prompt is not None
    assert "Split along the axis: subtopic" in runtime.prompt
    assert "single question answerable from independently cited sources" in runtime.prompt
    assert runtime.system_prompt is not None
    assert "'research' domain" in runtime.system_prompt


@pytest.mark.asyncio
async def test_try_decompose_ac_uses_profile_max_branching_from_loaded_yaml(tmp_path) -> None:
    """Loaded profile max_branching should drive the live decomposer prompt and bounds."""
    from ouroboros.orchestrator.profile_loader import load_profile

    (tmp_path / "custom.yaml").write_text(
        """
profile: custom
schema_version: 1
axis: source
min_unit: "single sourced claim"
cut_signal: "claim has citations"
max_branching: 3
must_produce: [claims]
evidence_schema:
  required: [claims]
verifier_capability: read_only_discovery
verifier_focus: "Check claim support."
suggested_tools: [Read, Grep]
suggested_model_tier: medium
""",
        encoding="utf-8",
    )
    runtime = _CapturingDecompositionRuntime(
        content='["Sub-AC 1: a", "Sub-AC 2: b", "Sub-AC 3: c"]'
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
        execution_profile=load_profile("custom", profiles_dir=tmp_path),
    )

    result = await executor._try_decompose_ac(
        ac_content="Split a research task into sourced claims.",
        ac_index=0,
        seed_goal="Produce a sourced memo",
        tools=["Read"],
        system_prompt="legacy system prompt",
    )

    assert result.disposition is DecompositionDisposition.SPLIT
    assert result.source is DecompositionSource.PREFLIGHT
    assert [child.description for child in result.children] == [
        "Sub-AC 1: a",
        "Sub-AC 2: b",
        "Sub-AC 3: c",
    ]
    assert result.trustworthy is False
    assert result.reasons == ("legacy_array_without_attestation",)
    assert runtime.prompt is not None
    assert "2-3 sub-ACs" in runtime.prompt
    assert "2-5 sub-ACs" not in runtime.prompt


class _FixedResponseRuntime:
    """Runtime that yields a single fixed decomposition response."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        del prompt, tools, system_prompt, resume_handle, resume_session_id
        yield AgentMessage(type="result", content=self._content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_disposition", "expected_children", "expected_reasons"),
    [
        # Explicit atomic verdict the response STARTS WITH → atomic.
        ("ATOMIC", DecompositionDisposition.ATOMIC, [], ("explicit_atomic",)),
        (
            "ATOMIC — this is one focused task",
            DecompositionDisposition.ATOMIC,
            [],
            ("explicit_atomic",),
        ),
        # A real split that happens to contain the word "ATOMIC" in a negation
        # must NOT be mis-read as atomic (the substring-match bug).
        (
            'NOT ATOMIC, decompose into: ["Sub-AC 1: build it", "Sub-AC 2: test it"]',
            DecompositionDisposition.SPLIT,
            ["Sub-AC 1: build it", "Sub-AC 2: test it"],
            ("legacy_array_without_attestation",),
        ),
        # A valid array whose elements merely mention "atomic" must still split.
        (
            '["update the atomic counter module", "add a regression test"]',
            DecompositionDisposition.SPLIT,
            ["update the atomic counter module", "add a regression test"],
            ("legacy_array_without_attestation",),
        ),
        # Unparseable / no verdict → fail closed as an explicit UNKNOWN decision.
        (
            "I think this could go either way, hard to say.",
            DecompositionDisposition.UNKNOWN,
            [],
            ("unparseable_decomposition_response",),
        ),
    ],
    ids=["bare_atomic", "atomic_prefix", "not_atomic_split", "array_mentions_atomic", "garbage"],
)
async def test_try_decompose_ac_parses_verdict_array_before_atomic_substring(
    response: str,
    expected_disposition: DecompositionDisposition,
    expected_children: list[str],
    expected_reasons: tuple[str, ...],
) -> None:
    """JSON array is parsed before the ATOMIC verdict, and only a leading ATOMIC
    counts — so negated/atomic-mentioning splits are not mis-classified, and
    unparseable responses fail closed to UNKNOWN."""
    executor = ParallelACExecutor(
        adapter=_FixedResponseRuntime(response),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    result = await executor._try_decompose_ac(
        ac_content="Some acceptance criterion.",
        ac_index=0,
        seed_goal="goal",
        tools=["Read"],
        system_prompt="system",
    )

    assert result.disposition is expected_disposition
    assert result.source is DecompositionSource.PREFLIGHT
    assert [child.description for child in result.children] == expected_children
    assert result.reasons == expected_reasons


@pytest.mark.asyncio
async def test_try_decompose_ac_prompts_for_artifact_partition_when_parent_declares() -> None:
    """When the parent AC's seed-authored contract declares expected_artifacts,
    the decomposer prompt must list them and require an exact one-child-each
    assignment via the child "expected_artifacts" JSON field."""
    from ouroboros.core.seed import AcceptanceCriterionSpec

    runtime = _CapturingDecompositionRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    await executor._try_decompose_ac(
        ac_content="Build the parser and its docs.",
        ac_index=0,
        seed_goal="goal",
        tools=["Read"],
        system_prompt="system",
        ac_spec=AcceptanceCriterionSpec(
            description="Build the parser and its docs.",
            expected_artifacts=("src/parser.py", "docs/parser.md"),
        ),
    )

    assert runtime.prompt is not None
    assert "- src/parser.py" in runtime.prompt
    assert "- docs/parser.md" in runtime.prompt
    assert "EXACTLY ONE child" in runtime.prompt
    assert '"expected_artifacts"' in runtime.prompt


@pytest.mark.asyncio
async def test_try_decompose_ac_never_mentions_artifacts_without_parent_contract() -> None:
    """Invariant 4 (prompt leg): a parent with no expected_artifacts must get
    the exact pre-feature prompt shape -- no artifact instructions, and no
    "expected_artifacts" example key to tempt the decomposer into inventing
    the field."""
    from ouroboros.core.seed import AcceptanceCriterionSpec

    runtime = _CapturingDecompositionRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    await executor._try_decompose_ac(
        ac_content="Build the parser.",
        ac_index=0,
        seed_goal="goal",
        tools=["Read"],
        system_prompt="system",
        ac_spec=AcceptanceCriterionSpec(description="Build the parser."),
    )

    assert runtime.prompt is not None
    assert "expected_artifacts" not in runtime.prompt


@pytest.mark.asyncio
async def test_parse_structured_decomposition_threads_parent_artifacts() -> None:
    """The structural parse leg must reject a proposal whose children do not
    exactly partition the parent's declared artifact set, and accept one that
    does (carrying the slices onto the parsed children)."""
    import json as _json

    valid = _json.dumps(
        {
            "children": [
                {
                    "description": "Implement the parser module",
                    "coverage_claims": ["parser scope"],
                    "verification_hint": "parser file exists",
                    "expected_artifacts": ["src/parser.py"],
                },
                {
                    "description": "Write the parser documentation",
                    "coverage_claims": ["docs scope"],
                    "verification_hint": "docs file exists",
                    "expected_artifacts": ["docs/parser.md"],
                },
            ],
            "covers_parent": True,
            "rationale": "Parser code and docs are independently verifiable.",
        }
    )
    proposal, reasons = ParallelACExecutor._parse_structured_decomposition(
        valid,
        parent_text="Build the parser and its docs.",
        min_sub_acs=2,
        max_sub_acs=5,
        parent_expected_artifacts=("src/parser.py", "docs/parser.md"),
    )
    assert reasons == ()
    assert proposal is not None
    assert proposal.children[0].expected_artifacts == ("src/parser.py",)
    assert proposal.children[1].expected_artifacts == ("docs/parser.md",)

    # Same payload but the docs artifact is never assigned: rejected.
    incomplete = valid.replace(
        '"expected_artifacts": ["docs/parser.md"]', '"expected_artifacts": []'
    )
    proposal, reasons = ParallelACExecutor._parse_structured_decomposition(
        incomplete,
        parent_text="Build the parser and its docs.",
        min_sub_acs=2,
        max_sub_acs=5,
        parent_expected_artifacts=("src/parser.py", "docs/parser.md"),
    )
    assert proposal is None
    assert any("not fully assigned" in reason for reason in reasons)
