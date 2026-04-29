"""End-to-end tests for the isolated RLM recursive execution loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult
from ouroboros.persistence.event_store import EventStore
from ouroboros.rlm import (
    RLM_HERMES_CALL_STARTED_EVENT,
    RLM_HERMES_CALL_SUCCEEDED_EVENT,
    RLM_HERMES_SYNTHESIZE_PARENT_MODE,
    RLMRunConfig,
    RLMTraceStore,
    run_rlm_loop,
    verify_rlm_subcall_child_ac_links,
)


class _DeterministicHermesRuntime:
    """Hermes RPC test double for exercising the full RLM loop boundary."""

    runtime_backend = "hermes"
    llm_backend = "hermes"
    working_directory = None
    permission_mode = None

    def __init__(self) -> None:
        self.prompts: list[dict[str, Any]] = []

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        """Return a successful Hermes output derived from the prompt envelope."""
        envelope = json.loads(prompt)
        self.prompts.append(envelope)
        trace = envelope["trace"]
        call_context = envelope["call_context"]

        completion = {
            "schema_version": "rlm.hermes.output.v1",
            "mode": envelope["mode"],
            "verdict": "passed",
            "confidence": 0.91,
            "result": {
                "summary": f"completed {call_context['call_id']}",
                "call_id": call_context["call_id"],
                "subcall_id": trace["subcall_id"],
                "parent_call_id": call_context["parent_call_id"],
                "generated_child_ac_node_ids": trace.get("generated_child_ac_node_ids", []),
            },
            "evidence_references": [
                {"chunk_id": chunk_id, "claim": f"covered {chunk_id}"}
                for chunk_id in trace.get("selected_chunk_ids", [])
            ],
            "residual_gaps": [],
        }
        return Result.ok(
            TaskResult(
                success=True,
                final_message=json.dumps(completion, sort_keys=True),
                messages=(),
            )
        )


async def test_recursive_loop_links_child_ac_to_originating_subcall(tmp_path: Path) -> None:
    """Running RLM recursion links child AC nodes to the parent Hermes sub-call."""
    target = tmp_path / "large.py"
    target.write_text("VALUE_1 = 1\nVALUE_2 = 2\n", encoding="utf-8")

    hermes_runtime = _DeterministicHermesRuntime()
    db_path = tmp_path / "rlm-e2e-trace.db"
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    persisted_store = EventStore(f"sqlite+aiosqlite:///{db_path}", read_only=True)
    await persisted_store.initialize(create_schema=False)
    try:
        persisted_events = await persisted_store.replay("rlm_run", result.generation_id)
    finally:
        await persisted_store.close()

    assert result.status == "completed"
    assert result.hermes_subcall_count == 3
    assert len(hermes_runtime.prompts) == 3

    scaffold_state = result.outer_scaffold_state
    assert scaffold_state is not None

    expected_parent_call_id = "rlm_call_atomic_synthesis"
    expected_parent_trace_id = "rlm_trace_rlm_call_atomic_synthesis"
    expected_child_ac_ids = (
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    )
    child_acs = scaffold_state.ac_tree.get_children("rlm_ac_root")
    assert tuple(child.id for child in child_acs) == expected_child_ac_ids

    successful_records = [record for record in records if record.success is True]
    records_by_call_id = {record.call_id: record for record in successful_records}
    parent_record = records_by_call_id[expected_parent_call_id]
    assert parent_record.mode == RLM_HERMES_SYNTHESIZE_PARENT_MODE
    assert parent_record.trace_id == expected_parent_trace_id
    assert parent_record.generated_child_ac_node_ids == expected_child_ac_ids

    for index, child_ac in enumerate(child_acs, start=1):
        child_call_id = f"rlm_call_atomic_chunk_{index:03d}"
        child_record = records_by_call_id[child_call_id]

        assert child_ac.parent_id == "rlm_ac_root"
        assert child_ac.originating_subcall_trace_id == expected_parent_trace_id
        assert child_ac.metadata["originating_subcall_trace_id"] == expected_parent_trace_id
        assert child_ac.metadata["chunk_id"] == f"large.py:{index}-{index}"

        assert child_record.ac_node_id == child_ac.id
        assert child_record.parent_call_id == expected_parent_call_id
        assert child_record.parent_trace_id == expected_parent_trace_id
        assert child_record.causal_parent_event_id == expected_parent_call_id
        assert child_record.depth == 1

    succeeded_events = [
        event
        for event in events
        if event.type == RLM_HERMES_CALL_SUCCEEDED_EVENT
        and event.data["hermes"]["call_id"] == expected_parent_call_id
    ]
    assert len(succeeded_events) == 1
    parent_event = succeeded_events[0]
    assert parent_event.data["trace"]["trace_id"] == expected_parent_trace_id
    assert parent_event.data["ac_node"]["child_ids"] == list(expected_child_ac_ids)
    assert parent_event.data["replay"]["creates_ac_node_ids"] == list(expected_child_ac_ids)

    persisted_links = verify_rlm_subcall_child_ac_links(persisted_events)
    assert len(persisted_links) == 1
    persisted_link = persisted_links[0]
    assert persisted_link.parent_call_id == expected_parent_call_id
    assert persisted_link.parent_trace_id == expected_parent_trace_id
    assert persisted_link.parent_ac_node_id == "rlm_ac_root"
    assert persisted_link.generated_child_ac_node_ids == expected_child_ac_ids
    assert persisted_link.child_call_ids == (
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
    )
    assert persisted_link.child_trace_ids == (
        "rlm_trace_rlm_call_atomic_chunk_001",
        "rlm_trace_rlm_call_atomic_chunk_002",
    )
    assert persisted_link.parent_lifecycle_statuses == ("started", "succeeded")
    assert persisted_link.child_lifecycle_statuses == (
        "started",
        "succeeded",
        "started",
        "succeeded",
    )
    assert [event.type for event in persisted_events] == [
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
    ]
