"""Tests for reusable RLM Hermes test fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.ac_tree import ACStatus, ACTree
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.persistence.event_store import EventStore
from ouroboros.rlm import (
    HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT,
    RLM_HERMES_CALL_FAILED_EVENT,
    RLM_HERMES_CALL_STARTED_EVENT,
    RLM_HERMES_CALL_SUCCEEDED_EVENT,
    RLM_HERMES_SYNTHESIZE_PARENT_MODE,
    RLM_RECURSIVE_FIXTURE_SCHEMA_VERSION,
    RLMHermesTraceRecord,
    RLMNodeLifecycleState,
    RLMRunConfig,
    RLMRunLifecycleState,
    RLMTerminationReason,
    RLMTraceStore,
    create_rlm_hermes_trace_event,
    hash_trace_text,
    load_recursive_fixture,
    recursive_fixture_from_mapping,
    rlm_hermes_trace_records_from_events,
    run_rlm_loop,
)

_RLM_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "rlm"


class _FailingFixtureHermesRuntime:
    """Hermes RPC test double that fails at the adapter boundary."""

    runtime_backend = "hermes"
    llm_backend = "hermes"
    working_directory = None
    permission_mode = None

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_handle": resume_handle,
                "resume_session_id": resume_session_id,
            }
        )
        return Result.err(
            ProviderError(
                "fixture hermes unavailable",
                provider="hermes",
                details={"error_type": "FixtureHermesFailure"},
            )
        )


def test_minimal_recursive_run_fixture_reconstructs_parent_subcall_and_child_ac(
    minimal_recursive_run_fixture: dict[str, Any],
) -> None:
    """The checked-in recursive fixture has stable IDs across trace and AC replay."""
    payload = minimal_recursive_run_fixture
    ac_tree = ACTree.from_dict(payload["ac_tree"])
    records = tuple(RLMHermesTraceRecord.from_dict(item) for item in payload["trace_records"])
    records_by_call_id = {record.call_id: record for record in records}
    parent = payload["parent"]
    child = payload["child"]

    assert payload["schema_version"] == "rlm.minimal_recursive_run_fixture.v1"
    assert set(records_by_call_id) == {parent["call_id"], child["call_id"]}

    parent_record = records_by_call_id[parent["call_id"]]
    child_record = records_by_call_id[child["call_id"]]
    child_ac = ac_tree.get_node(child["ac_node_id"])
    assert child_ac is not None

    assert parent_record.trace_id == parent["trace_id"]
    assert parent_record.subcall_id == parent["subcall_id"]
    assert parent_record.generated_child_ac_node_ids == (child["ac_node_id"],)

    assert child_record.parent_call_id == parent["call_id"]
    assert child_record.parent_trace_id == parent["trace_id"]
    assert child_record.causal_parent_event_id == parent["call_id"]
    assert child_record.depth == 1
    assert child_record.ac_node_id == child_ac.id

    assert child_ac.parent_id == parent["ac_node_id"]
    assert child_ac.originating_subcall_trace_id == parent["trace_id"]
    assert child_ac.metadata["originating_subcall_trace_id"] == parent["trace_id"]
    assert child_ac.metadata["rlm_parent_call_id"] == parent["call_id"]
    assert child_ac.metadata["rlm_parent_subcall_id"] == parent["subcall_id"]
    assert child_ac.metadata["rlm_child_call_context"] == {
        "call_id": child["call_id"],
        "parent_call_id": parent["call_id"],
        "parent_subcall_id": parent["subcall_id"],
        "depth": 1,
    }

    events = tuple(
        create_rlm_hermes_trace_event(
            record,
            event_type=RLM_HERMES_CALL_SUCCEEDED_EVENT,
            aggregate_id=payload["generation_id"],
        )
        for record in records
    )
    assert rlm_hermes_trace_records_from_events(events) == records
    assert events[0].data["replay"]["creates_ac_node_ids"] == [child["ac_node_id"]]
    assert events[0].data["ac_node"]["child_ids"] == [child["ac_node_id"]]
    assert events[1].data["trace"]["parent_trace_id"] == parent["trace_id"]
    assert events[1].data["trace"]["causal_parent_event_id"] == parent["call_id"]


def test_long_context_truncation_fixture_declares_retained_facts_and_requirements(
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The long-context fixture is deterministic and self-checking."""
    payload = long_context_truncation_fixture
    target = payload["target"]
    truncation_config = payload["truncation_config"]
    retained_facts = payload["expected_retained_facts"]
    omitted_facts = payload["expected_omitted_facts"]
    completion_requirements = payload["completion_requirements"]

    assert payload["schema_version"] == "rlm.long_context_truncation_fixture.v1"
    assert payload["fixture_id"] == "rlm-long-context-truncation-v1"
    assert target["line_count"] == len(target["lines"])
    assert target["line_count"] > (
        truncation_config["chunk_line_limit"] * truncation_config["max_atomic_chunks"]
    )

    retained_fact_ids = {fact["fact_id"] for fact in retained_facts}
    omitted_fact_ids = {fact["fact_id"] for fact in omitted_facts}
    selected_chunk_ids = set(truncation_config["expected_selected_chunk_ids"])
    omitted_chunk_ids = set(truncation_config["expected_omitted_chunk_ids"])

    assert len(retained_fact_ids) == len(retained_facts)
    assert len(omitted_fact_ids) == len(omitted_facts)
    assert retained_fact_ids.isdisjoint(omitted_fact_ids)
    assert selected_chunk_ids.isdisjoint(omitted_chunk_ids)
    assert len(selected_chunk_ids) == truncation_config["max_atomic_chunks"]

    for fact in retained_facts:
        assert target["lines"][fact["line"] - 1] == fact["text"]
        assert fact["chunk_id"] in selected_chunk_ids
        assert fact["must_appear_in"] == [
            "chunk_prompt",
            "parent_synthesis_prompt",
            "completion_evidence",
        ]

    for fact in omitted_facts:
        assert target["lines"][fact["line"] - 1] == fact["text"]
        assert fact["chunk_id"] in omitted_chunk_ids
        assert fact["line"] > truncation_config["truncation_boundary"]["last_retained_line"]

    cite_requirement = completion_requirements[0]
    boundary_requirement = completion_requirements[1]
    assert set(cite_requirement["required_fact_ids"]) == retained_fact_ids
    assert set(cite_requirement["must_cite_chunk_ids"]) == selected_chunk_ids
    assert cite_requirement["minimum_confidence"] == 0.8
    assert boundary_requirement["must_not_claim_fact_ids"] == sorted(omitted_fact_ids)
    assert boundary_requirement["must_report_truncation"] is True
    assert boundary_requirement["truncation_boundary"] == truncation_config["truncation_boundary"]


def test_recursive_fixture_loader_provides_prompt_state_limits_and_expected_outputs(
    tmp_path: Path,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The recursive fixture config is the source of truth for RLM test runs."""
    fixture = load_recursive_fixture(_RLM_FIXTURES_DIR / "long_context_truncation.json")
    payload = long_context_truncation_fixture
    recursive_run = payload["recursive_run"]

    assert recursive_run["schema_version"] == RLM_RECURSIVE_FIXTURE_SCHEMA_VERSION
    assert fixture.fixture_id == payload["fixture_id"]
    assert fixture.initial_prompt == recursive_run["initial_prompt"]
    assert fixture.expected_selected_chunk_ids == tuple(
        recursive_run["expected_outputs"]["selected_chunk_ids"]
    )
    assert fixture.expected_omitted_chunk_ids == tuple(
        recursive_run["expected_outputs"]["omitted_chunk_ids"]
    )

    target_path = fixture.write_target(tmp_path)
    assert target_path == tmp_path / payload["target"]["path"]
    assert target_path.read_text(encoding=payload["target"]["encoding"]).splitlines() == (
        payload["target"]["lines"]
    )

    config = fixture.to_run_config(cwd=tmp_path)
    limits = recursive_run["iteration_limits"]
    assert config.target == payload["target"]["path"]
    assert config.fixture_id == payload["fixture_id"]
    assert config.initial_prompt == recursive_run["initial_prompt"]
    assert config.max_depth == limits["max_depth"]
    assert config.ambiguity_threshold == limits["ambiguity_threshold"]
    assert config.chunk_line_limit == limits["chunk_line_limit"]
    assert config.max_atomic_chunks == limits["max_atomic_chunks"]
    assert config.max_iterations == limits["max_iterations"]

    scaffold_state = fixture.initial_scaffold_state(cwd=tmp_path)
    fixture.assert_initial_state_matches(scaffold_state)


@pytest.mark.asyncio
async def test_recursive_fixture_executes_successfully_with_deterministic_hermes(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The long-context fixture completes through the executable RLM loop."""
    recursive_fixture = recursive_fixture_from_mapping(long_context_truncation_fixture)
    recursive_fixture.write_target(tmp_path)
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-fixture-success.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            recursive_fixture.to_run_config(
                cwd=tmp_path,
                hermes_runtime=deterministic_rlm_hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    recursive_fixture.assert_result_matches(result)
    assert result.atomic_execution is not None
    assert result.atomic_execution.success is True
    assert result.atomic_execution.hermes_subcall.mode == RLM_HERMES_SYNTHESIZE_PARENT_MODE

    expected_chunk_calls = [
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
        "rlm_call_atomic_chunk_003",
        "rlm_call_atomic_chunk_004",
    ]
    assert [exchange.call_id for exchange in deterministic_rlm_hermes_runtime.exchanges] == [
        *expected_chunk_calls,
        "rlm_call_atomic_synthesis",
    ]
    assert all(call["tools"] == [] for call in deterministic_rlm_hermes_runtime.calls)
    assert all(
        call["system_prompt"] == HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT
        for call in deterministic_rlm_hermes_runtime.calls
    )

    terminal_records = [record for record in records if record.success is True]
    assert len(terminal_records) == result.hermes_subcall_count
    assert [record.call_id for record in terminal_records] == [
        *expected_chunk_calls,
        "rlm_call_atomic_synthesis",
    ]
    assert [
        event.type for event in events if event.type == RLM_HERMES_CALL_SUCCEEDED_EVENT
    ] == [RLM_HERMES_CALL_SUCCEEDED_EVENT] * result.hermes_subcall_count


@pytest.mark.asyncio
async def test_recursive_fixture_loop_terminates_at_parent_synthesis(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """Fixture execution stops with the outer scaffold's synthesis condition."""
    recursive_fixture = recursive_fixture_from_mapping(long_context_truncation_fixture)
    recursive_fixture.write_target(tmp_path)

    result = await run_rlm_loop(
        recursive_fixture.to_run_config(
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
        )
    )

    scaffold = result.outer_scaffold_state
    assert scaffold is not None
    assert scaffold.run_state == RLMRunLifecycleState.COMPLETED
    assert scaffold.termination_reason == RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED
    assert scaffold.is_terminal is True
    assert scaffold.has_converged is True
    assert scaffold.work_queue == []
    assert scaffold.iteration == result.hermes_subcall_count + 1

    root_node = scaffold.nodes["rlm_node_root"]
    assert root_node.state == RLMNodeLifecycleState.SYNTHESIS_COMPLETE
    assert root_node.terminal_reason == RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED
    assert scaffold.generated_rlm_tree_depth == 1

    child_node_ids = root_node.child_node_ids
    assert child_node_ids == (
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
        "rlm_node_atomic_chunk_003",
        "rlm_node_atomic_chunk_004",
    )
    assert [
        scaffold.nodes[node_id].state for node_id in child_node_ids
    ] == [RLMNodeLifecycleState.ATOMIC_COMPLETE] * 4

    root_ac = scaffold.ac_tree.get_node("rlm_ac_root")
    assert root_ac is not None
    assert root_ac.status == ACStatus.COMPLETED
    assert root_ac.children_ids == tuple(
        f"rlm_ac_atomic_chunk_{index:03d}" for index in range(1, 5)
    )
    assert all(
        scaffold.ac_tree.get_node(f"rlm_ac_atomic_chunk_{index:03d}").status
        == ACStatus.COMPLETED
        for index in range(1, 5)
    )
    assert scaffold.transitions[-1].to_state == RLMRunLifecycleState.COMPLETED
    assert scaffold.transitions[-1].reason == RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED


@pytest.mark.asyncio
async def test_recursive_fixture_propagates_hermes_adapter_failure(
    tmp_path: Path,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """Adapter failures from Hermes abort the fixture run and persist failure traces."""
    recursive_fixture = recursive_fixture_from_mapping(long_context_truncation_fixture)
    recursive_fixture.write_target(tmp_path)
    hermes_runtime = _FailingFixtureHermesRuntime()
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-fixture-failure.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        with pytest.raises(ValueError, match="Hermes atomic execution sub-call failed"):
            await run_rlm_loop(
                recursive_fixture.to_run_config(
                    cwd=tmp_path,
                    hermes_runtime=hermes_runtime,
                    trace_store=trace_store,
                )
            )
        events = await event_store.replay("rlm_run", "rlm_generation_0")
        records = await trace_store.replay_hermes_subcalls("rlm_generation_0")
    finally:
        await event_store.close()

    assert len(hermes_runtime.calls) == 1
    failed_prompt = json.loads(str(hermes_runtime.calls[0]["prompt"]))
    assert failed_prompt["run"]["fixture_id"] == recursive_fixture.fixture_id
    assert failed_prompt["call_context"] == {
        "call_id": "rlm_call_atomic_chunk_001",
        "parent_call_id": "rlm_call_atomic_synthesis",
        "depth": 1,
    }
    assert hermes_runtime.calls[0]["tools"] == []
    assert hermes_runtime.calls[0]["system_prompt"] == HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT

    assert [event.type for event in events] == [
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_FAILED_EVENT,
    ]
    assert [event.data["lifecycle"]["status"] for event in events] == ["started", "failed"]
    assert len(records) == 2
    started_record, failed_record = records
    assert started_record.success is None
    assert started_record.call_id == "rlm_call_atomic_chunk_001"
    assert failed_record.success is False
    assert failed_record.exit_code == 1
    assert failed_record.completion == "fixture hermes unavailable"
    assert failed_record.adapter_error == {
        "provider": "hermes",
        "message": "fixture hermes unavailable",
        "details": {"error_type": "FixtureHermesFailure"},
    }


@pytest.mark.asyncio
async def test_long_context_truncation_fixture_selects_expected_retained_facts(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The fixture drives a deterministic truncated recursive RLM execution."""
    payload = long_context_truncation_fixture
    recursive_fixture = recursive_fixture_from_mapping(payload)
    truncation_config = payload["truncation_config"]
    recursive_fixture.write_target(tmp_path)

    result = await run_rlm_loop(
        recursive_fixture.to_run_config(
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
        )
    )
    recursive_fixture.assert_result_matches(result)

    expected_selected_chunk_ids = truncation_config["expected_selected_chunk_ids"]
    prompt_envelopes = [
        json.loads(exchange.prompt) for exchange in deterministic_rlm_hermes_runtime.exchanges
    ]
    synthesis_prompt = prompt_envelopes[-1]
    synthesis_chunks = synthesis_prompt["context"]["chunks"]
    joined_prompts = "\n".join(
        exchange.prompt for exchange in deterministic_rlm_hermes_runtime.exchanges
    )

    assert result.status == "completed"
    assert result.hermes_subcall_count == len(expected_selected_chunk_ids) + 1
    assert prompt_envelopes[0]["run"]["fixture_id"] == payload["fixture_id"]
    assert (
        prompt_envelopes[0]["objective"]["initial_prompt"] == recursive_fixture.initial_prompt
    )
    assert prompt_envelopes[0]["context"]["initial_prompt"] == recursive_fixture.initial_prompt
    observed_chunk_call_ids = [
        exchange.call_id for exchange in deterministic_rlm_hermes_runtime.exchanges[:-1]
    ]
    assert observed_chunk_call_ids == [
        f"rlm_call_atomic_chunk_{index:03d}"
        for index in range(1, len(expected_selected_chunk_ids) + 1)
    ]
    assert deterministic_rlm_hermes_runtime.exchanges[-1].call_id == (
        "rlm_call_atomic_synthesis"
    )
    assert [chunk["chunk_id"] for chunk in synthesis_chunks] == expected_selected_chunk_ids
    assert [chunk["end_line"] for chunk in synthesis_chunks][-1] == (
        truncation_config["truncation_boundary"]["last_retained_line"]
    )
    assert synthesis_chunks[-1]["truncated"] is True

    chunk_prompts_by_id = {
        envelope["context"]["chunks"][0]["chunk_id"]: envelope
        for envelope in prompt_envelopes[:-1]
    }
    assert set(chunk_prompts_by_id) == set(expected_selected_chunk_ids)
    for fact in payload["expected_retained_facts"]:
        chunk_prompt = chunk_prompts_by_id[fact["chunk_id"]]
        assert fact["text"] in chunk_prompt["context"]["chunks"][0]["content"]
        assert fact["text"] in json.dumps(synthesis_chunks)

    for fact in payload["expected_omitted_facts"]:
        assert fact["chunk_id"] not in expected_selected_chunk_ids
        assert fact["text"] not in joined_prompts

    required_output_fields = set(synthesis_prompt["output_contract"]["required_fields"])
    retained_fact_ids = {fact["fact_id"] for fact in payload["expected_retained_facts"]}
    selected_chunk_ids = {chunk["chunk_id"] for chunk in synthesis_chunks}
    for requirement in payload["completion_requirements"]:
        assert set(requirement.get("required_fact_ids", ())) <= retained_fact_ids
        assert set(requirement.get("must_cite_chunk_ids", ())) <= selected_chunk_ids
        assert set(requirement.get("required_output_fields", ())) <= required_output_fields


@pytest.mark.asyncio
async def test_deterministic_hermes_fixture_emits_nested_traceable_exchanges(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
) -> None:
    """Nested fixture calls preserve prompt/completion pairs in trace replay."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "VALUE_1 = 1",
                "VALUE_2 = 2",
                "VALUE_3 = 3",
            ]
        ),
        encoding="utf-8",
    )
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-fixture-trace.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=deterministic_rlm_hermes_runtime,
                trace_store=trace_store,
            )
        )
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    exchanges = deterministic_rlm_hermes_runtime.exchanges
    assert result.hermes_subcall_count == 4
    assert [exchange.call_id for exchange in exchanges] == [
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
        "rlm_call_atomic_chunk_003",
        "rlm_call_atomic_synthesis",
    ]

    records_by_call_id = {record.call_id: record for record in records}
    prompts_by_call_id = deterministic_rlm_hermes_runtime.prompts_by_call_id
    completions_by_call_id = deterministic_rlm_hermes_runtime.completions_by_call_id
    assert set(records_by_call_id) == set(prompts_by_call_id) == set(completions_by_call_id)

    child_call_ids = [exchange.call_id for exchange in exchanges[:3]]
    for exchange in exchanges[:3]:
        assert exchange.parent_call_id == "rlm_call_atomic_synthesis"
        assert exchange.depth == 1
        completion = exchange.completion_payload()
        assert completion["result"]["call_id"] == exchange.call_id
        assert completion["result"]["subcall_id"] == exchange.subcall_id
        assert completion["result"]["parent_call_id"] == "rlm_call_atomic_synthesis"
        assert completion["result"]["depth"] == 1
        assert completion["result"]["prompt_hash"] == hash_trace_text(exchange.prompt)
        assert completion["result"]["selected_chunk_ids"] == list(exchange.selected_chunk_ids)
        assert completion["evidence_references"][0]["chunk_id"] == exchange.selected_chunk_ids[0]

    synthesis = exchanges[-1]
    synthesis_completion = synthesis.completion_payload()
    assert synthesis.mode == RLM_HERMES_SYNTHESIZE_PARENT_MODE
    assert synthesis.parent_call_id is None
    assert synthesis.depth == 0
    assert synthesis.generated_child_ac_node_ids == (
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
        "rlm_ac_atomic_chunk_003",
    )
    assert synthesis_completion["result"]["child_call_ids"] == child_call_ids

    for exchange in exchanges:
        prompt_envelope = json.loads(exchange.prompt)
        record = records_by_call_id[exchange.call_id]

        assert prompt_envelope["call_context"]["call_id"] == exchange.call_id
        assert prompt_envelope["trace"]["subcall_id"] == exchange.subcall_id
        assert record.subcall_id == exchange.subcall_id
        assert record.prompt == exchange.prompt
        assert record.completion == exchange.completion
        assert record.response_hash == hash_trace_text(exchange.completion)
        assert record.selected_chunk_ids == exchange.selected_chunk_ids
        assert record.generated_child_ac_node_ids == exchange.generated_child_ac_node_ids
