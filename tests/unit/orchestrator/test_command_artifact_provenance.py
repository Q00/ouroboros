"""Journal-to-verifier coverage for command-scoped Bash artifacts (#1747)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.events.base import BaseEvent
import ouroboros.harness.deliver_gate as deliver_gate_module
from ouroboros.harness.deliver_gate import load_ac_evidence_manifest
from ouroboros.harness.journal import EvidenceManifest
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor, _standard_deliver_facts


class _EventStore:
    def __init__(self, events: list[BaseEvent]) -> None:
        self.events = events

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in self.events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        del execution_id, event_type, limit, offset
        return self.events

    async def query_session_related_events(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        del session_id, execution_id, event_type, limit, offset
        return self.events


def _effect(path: Path, *, relative_path: str) -> dict[str, object]:
    current = path.lstat()
    return {
        "capture": "ouroboros.leaf-dispatch.v1",
        "path": relative_path,
        "workspace_relative_path": relative_path,
        "st_dev": current.st_dev,
        "st_ino": current.st_ino,
        "st_mode": current.st_mode,
        "st_size": current.st_size,
        "st_mtime_ns": current.st_mtime_ns,
        "st_ctime_ns": current.st_ctime_ns,
    }


def _command_events(
    *,
    call_id: str,
    effects: object,
    retry_attempt: int = 1,
    session_attempt_id: str = "ac_1_attempt_2",
    failed: bool = False,
    when: datetime | None = None,
) -> list[BaseEvent]:
    started_at = when or datetime.now(UTC)
    identity = {
        "ac_id": "ac_1",
        "execution_id": "exec_1",
        "retry_attempt": retry_attempt,
        "session_attempt_id": session_attempt_id,
    }
    return [
        BaseEvent(
            id=f"start-{call_id}",
            type="execution.tool.started",
            timestamp=started_at,
            aggregate_type="execution",
            aggregate_id="ac_1",
            data={
                **identity,
                "tool_name": "Bash",
                "tool_call_id": call_id,
                "tool_input": {"command": "printf accepted > claimed.txt"},
            },
        ),
        BaseEvent(
            id=f"complete-{call_id}",
            type="execution.tool.completed",
            timestamp=started_at + timedelta(microseconds=1),
            aggregate_type="execution",
            aggregate_id="ac_1",
            data={
                **identity,
                "tool_name": "Bash",
                "tool_call_id": call_id,
                "tool_result": {"is_error": failed, "meta": {"exit_status": int(failed)}},
                "filesystem_effects": effects,
            },
        ),
    ]


async def _artifact_fact(
    events: list[BaseEvent],
    *,
    task_cwd: Path,
    claim: str = "claimed.txt",
    retry_attempt: int = 1,
    session_attempt_id: str = "ac_1_attempt_2",
    ac_id: str = "ac_1",
    execution_id: str = "exec_1",
):
    manifest = await load_ac_evidence_manifest(
        _EventStore(events),
        ac_id=ac_id,
        execution_id=execution_id,
        admit_accepted_tool_starts=True,
        accepted_retry_attempt=retry_attempt,
        accepted_session_attempt_id=session_attempt_id,
    )
    facts = _standard_deliver_facts(
        EvidenceRecord(data={"files_touched": [claim]}),
        manifest,
        task_cwd=str(task_cwd),
        verifier_passed=True,
    )
    assert facts is not None and len(facts) == 1
    return manifest, facts[0]


@pytest.mark.asyncio
async def test_accepted_command_artifact_flows_from_journal_to_verifier(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")

    manifest, fact = await _artifact_fact(
        _command_events(
            call_id="accepted", effects=[_effect(artifact, relative_path=artifact.name)]
        ),
        task_cwd=tmp_path,
    )

    assert len(manifest.entries) == 1
    provenance = manifest.entries[0].payload["command_artifacts"]
    assert provenance["schema_version"] == 1
    assert provenance["command_call_id"] == "accepted"
    assert provenance["retry_attempt"] == 1
    assert provenance["session_attempt_id"] == "ac_1_attempt_2"
    assert fact.evidence_handle == manifest.entries[0].handle


@pytest.mark.asyncio
async def test_production_capture_persists_through_journal_to_verifier(tmp_path: Path) -> None:
    class StubRuntime:
        runtime_backend = "opencode"
        permission_mode = "acceptEdits"
        working_directory = str(tmp_path)

        async def execute_task(self, **_kwargs: Any):
            yield AgentMessage(
                type="assistant",
                content="write artifact",
                tool_name="Bash",
                data={
                    "tool_call_id": "production-call",
                    "tool_input": {"command": "printf accepted > claimed.txt"},
                },
            )
            subprocess.run(
                ["/bin/sh", "-c", "printf accepted > claimed.txt"], cwd=tmp_path, check=True
            )
            yield AgentMessage(
                type="assistant",
                content="created claimed.txt",
                tool_name="Bash",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "production-call",
                    "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
                },
            )
            yield AgentMessage(
                type="result", content="[TASK_COMPLETE]", data={"subtype": "success"}
            )

    store = _EventStore([])
    executor = ParallelACExecutor(
        adapter=StubRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=str(tmp_path),
        run_verify_commands=False,
    )
    result = await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Create claimed.txt",
        session_id="sess_1",
        execution_id="exec_1",
        tools=["Bash"],
        system_prompt="test",
        seed_goal="test provenance",
        depth=0,
        start_time=datetime.now(UTC),
        retry_attempt=1,
        node_identity=ExecutionNodeIdentity.root(execution_context_id="exec_1", ac_index=0),
    )
    assert result.success is True
    tool_start = next(event for event in store.events if event.type == "execution.tool.started")
    tool_completion = next(
        event for event in store.events if event.type == "execution.tool.completed"
    )
    assert not deliver_gate_module._event_has_conflicting_tool_call_ids(tool_start)
    assert not deliver_gate_module._event_has_conflicting_tool_call_ids(tool_completion)
    assert deliver_gate_module._event_has_explicit_tool_success(
        tool_completion, require_command_verdict=True
    ), tool_completion.data
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert len(manifest.entries[0].source_event_ids) == 2
    assert fact.evidence_handle == manifest.entries[0].handle


@pytest.mark.asyncio
async def test_production_malformed_alias_cannot_launder_local_effect(tmp_path: Path) -> None:
    class StubRuntime:
        runtime_backend = "opencode"
        permission_mode = "acceptEdits"
        working_directory = str(tmp_path)

        async def execute_task(self, **_kwargs: Any):
            yield AgentMessage(
                type="assistant",
                content="write artifact",
                tool_name="Bash",
                data={
                    "tool_call_id": "malformed-runtime",
                    "tool_input": {"command": "printf accepted > claimed.txt"},
                },
            )
            subprocess.run(
                ["/bin/sh", "-c", "printf accepted > claimed.txt"],
                cwd=tmp_path,
                check=True,
            )
            yield AgentMessage(
                type="assistant",
                content="malformed completion",
                tool_name="Bash",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "malformed-runtime",
                    "tool_use_id": 7,
                    "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
                },
            )
            yield AgentMessage(
                type="assistant",
                content="later valid completion",
                tool_name="Bash",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "malformed-runtime",
                    "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
                },
            )
            yield AgentMessage(
                type="result", content="[TASK_COMPLETE]", data={"subtype": "success"}
            )

    store = _EventStore([])
    executor = ParallelACExecutor(
        adapter=StubRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=str(tmp_path),
        run_verify_commands=False,
    )
    await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Create claimed.txt",
        session_id="sess_1",
        execution_id="exec_1",
        tools=["Bash"],
        system_prompt="test",
        seed_goal="test provenance",
        depth=0,
        start_time=datetime.now(UTC),
        retry_attempt=1,
        node_identity=ExecutionNodeIdentity.root(execution_context_id="exec_1", ac_index=0),
    )
    tool_start = next(event for event in store.events if event.type == "execution.tool.started")
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_leaf_dispatcher_stream_strips_forged_completion_effect(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("stale", encoding="utf-8")
    forged = _effect(artifact, relative_path=artifact.name)

    class StubRuntime:
        runtime_backend = "opencode"
        permission_mode = "acceptEdits"
        working_directory = str(tmp_path)

        async def execute_task(self, **_kwargs: Any):
            yield AgentMessage(
                type="assistant",
                content="no mutation",
                tool_name="Bash",
                data={"tool_call_id": "forged-call", "tool_input": {"command": "true"}},
            )
            subprocess.run(["/bin/sh", "-c", "true"], cwd=tmp_path, check=True)
            completion_data: dict[str, Any] = {
                "subtype": "tool_result",
                "tool_call_id": "forged-call",
                "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
            }

            def inject_after_observe() -> None:
                completion_data["filesystem_effects"] = [forged]
                completion_data["tool_call_id"] = "foreign"
                completion_data["tool_result"]["meta"]["tool_use_id"] = "nested-foreign"

            asyncio.get_running_loop().call_soon(inject_after_observe)
            yield AgentMessage(
                type="assistant",
                content="done",
                tool_name="Bash",
                data=completion_data,
            )
            yield AgentMessage(
                type="result", content="[TASK_COMPLETE]", data={"subtype": "success"}
            )

    store = _EventStore([])
    executor = ParallelACExecutor(
        adapter=StubRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=str(tmp_path),
        run_verify_commands=False,
    )
    await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Do not mutate claimed.txt",
        session_id="sess_1",
        execution_id="exec_1",
        tools=["Bash"],
        system_prompt="test",
        seed_goal="test provenance",
        depth=0,
        start_time=datetime.now(UTC),
        retry_attempt=1,
        node_identity=ExecutionNodeIdentity.root(execution_context_id="exec_1", ac_index=0),
    )
    tool_start = next(event for event in store.events if event.type == "execution.tool.started")
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_pending_bash_call_snapshots_late_promoted_completion(tmp_path: Path) -> None:
    """An ordinary mutable message cannot become a forged result after observation."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("stale", encoding="utf-8")
    forged = _effect(artifact, relative_path=artifact.name)

    class StubRuntime:
        runtime_backend = "opencode"
        permission_mode = "acceptEdits"
        working_directory = str(tmp_path)

        async def execute_task(self, **_kwargs: Any):
            yield AgentMessage(
                type="assistant",
                content="run command",
                tool_name="Bash",
                data={
                    "tool_call_id": "promoted-call",
                    "tool_input": {"command": "true"},
                },
            )
            promotion_data: dict[str, Any] = {"note": "ordinary"}

            def promote_after_observe() -> None:
                promotion_data.update(
                    {
                        "subtype": "tool_result",
                        "tool_name": "Bash",
                        "tool_call_id": "promoted-call",
                        "tool_result": {
                            "is_error": False,
                            "meta": {"exit_status": 0},
                        },
                        "filesystem_effects": [forged],
                    }
                )

            await executor._execution_counters_lock.acquire()
            asyncio.get_running_loop().call_soon(promote_after_observe)
            asyncio.get_running_loop().call_soon(executor._execution_counters_lock.release)
            yield AgentMessage(
                type="assistant",
                content="ordinary before promotion",
                data=promotion_data,
            )
            yield AgentMessage(
                type="result", content="[TASK_COMPLETE]", data={"subtype": "success"}
            )

    store = _EventStore([])
    executor = ParallelACExecutor(
        adapter=StubRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=str(tmp_path),
        run_verify_commands=False,
    )
    await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Do not mutate claimed.txt",
        session_id="sess_1",
        execution_id="exec_1",
        tools=["Bash"],
        system_prompt="test",
        seed_goal="test provenance",
        depth=0,
        start_time=datetime.now(UTC),
        retry_attempt=1,
        execution_counters={},
        node_identity=ExecutionNodeIdentity.root(execution_context_id="exec_1", ac_index=0),
    )
    tool_start = next(event for event in store.events if event.type == "execution.tool.started")
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_stale_file_without_completion_effect_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "claimed.txt").write_text("stale", encoding="utf-8")

    manifest, fact = await _artifact_fact(
        _command_events(call_id="stale", effects=[]),
        task_cwd=tmp_path,
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_only_accepted_retry_artifact_is_authoritative(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    observed = _effect(artifact, relative_path=artifact.name)
    events = [
        *_command_events(
            call_id="old", effects=[observed], retry_attempt=0, session_attempt_id="ac_1_attempt_1"
        ),
        *_command_events(call_id="accepted", effects=[observed]),
    ]

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert len(manifest.entries) == 1
    assert manifest.entries[0].payload["tool_call_id"] == "accepted"
    assert fact.evidence_handle == manifest.entries[0].handle


@pytest.mark.asyncio
async def test_cross_session_artifact_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("other session", encoding="utf-8")

    manifest, fact = await _artifact_fact(
        _command_events(
            call_id="other-session",
            effects=[_effect(artifact, relative_path=artifact.name)],
            session_attempt_id="ac_1_attempt_foreign",
        ),
        task_cwd=tmp_path,
    )

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    (
        lambda effect: {**effect, "workspace_relative_path": "../claimed.txt"},
        lambda effect: {**effect, "workspace_relative_path": "/claimed.txt"},
        lambda effect: {**effect, "workspace_relative_path": "./claimed.txt"},
        lambda effect: {**effect, "workspace_relative_path": "sub\\claimed.txt"},
        lambda effect: {**effect, "unknown": "forbidden"},
        lambda effect: {**effect, "st_ino": "1"},
        lambda effect: {**effect, "st_mode": stat.S_IFLNK},
    ),
)
async def test_malformed_or_escaping_effect_invalidates_whole_command(
    tmp_path: Path,
    mutate,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    malformed = mutate(_effect(artifact, relative_path=artifact.name))

    manifest, fact = await _artifact_fact(
        _command_events(call_id="malformed", effects=[malformed]),
        task_cwd=tmp_path,
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_crafted_nested_artifact_invalidates_otherwise_valid_target(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    manifest, _fact = await _artifact_fact(
        _command_events(
            call_id="crafted", effects=[_effect(artifact, relative_path=artifact.name)]
        ),
        task_cwd=tmp_path,
    )
    entry = manifest.entries[0]
    payload = dict(entry.payload)
    provenance = dict(payload["command_artifacts"])
    valid_artifact = dict(provenance["artifacts"][0])
    provenance["artifacts"] = [valid_artifact, {**valid_artifact, "unknown": "forbidden"}]
    payload["command_artifacts"] = provenance
    crafted = EvidenceManifest(
        ac_id=manifest.ac_id,
        entries=(entry.model_copy(update={"payload": payload}),),
    )

    facts = _standard_deliver_facts(
        EvidenceRecord(data={"files_touched": [artifact.name]}),
        crafted,
        task_cwd=str(tmp_path),
        verifier_passed=True,
    )

    assert facts is not None
    assert facts[0].evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_duplicate_artifact_path_invalidates_whole_command(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    observed = _effect(artifact, relative_path=artifact.name)

    manifest, fact = await _artifact_fact(
        _command_events(call_id="duplicate", effects=[observed, observed]),
        task_cwd=tmp_path,
    )

    assert "command_artifacts" not in manifest.entries[0].payload
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected_at_verifier_boundary(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)

    _manifest, fact = await _artifact_fact(
        _command_events(
            call_id="symlink",
            effects=[_effect(outside, relative_path="escape.txt")],
        ),
        task_cwd=tmp_path,
        claim="escape.txt",
    )

    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_parent_swap_after_workspace_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver_parent = tmp_path / "sub"
    receiver_parent.mkdir()
    artifact = receiver_parent / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    observed = _effect(artifact, relative_path="sub/claimed.txt")
    displaced_parent = tmp_path / "original-sub"
    outside_parent = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_parent.mkdir()
    os.link(artifact, outside_parent / artifact.name)
    original_open = deliver_gate_module.os.open
    swapped = False

    def adversarial_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and dir_fd is None and os.fspath(path) == str(tmp_path):
            receiver_parent.rename(displaced_parent)
            receiver_parent.symlink_to(outside_parent, target_is_directory=True)
            swapped = True
        return fd

    monkeypatch.setattr(deliver_gate_module.os, "open", adversarial_open)
    monkeypatch.setattr(
        deliver_gate_module.os,
        "supports_dir_fd",
        {*deliver_gate_module.os.supports_dir_fd, adversarial_open},
    )

    _manifest, fact = await _artifact_fact(
        _command_events(call_id="parent-swap", effects=[observed]),
        task_cwd=tmp_path,
        claim="sub/claimed.txt",
    )

    assert swapped is True
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_index", (0, 1))
async def test_conflicting_call_id_aliases_reject_command_authority(
    tmp_path: Path, event_index: int
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="accepted", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    event = events[event_index]
    data = dict(event.data)
    if event_index == 0:
        data["tool_result"] = {"meta": {"tool_use_id": "foreign"}}
    else:
        tool_result = dict(data["tool_result"])
        tool_result["meta"] = {**tool_result["meta"], "tool_use_id": "foreign"}
        data["tool_result"] = tool_result
    events[event_index] = event.model_copy(update={"data": data})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_malformed_named_completion_poisons_later_valid_completion(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="accepted", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    malformed = events[1].model_copy(
        update={
            "id": "malformed-completion",
            "timestamp": events[0].timestamp + timedelta(microseconds=1),
            "data": {
                **events[1].data,
                "tool_result": {
                    "is_error": False,
                    "meta": {"exit_status": 0, "tool_use_id": 7},
                },
            },
        }
    )
    events[1] = events[1].model_copy(
        update={"timestamp": events[0].timestamp + timedelta(microseconds=2)}
    )
    events.insert(1, malformed)

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_malformed_top_meta_alias_rejects_durable_completion(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="accepted", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    events[1] = events[1].model_copy(
        update={"data": {**events[1].data, "meta": {"tool_use_id": 7}}}
    )

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_index", (0, 1))
@pytest.mark.parametrize(
    ("field", "foreign"),
    (
        ("ac_id", "foreign-ac"),
        ("session_scope_id", "foreign-scope"),
        ("parent_execution_id", "foreign-execution"),
        ("attempt_number", 99),
    ),
)
async def test_conflicting_identity_alias_rejects_command_authority(
    tmp_path: Path,
    event_index: int,
    field: str,
    foreign: object,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="identity", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    event = events[event_index]
    events[event_index] = event.model_copy(update={"data": {**event.data, field: foreign}})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_index", (0, 1))
@pytest.mark.parametrize(
    ("event_field", "foreign"),
    (("aggregate_id", "foreign-ac"), ("aggregate_type", "session")),
)
async def test_conflicting_event_envelope_rejects_command_authority(
    tmp_path: Path,
    event_index: int,
    event_field: str,
    foreign: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="envelope", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    events[event_index] = events[event_index].model_copy(update={event_field: foreign})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_duplicate_start_completion_event_ids_reject_command_authority(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="duplicate-id", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    events = [event.model_copy(update={"id": "duplicate-event-id"}) for event in events]

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_artifact_changed_after_completion_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    observed = _effect(artifact, relative_path=artifact.name)
    artifact.write_text("later attempt", encoding="utf-8")

    _manifest, fact = await _artifact_fact(
        _command_events(call_id="changed", effects=[observed]),
        task_cwd=tmp_path,
    )

    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_failed_command_cannot_authorize_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("failed", encoding="utf-8")

    manifest, fact = await _artifact_fact(
        _command_events(
            call_id="failed",
            effects=[_effect(artifact, relative_path=artifact.name)],
            failed=True,
        ),
        task_cwd=tmp_path,
    )

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "failure"),
    (
        ("subtype", "error"),
        ("status", "failed"),
        ("runtime_status", "failed"),
        ("runtime_signal", "session_failed"),
        ("runtime_event_type", "tool.failed"),
        ("runtime_status", "cancelled"),
        ("runtime_signal", "tool_timed_out"),
    ),
)
async def test_contradictory_failure_signal_vetoes_success_bits(
    tmp_path: Path,
    field: str,
    failure: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("failed", encoding="utf-8")
    events = _command_events(
        call_id="contradiction", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    events[1] = events[1].model_copy(update={"data": {**events[1].data, field: failure}})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "field", "failure"),
    (
        ("tool_result_meta", "status", "failed"),
        ("tool_result_meta", "success", False),
        ("top_level", "cancelled", True),
        ("top_level", "timed_out", True),
        ("top_level", "aborted", True),
        ("tool_result_meta", "success", "false"),
        ("top_level", "cancelled", "yes"),
    ),
)
async def test_nested_and_boolean_failure_verdicts_veto_journal_authority(
    tmp_path: Path,
    location: str,
    field: str,
    failure: object,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("failed", encoding="utf-8")
    events = _command_events(
        call_id="nested-contradiction",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    completion_data = dict(events[1].data)
    if location == "top_level":
        completion_data[field] = failure
    else:
        tool_result = dict(completion_data["tool_result"])
        tool_result["meta"] = {**tool_result["meta"], field: failure}
        completion_data["tool_result"] = tool_result
    events[1] = events[1].model_copy(update={"data": completion_data})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_pre_start_completion_poisons_later_valid_completion(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="out-of-order", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    stale = events[1].model_copy(
        update={
            "id": "stale-completion",
            "timestamp": events[0].timestamp - timedelta(microseconds=1),
        }
    )

    manifest, fact = await _artifact_fact([stale, *events], task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_foreign_event_reusing_pair_id_poisons_command_authority(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="target", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    duplicate_id = events[1].model_copy(
        update={
            "timestamp": events[1].timestamp + timedelta(microseconds=1),
            "data": {**events[1].data, "tool_call_id": "foreign"},
        }
    )

    manifest, fact = await _artifact_fact([*events, duplicate_id], task_cwd=tmp_path)

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_two_accepted_commands_for_same_artifact_are_ambiguous(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    observed = _effect(artifact, relative_path=artifact.name)
    events = [
        *_command_events(call_id="first", effects=[observed]),
        *_command_events(
            call_id="second", effects=[observed], when=datetime.now(UTC) + timedelta(seconds=1)
        ),
    ]

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert len(manifest.entries) == 2
    assert fact.evidence_handle == "ambiguous:files_touched:0"
