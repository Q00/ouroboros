"""Unit tests for RLM trace serialization and replay helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.rlm.trace import (
    RLM_HERMES_CALL_COMPLETED_EVENT,
    RLM_HERMES_CALL_FAILED_EVENT,
    RLM_HERMES_CALL_STARTED_EVENT,
    RLM_HERMES_CALL_SUCCEEDED_EVENT,
    RLM_TRACE_SCHEMA_VERSION,
    RLMHermesTraceRecord,
    RLMTraceStore,
    create_rlm_hermes_trace_event,
    hash_trace_text,
    replay_rlm_hermes_trace_records,
    rlm_hermes_trace_records_from_events,
)


def test_trace_record_round_trips_current_flat_payload() -> None:
    """Current flat trace fragments deserialize without field loss."""
    record = RLMHermesTraceRecord(
        prompt="bounded prompt",
        completion="bounded completion",
        parent_call_id="rlm_call_parent",
        depth=2,
        call_id="rlm_call_child",
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_child",
        ac_node_id="rlm_ac_child",
        exit_code=0,
    )

    restored = RLMHermesTraceRecord.from_dict(record.to_dict())

    assert restored == record


def test_trace_record_loads_legacy_sparse_payload_with_defaults() -> None:
    """Records written before new trace fields remain replayable."""
    restored = RLMHermesTraceRecord.from_dict(
        {
            "prompt": "legacy prompt",
            "completion": "legacy completion",
            "parent_call_id": "legacy_parent",
            "depth": 1,
        }
    )

    assert restored.schema_version == RLM_TRACE_SCHEMA_VERSION
    assert restored.prompt == "legacy prompt"
    assert restored.completion == "legacy completion"
    assert restored.parent_call_id == "legacy_parent"
    assert restored.depth == 1
    assert restored.call_id is None
    assert restored.trace_id is None
    assert restored.subcall_id is None
    assert restored.parent_trace_id is None
    assert restored.causal_parent_event_id is None
    assert restored.generated_child_ac_node_ids == ()
    assert restored.mode == "none"
    assert restored.generation_id is None
    assert restored.rlm_node_id is None
    assert restored.ac_node_id is None
    assert restored.exit_code is None
    assert restored.runtime == "hermes"


def test_trace_record_loads_nested_event_payload_shape() -> None:
    """The documented nested EventStore trace shape rehydrates to one record."""
    restored = RLMHermesTraceRecord.from_event_data(
        {
            "schema_version": RLM_TRACE_SCHEMA_VERSION,
            "rlm_run_id": "rlm_generation_0",
            "generation_id": "rlm_generation_0",
            "mode": "decompose_ac",
            "rlm_node": {"id": "rlm_node_parent", "depth": 0},
            "ac_node": {"id": "ac_parent", "depth": 0},
            "hermes": {
                "call_id": "rlm_call_parent",
                "parent_call_id": None,
                "runtime": "hermes",
                "prompt": "nested prompt",
                "completion": "nested completion",
                "depth": 0,
                "exit_code": 0,
            },
        }
    )

    assert restored.schema_version == RLM_TRACE_SCHEMA_VERSION
    assert restored.call_id == "rlm_call_parent"
    assert restored.subcall_id == "rlm_call_parent"
    assert restored.mode == "decompose_ac"
    assert restored.generation_id == "rlm_generation_0"
    assert restored.rlm_node_id == "rlm_node_parent"
    assert restored.ac_node_id == "ac_parent"
    assert restored.prompt == "nested prompt"
    assert restored.completion == "nested completion"
    assert restored.depth == 0
    assert restored.exit_code == 0


def test_trace_record_round_trips_new_hermes_subcall_fields() -> None:
    """Trace write/read helpers preserve the Hermes sub-call metadata contract."""
    record = RLMHermesTraceRecord(
        prompt="bounded prompt",
        completion="bounded completion",
        parent_call_id="rlm_call_parent",
        depth=2,
        trace_id="trace_child",
        subcall_id="subcall_child",
        parent_trace_id="trace_parent",
        causal_parent_event_id="event_parent",
        call_id="rlm_call_child",
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_child",
        ac_node_id="rlm_ac_child",
        selected_chunk_ids=("chunk-a", "chunk-b"),
        generated_child_ac_node_ids=("rlm_ac_grandchild_1", "rlm_ac_grandchild_2"),
        resume_handle_id="hermes-resume-1",
        runtime_handle_id="hermes-runtime-1",
        prompt_hash=hash_trace_text("bounded prompt"),
        response_hash=hash_trace_text("bounded completion"),
        success=True,
        exit_code=0,
        elapsed_ms=1250,
        adapter_error={"provider": "hermes", "message": "transient retry"},
        system_prompt_hash="sha256:system-policy",
    )

    payload = record.to_dict()
    assert payload["trace_id"] == "trace_child"
    assert payload["subcall_id"] == "subcall_child"
    assert payload["parent_trace_id"] == "trace_parent"
    assert payload["causal_parent_event_id"] == "event_parent"
    assert payload["selected_chunk_ids"] == ["chunk-a", "chunk-b"]
    assert payload["generated_child_ac_node_ids"] == [
        "rlm_ac_grandchild_1",
        "rlm_ac_grandchild_2",
    ]
    assert payload["resume_handle_id"] == "hermes-resume-1"
    assert payload["runtime_handle_id"] == "hermes-runtime-1"
    assert payload["prompt_hash"] == hash_trace_text("bounded prompt")
    assert payload["response_hash"] == hash_trace_text("bounded completion")
    assert payload["success"] is True
    assert payload["elapsed_ms"] == 1250
    assert payload["adapter_error"] == {"provider": "hermes", "message": "transient retry"}
    assert payload["system_prompt_hash"] == "sha256:system-policy"

    assert RLMHermesTraceRecord.from_dict(payload) == record

    event_data = record.to_event_data()
    assert event_data["trace_id"] == "trace_child"
    assert event_data["subcall_id"] == "subcall_child"
    assert event_data["parent_trace_id"] == "trace_parent"
    assert event_data["causal_parent_event_id"] == "event_parent"
    assert event_data["trace"] == {
        "trace_id": "trace_child",
        "subcall_id": "subcall_child",
        "parent_call_id": "rlm_call_parent",
        "parent_trace_id": "trace_parent",
        "causal_parent_event_id": "event_parent",
        "depth": 2,
        "selected_chunk_ids": ["chunk-a", "chunk-b"],
    }
    assert event_data["context"] == {"selected_chunk_ids": ["chunk-a", "chunk-b"]}
    assert event_data["rlm_node"] == {"id": "rlm_node_child", "depth": 2}
    assert event_data["ac_node"] == {
        "id": "rlm_ac_child",
        "depth": 2,
        "child_ids": ["rlm_ac_grandchild_1", "rlm_ac_grandchild_2"],
    }
    assert event_data["replay"] == {
        "creates_ac_node_ids": ["rlm_ac_grandchild_1", "rlm_ac_grandchild_2"],
        "generated_child_ac_node_ids": ["rlm_ac_grandchild_1", "rlm_ac_grandchild_2"],
    }
    assert event_data["atomic_ac_execution"] == {
        "rlm_node_id": "rlm_node_child",
        "ac_node_id": "rlm_ac_child",
        "call_id": "rlm_call_child",
        "subcall_id": "subcall_child",
        "parent_call_id": "rlm_call_parent",
        "depth": 2,
        "selected_chunk_ids": ["chunk-a", "chunk-b"],
        "input": "bounded prompt",
        "output": "bounded completion",
        "exit_code": 0,
        "success": True,
    }
    assert event_data["recursion"] == {
        "trace_id": "trace_child",
        "subcall_id": "subcall_child",
        "call_id": "rlm_call_child",
        "parent_call_id": "rlm_call_parent",
        "parent_trace_id": "trace_parent",
        "causal_parent_event_id": "event_parent",
        "depth": 2,
        "rlm_node_id": "rlm_node_child",
        "ac_node_id": "rlm_ac_child",
        "selected_chunk_ids": ["chunk-a", "chunk-b"],
        "generated_child_ac_node_ids": ["rlm_ac_grandchild_1", "rlm_ac_grandchild_2"],
    }
    assert RLMHermesTraceRecord.from_event_data(event_data) == record
    assert RLMHermesTraceRecord.from_event_data_many(event_data) == (record,)


def test_trace_record_loads_replay_causal_link_fields() -> None:
    """Replay-oriented causal fields hydrate from nested trace/replay payloads."""
    restored = RLMHermesTraceRecord.from_event_data(
        {
            "schema_version": RLM_TRACE_SCHEMA_VERSION,
            "trace_id": "trace_child",
            "generation_id": "rlm_generation_0",
            "causal_parent_event_id": "event_parent",
            "mode": "decompose_ac",
            "trace": {
                "subcall_id": "subcall_child",
                "parent_call_id": "rlm_call_parent",
                "parent_trace_id": "trace_parent",
                "depth": 1,
            },
            "recursion": {
                "call_id": "rlm_call_child",
                "rlm_node_id": "rlm_node_child",
                "ac_node_id": "rlm_ac_child",
            },
            "ac_node": {
                "id": "rlm_ac_child",
                "depth": 1,
                "child_ids": ["rlm_ac_grandchild_1"],
            },
            "replay": {
                "generated_child_ac_node_ids": [
                    "rlm_ac_grandchild_1",
                    "rlm_ac_grandchild_2",
                ]
            },
            "hermes": {
                "prompt": "decompose prompt",
                "completion": "decompose completion",
                "runtime": "hermes",
            },
        }
    )

    assert restored.trace_id == "trace_child"
    assert restored.subcall_id == "subcall_child"
    assert restored.call_id == "rlm_call_child"
    assert restored.parent_call_id == "rlm_call_parent"
    assert restored.parent_trace_id == "trace_parent"
    assert restored.causal_parent_event_id == "event_parent"
    assert restored.depth == 1
    assert restored.generated_child_ac_node_ids == (
        "rlm_ac_grandchild_1",
        "rlm_ac_grandchild_2",
    )


def test_trace_reader_returns_subquestion_hermes_call_records() -> None:
    """Trace readers can reconstruct Hermes calls embedded in decomposition payloads."""
    first = RLMHermesTraceRecord(
        prompt="decompose prompt",
        completion="decompose completion",
        mode="decompose_ac",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_parent",
        ac_node_id="ac_parent",
        prompt_hash=hash_trace_text("decompose prompt"),
        response_hash=hash_trace_text("decompose completion"),
        success=True,
        exit_code=0,
    )
    second = RLMHermesTraceRecord(
        prompt="follow-up prompt",
        completion="follow-up completion",
        mode="decompose_ac",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_parent",
        ac_node_id="ac_parent",
        selected_chunk_ids=("chunk-c",),
        success=False,
        exit_code=1,
        adapter_error={"provider": "hermes", "message": "invalid JSON"},
    )

    records = RLMHermesTraceRecord.from_event_data_many(
        {
            "hermes_subquestion_results": [
                {"child_ac_id": "ac_child_1", "hermes_call": first.to_dict()},
                {"child_ac_id": "ac_child_2", "hermes_call": second.to_dict()},
            ]
        }
    )

    assert records == (first, second)
    assert (
        RLMHermesTraceRecord.from_event_data(
            {
                "hermes_subquestion_results": [
                    {"child_ac_id": "ac_child_1", "hermes_call": first.to_dict()}
                ]
            }
        )
        == first
    )


def test_trace_reader_reconstructs_multiple_hermes_results_with_parent_links() -> None:
    """Replay extracts sibling Hermes results without dropping parent call links."""
    first = RLMHermesTraceRecord(
        prompt="first chunk prompt",
        completion="first chunk result",
        parent_call_id="rlm_call_atomic_synthesis",
        depth=1,
        call_id="rlm_call_atomic_chunk_001",
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_atomic_chunk_001",
        ac_node_id="rlm_ac_atomic_chunk_001",
        selected_chunk_ids=("src/a.py:1-20",),
        success=True,
        exit_code=0,
    )
    second = RLMHermesTraceRecord(
        prompt="second chunk prompt",
        completion="second chunk result",
        parent_call_id="rlm_call_atomic_synthesis",
        depth=1,
        call_id="rlm_call_atomic_chunk_002",
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_atomic_chunk_002",
        ac_node_id="rlm_ac_atomic_chunk_002",
        selected_chunk_ids=("src/b.py:1-20",),
        success=True,
        exit_code=0,
    )
    event = BaseEvent(
        type=RLM_HERMES_CALL_COMPLETED_EVENT,
        aggregate_type="rlm_run",
        aggregate_id="rlm_generation_0",
        data={
            "hermes_subcalls": [
                {
                    "child_result_id": "rlm_node_root:child_result:000",
                    "hermes_call": first.to_dict(),
                },
                {"child_result_id": "rlm_node_root:child_result:001", **second.to_event_data()},
            ],
        },
    )

    records = rlm_hermes_trace_records_from_events([event])

    assert records == (first, second)
    assert [record.call_id for record in records] == [
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
    ]
    assert [record.parent_call_id for record in records] == [
        "rlm_call_atomic_synthesis",
        "rlm_call_atomic_synthesis",
    ]
    assert [record.rlm_node_id for record in records] == [
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
    ]
    assert [record.ac_node_id for record in records] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    ]
    assert [record.selected_chunk_ids for record in records] == [
        ("src/a.py:1-20",),
        ("src/b.py:1-20",),
    ]


def test_trace_reader_preserves_nested_parent_call_chain_and_depths() -> None:
    """Replay preserves root, child, and grandchild call ancestry."""
    root = RLMHermesTraceRecord(
        prompt="root synthesis prompt",
        completion="root synthesis completion",
        parent_call_id=None,
        depth=0,
        trace_id="trace_root",
        subcall_id="subcall_root",
        call_id="call_root",
        mode="synthesize_parent",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_node_root",
        ac_node_id="ac_root",
        generated_child_ac_node_ids=("ac_child",),
        exit_code=0,
    )
    child = RLMHermesTraceRecord(
        prompt="child decomposition prompt",
        completion="child decomposition completion",
        parent_call_id=root.call_id,
        depth=1,
        trace_id="trace_child",
        subcall_id="subcall_child",
        parent_trace_id=root.trace_id,
        causal_parent_event_id=root.call_id,
        call_id="call_child",
        mode="decompose_ac",
        generation_id=root.generation_id,
        rlm_node_id="rlm_node_child",
        ac_node_id="ac_child",
        generated_child_ac_node_ids=("ac_grandchild",),
        exit_code=0,
    )
    grandchild = RLMHermesTraceRecord(
        prompt="grandchild atomic prompt",
        completion="grandchild atomic completion",
        parent_call_id=child.call_id,
        depth=2,
        trace_id="trace_grandchild",
        subcall_id="subcall_grandchild",
        parent_trace_id=child.trace_id,
        causal_parent_event_id=child.call_id,
        call_id="call_grandchild",
        mode="execute_atomic",
        generation_id=root.generation_id,
        rlm_node_id="rlm_node_grandchild",
        ac_node_id="ac_grandchild",
        selected_chunk_ids=("src/rlm.py:1-10",),
        exit_code=0,
    )
    event = BaseEvent(
        type=RLM_HERMES_CALL_COMPLETED_EVENT,
        aggregate_type="rlm_run",
        aggregate_id="rlm_generation_0",
        data={
            "hermes_subcalls": [
                {"hermes_call": root.to_dict()},
                child.to_event_data(),
                grandchild.to_event_data(),
            ],
        },
    )

    records = rlm_hermes_trace_records_from_events([event])
    records_by_call_id = {record.call_id: record for record in records}

    assert [record.call_id for record in records] == [
        "call_root",
        "call_child",
        "call_grandchild",
    ]
    assert {
        call_id: (record.parent_call_id, record.depth)
        for call_id, record in records_by_call_id.items()
    } == {
        "call_root": (None, 0),
        "call_child": ("call_root", 1),
        "call_grandchild": ("call_child", 2),
    }
    assert records_by_call_id["call_child"].parent_trace_id == root.trace_id
    assert records_by_call_id["call_child"].causal_parent_event_id == root.call_id
    assert records_by_call_id["call_grandchild"].parent_trace_id == child.trace_id
    assert records_by_call_id["call_grandchild"].causal_parent_event_id == child.call_id


@pytest.mark.asyncio
async def test_event_store_replay_preserves_rlm_trace_fields(tmp_path) -> None:
    """RLM trace payload fields survive append and replay through EventStore."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-trace.db'}")
    await store.initialize()
    try:
        record = RLMHermesTraceRecord(
            prompt="persisted prompt",
            completion="persisted completion",
            parent_call_id="rlm_call_parent",
            depth=1,
            trace_id="trace_persisted_child",
            subcall_id="subcall_persisted_child",
            parent_trace_id="trace_persisted_parent",
            causal_parent_event_id="event_persisted_parent",
            call_id="rlm_call_child",
            mode="execute_atomic",
            generation_id="rlm_generation_0",
            rlm_node_id="rlm_node_child",
            ac_node_id="rlm_ac_child",
            selected_chunk_ids=("chunk-persisted-a", "chunk-persisted-b"),
            generated_child_ac_node_ids=("rlm_ac_grandchild_1", "rlm_ac_grandchild_2"),
            exit_code=0,
        )
        await store.append(create_rlm_hermes_trace_event(record))

        replayed = await store.replay("rlm_run", "rlm_generation_0")
    finally:
        await store.close()

    assert len(replayed) == 1
    assert replayed[0].event_version == 1
    assert replayed[0].data["trace_id"] == "trace_persisted_child"
    assert replayed[0].data["trace"] == {
        "trace_id": "trace_persisted_child",
        "subcall_id": "subcall_persisted_child",
        "parent_call_id": "rlm_call_parent",
        "parent_trace_id": "trace_persisted_parent",
        "causal_parent_event_id": "event_persisted_parent",
        "depth": 1,
        "selected_chunk_ids": ["chunk-persisted-a", "chunk-persisted-b"],
    }
    assert replayed[0].data["replay"] == {
        "creates_ac_node_ids": ["rlm_ac_grandchild_1", "rlm_ac_grandchild_2"],
        "generated_child_ac_node_ids": ["rlm_ac_grandchild_1", "rlm_ac_grandchild_2"],
    }
    restored = RLMHermesTraceRecord.from_event_data(replayed[0].data)
    assert restored == record


@pytest.mark.asyncio
async def test_event_store_replay_preserves_parent_child_causal_chain(tmp_path) -> None:
    """Persisted RLM traces reconstruct parent, child, and AC creation links."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-causal-chain.db'}")
    await store.initialize()
    parent = RLMHermesTraceRecord(
        prompt="parent synthesis prompt",
        completion="parent synthesis completion",
        depth=0,
        trace_id="trace_parent",
        subcall_id="call_parent",
        call_id="call_parent",
        mode="synthesize_parent",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_parent",
        ac_node_id="ac_parent",
        selected_chunk_ids=("chunk-a", "chunk-b"),
        generated_child_ac_node_ids=("ac_child_1", "ac_child_2"),
        exit_code=0,
    )
    children = (
        RLMHermesTraceRecord(
            prompt="first child prompt",
            completion="first child completion",
            parent_call_id=parent.call_id,
            depth=1,
            trace_id="trace_child_1",
            subcall_id="call_child_1",
            parent_trace_id=parent.trace_id,
            causal_parent_event_id=parent.call_id,
            call_id="call_child_1",
            mode="execute_atomic",
            generation_id=parent.generation_id,
            rlm_node_id="rlm_child_1",
            ac_node_id="ac_child_1",
            selected_chunk_ids=("chunk-a",),
            exit_code=0,
        ),
        RLMHermesTraceRecord(
            prompt="second child prompt",
            completion="second child completion",
            parent_call_id=parent.call_id,
            depth=1,
            trace_id="trace_child_2",
            subcall_id="call_child_2",
            parent_trace_id=parent.trace_id,
            causal_parent_event_id=parent.call_id,
            call_id="call_child_2",
            mode="execute_atomic",
            generation_id=parent.generation_id,
            rlm_node_id="rlm_child_2",
            ac_node_id="ac_child_2",
            selected_chunk_ids=("chunk-b",),
            exit_code=0,
        ),
    )

    try:
        await store.append_batch(
            [create_rlm_hermes_trace_event(record) for record in (parent, *children)]
        )
        events = await store.replay("rlm_run", "rlm_generation_0")
    finally:
        await store.close()

    records = rlm_hermes_trace_records_from_events(events)
    by_trace_id = {record.trace_id: record for record in records}
    parent_record = by_trace_id["trace_parent"]
    child_records = [by_trace_id["trace_child_1"], by_trace_id["trace_child_2"]]

    assert parent_record.generated_child_ac_node_ids == ("ac_child_1", "ac_child_2")
    assert [child.ac_node_id for child in child_records] == ["ac_child_1", "ac_child_2"]
    assert [child.parent_trace_id for child in child_records] == [
        parent_record.trace_id,
        parent_record.trace_id,
    ]
    assert [child.causal_parent_event_id for child in child_records] == [
        parent_record.call_id,
        parent_record.call_id,
    ]

    parent_event = next(
        event for event in events if event.data["hermes"]["call_id"] == "call_parent"
    )
    assert parent_event.data["ac_node"]["child_ids"] == ["ac_child_1", "ac_child_2"]
    assert parent_event.data["replay"]["creates_ac_node_ids"] == [
        "ac_child_1",
        "ac_child_2",
    ]


def test_legacy_db_row_without_event_version_keeps_trace_payload_replayable() -> None:
    """Pre-version EventStore rows can still hydrate the new trace model."""
    event = BaseEvent.from_db_row(
        {
            "id": "legacy-rlm-event",
            "event_type": "rlm.hermes.call.completed",
            "timestamp": datetime.now(UTC),
            "aggregate_type": "rlm_run",
            "aggregate_id": "rlm_generation_0",
            "payload": {
                "hermes_call": {
                    "prompt": "legacy persisted prompt",
                    "response": "legacy persisted completion",
                    "parent_call_id": "legacy_parent",
                    "depth": 2,
                }
            },
        }
    )

    restored = RLMHermesTraceRecord.from_event_data(event.data)

    assert event.event_version == 0
    assert restored.prompt == "legacy persisted prompt"
    assert restored.completion == "legacy persisted completion"
    assert restored.parent_call_id == "legacy_parent"
    assert restored.depth == 2
    assert restored.call_id is None
    assert restored.subcall_id is None
    assert restored.schema_version == RLM_TRACE_SCHEMA_VERSION


def test_legacy_subquestion_wrapper_child_ac_id_associates_nested_trace() -> None:
    """Legacy decomposition wrappers can supply child AC IDs for nested calls."""
    event = BaseEvent(
        type=RLM_HERMES_CALL_COMPLETED_EVENT,
        aggregate_type="rlm_run",
        aggregate_id="rlm_generation_0",
        data={
            "hermes_subquestion_results": [
                {
                    "child_ac_id": "legacy_child_ac_1",
                    "child_node_id": "legacy_rlm_node_1",
                    "hermes_call": {
                        "prompt": "legacy child prompt",
                        "response": "legacy child response",
                        "call_id": "legacy_call_child_1",
                        "parent_call_id": "legacy_call_parent",
                        "parent_trace_record_id": "legacy_trace_parent",
                        "parent_event_id": "legacy_event_parent",
                        "chunk_id": "legacy_chunk_1",
                        "depth": 2,
                    },
                }
            ]
        },
    )

    (record,) = rlm_hermes_trace_records_from_events([event])

    assert record.prompt == "legacy child prompt"
    assert record.completion == "legacy child response"
    assert record.call_id == "legacy_call_child_1"
    assert record.parent_call_id == "legacy_call_parent"
    assert record.parent_trace_id == "legacy_trace_parent"
    assert record.causal_parent_event_id == "legacy_event_parent"
    assert record.selected_chunk_ids == ("legacy_chunk_1",)
    assert record.ac_node_id == "legacy_child_ac_1"
    assert record.rlm_node_id == "legacy_rlm_node_1"


def test_trace_reader_loads_explicit_recursion_metadata() -> None:
    """Replay accepts recursion metadata even when IDs are not duplicated elsewhere."""
    restored = RLMHermesTraceRecord.from_event_data(
        {
            "schema_version": RLM_TRACE_SCHEMA_VERSION,
            "generation_id": "rlm_generation_0",
            "mode": "execute_atomic",
            "hermes": {
                "prompt": "recursion prompt",
                "completion": "recursion completion",
                "runtime": "hermes",
            },
            "recursion": {
                "call_id": "rlm_call_child",
                "parent_call_id": "rlm_call_parent",
                "depth": 3,
                "rlm_node_id": "rlm_node_child",
                "ac_node_id": "rlm_ac_child",
                "selected_chunk_ids": ["chunk-1"],
            },
        }
    )

    assert restored.call_id == "rlm_call_child"
    assert restored.parent_call_id == "rlm_call_parent"
    assert restored.depth == 3
    assert restored.rlm_node_id == "rlm_node_child"
    assert restored.ac_node_id == "rlm_ac_child"
    assert restored.selected_chunk_ids == ("chunk-1",)


@pytest.mark.asyncio
async def test_rlm_trace_store_persists_and_replays_hermes_subcalls(tmp_path) -> None:
    """The storage helper writes replayable Hermes prompt/completion records."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-trace-store.db'}")
    await store.initialize()
    trace_store = RLMTraceStore(store)
    try:
        started = RLMHermesTraceRecord(
            prompt="started prompt",
            completion="",
            parent_call_id="rlm_call_parent",
            depth=1,
            call_id="rlm_call_child",
            mode="execute_atomic",
            generation_id="rlm_generation_0",
            rlm_node_id="rlm_node_child",
            ac_node_id="rlm_ac_child",
            selected_chunk_ids=("chunk-1",),
        )
        completed = RLMHermesTraceRecord(
            prompt="started prompt",
            completion="completed response",
            parent_call_id="rlm_call_parent",
            depth=1,
            call_id="rlm_call_child",
            mode="execute_atomic",
            generation_id="rlm_generation_0",
            rlm_node_id="rlm_node_child",
            ac_node_id="rlm_ac_child",
            selected_chunk_ids=("chunk-1",),
            success=True,
            exit_code=0,
        )
        failed = RLMHermesTraceRecord(
            prompt="failed prompt",
            completion="failed response",
            parent_call_id="rlm_call_parent",
            depth=1,
            call_id="rlm_call_failed",
            mode="execute_atomic",
            generation_id="rlm_generation_0",
            rlm_node_id="rlm_node_failed",
            ac_node_id="rlm_ac_failed",
            selected_chunk_ids=("chunk-failed",),
            success=False,
            exit_code=1,
            adapter_error={"provider": "hermes", "message": "failed response"},
        )

        started_event = await trace_store.append_hermes_call_started(started)
        succeeded_event = await trace_store.append_hermes_call_succeeded(completed)
        failed_event = await trace_store.append_hermes_call_failed(failed)
        completed_event = await trace_store.append_hermes_call_completed(completed)

        replayed = await trace_store.replay_hermes_subcalls("rlm_generation_0")
        replayed_via_function = await replay_rlm_hermes_trace_records(
            store,
            "rlm_generation_0",
        )
    finally:
        await store.close()

    assert started_event.type == RLM_HERMES_CALL_STARTED_EVENT
    assert succeeded_event.type == RLM_HERMES_CALL_SUCCEEDED_EVENT
    assert failed_event.type == RLM_HERMES_CALL_FAILED_EVENT
    assert completed_event.type == RLM_HERMES_CALL_COMPLETED_EVENT
    assert started_event.data["lifecycle"]["status"] == "started"
    assert succeeded_event.data["lifecycle"]["status"] == "succeeded"
    assert failed_event.data["lifecycle"]["status"] == "failed"
    assert completed_event.data["lifecycle"]["status"] == "completed"
    assert failed_event.data["hermes"]["adapter_error"] == {
        "provider": "hermes",
        "message": "failed response",
    }
    assert replayed == (started, completed, failed, completed)
    assert replayed_via_function == (started, completed, failed, completed)


def test_create_rlm_hermes_trace_event_requires_supported_event_type() -> None:
    """The storage helper only emits the RLM Hermes trace lifecycle events."""
    record = RLMHermesTraceRecord(generation_id="rlm_generation_0")

    with pytest.raises(ValueError, match="RLM Hermes trace event_type"):
        create_rlm_hermes_trace_event(record, event_type="rlm.unsupported")
