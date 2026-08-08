"""Journal-to-verifier coverage for command-scoped Bash artifacts (#1747)."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import inspect
import os
from pathlib import Path
import stat
import subprocess
import textwrap
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.core.filesystem_capability import open_nofollow_directory_chain
from ouroboros.events.base import BaseEvent
import ouroboros.harness.deliver_gate as deliver_gate_module
from ouroboros.harness.deliver_gate import load_ac_evidence_manifest
from ouroboros.harness.journal import EvidenceManifest
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
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


def _workspace_chain_payload(workspace: Path) -> list[dict[str, int | str]]:
    workspace_chain = open_nofollow_directory_chain(workspace)
    try:
        workspace_fingerprint = workspace_chain.fingerprint()
    finally:
        workspace_chain.close()
    return [
        {"name": name, "st_dev": device, "st_ino": inode, "st_mode": mode}
        for name, device, inode, mode in workspace_fingerprint
    ]


def _effect(path: Path, *, relative_path: str) -> dict[str, object]:
    current = path.lstat()
    workspace = path
    for _part in Path(relative_path).parts:
        workspace = workspace.parent
    return {
        "capture": "ouroboros.leaf-dispatch.v1",
        "path": relative_path,
        "workspace_relative_path": relative_path,
        "workspace_chain": _workspace_chain_payload(workspace),
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


def _set_verdict_payload(
    completion_data: dict[str, Any],
    *,
    location: str,
    field: str,
    value: object,
) -> None:
    if location == "top_level":
        completion_data[field] = value
    elif location == "top_level_meta":
        completion_data["meta"] = {field: value}
    else:
        tool_result = dict(completion_data["tool_result"])
        if location == "tool_result":
            tool_result[field] = value
        else:
            tool_result["meta"] = {**tool_result["meta"], field: value}
        completion_data["tool_result"] = tool_result


def _verdict_payload_value(data: dict[str, Any], *, location: str, field: str) -> object:
    if location == "top_level":
        return data[field]
    if location == "top_level_meta":
        return data["meta"][field]
    if location == "tool_result":
        return data["tool_result"][field]
    return data["tool_result"]["meta"][field]


def _set_completion_verdict(
    events: list[BaseEvent],
    *,
    location: str,
    field: str,
    value: object,
) -> None:
    completion_data = dict(events[1].data)
    _set_verdict_payload(
        completion_data,
        location=location,
        field=field,
        value=value,
    )
    events[1] = events[1].model_copy(update={"data": completion_data})


async def _run_production_artifact_route(
    tmp_path: Path,
    *,
    call_id: str,
    completion_data: dict[str, Any],
    before_verify: Callable[[], None] | None = None,
):
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
                    "tool_call_id": call_id,
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
                content="command completion",
                tool_name="Bash",
                data={"subtype": "tool_result", "tool_call_id": call_id, **completion_data},
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
    tool_completion = next(
        event for event in store.events if event.type == "execution.tool.completed"
    )
    if before_verify is not None:
        before_verify()
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )
    return tool_completion, manifest, fact


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
    assert provenance["workspace_chain"][0]["name"] == os.sep
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
async def test_production_route_rejects_intermediate_task_cwd_ancestor_swap(
    tmp_path: Path,
) -> None:
    """The real route rejects ordinary ancestor replacement before Bash start."""
    ancestor = tmp_path / "trusted-ancestor"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    artifact = workspace / "claimed.txt"
    artifact.write_text("before", encoding="utf-8")
    displaced_ancestor = tmp_path / "displaced-ancestor"
    replacement_ancestor = tmp_path / "replacement-ancestor"
    replacement_workspace = replacement_ancestor / workspace.name
    replacement_workspace.mkdir(parents=True)
    replacement_artifact = replacement_workspace / artifact.name
    os.link(artifact, replacement_artifact)
    expected_before_swap = _effect(artifact, relative_path=artifact.name)
    leaf_identity_keys = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    replacement_before_swap = _effect(replacement_artifact, relative_path=artifact.name)
    assert tuple(expected_before_swap[key] for key in leaf_identity_keys) == tuple(
        replacement_before_swap[key] for key in leaf_identity_keys
    )
    swapped = False

    class StubRuntime:
        runtime_backend = "opencode"
        permission_mode = "acceptEdits"
        working_directory = str(workspace)

        async def execute_task(self, **_kwargs: Any):
            nonlocal swapped
            assert expected_before_swap == _effect(artifact, relative_path=artifact.name)
            ancestor.rename(displaced_ancestor)
            replacement_ancestor.rename(ancestor)
            swapped = True
            replacement_after_swap = _effect(
                workspace / artifact.name,
                relative_path=artifact.name,
            )
            assert tuple(expected_before_swap[key] for key in leaf_identity_keys) == tuple(
                replacement_after_swap[key] for key in leaf_identity_keys
            )
            yield AgentMessage(
                type="assistant",
                content="write artifact",
                tool_name="Bash",
                data={
                    "tool_call_id": "production-ancestor-swap",
                    "tool_input": {"command": "printf accepted > claimed.txt"},
                },
            )
            subprocess.run(
                ["/bin/sh", "-c", "printf accepted > claimed.txt"],
                cwd=workspace,
                check=True,
            )
            yield AgentMessage(
                type="assistant",
                content="created claimed.txt",
                tool_name="Bash",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "production-ancestor-swap",
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
        task_cwd=str(workspace),
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
    tool_start = next(event for event in store.events if event.type == "execution.tool.started")
    tool_completion = next(
        event for event in store.events if event.type == "execution.tool.completed"
    )
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=workspace,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert result.success is True
    assert swapped is True
    assert _effect(workspace / artifact.name, relative_path=artifact.name) != expected_before_swap
    assert "filesystem_effects" not in tool_completion.data
    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_production_route_rejects_post_capture_ordinary_ancestor_replacement(
    tmp_path: Path,
) -> None:
    """Durable workspace identity rejects namespace replacement before replay."""
    ancestor = tmp_path / "selected-ancestor"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    artifact = workspace / "claimed.txt"
    artifact.write_text("before", encoding="utf-8")
    displaced_ancestor = tmp_path / "displaced-ancestor"
    replacement_ancestor = tmp_path / "replacement-ancestor"
    replacement_workspace = replacement_ancestor / workspace.name
    replacement_workspace.mkdir(parents=True)
    replacement_artifact = replacement_workspace / artifact.name
    os.link(artifact, replacement_artifact)
    swapped = False

    def replace_before_verify() -> None:
        nonlocal swapped
        ancestor.rename(displaced_ancestor)
        replacement_ancestor.rename(ancestor)
        swapped = True

    tool_completion, manifest, fact = await _run_production_artifact_route(
        workspace,
        call_id="post-capture-ancestor-replacement",
        completion_data={"tool_result": {"is_error": False, "meta": {"exit_status": 0}}},
        before_verify=replace_before_verify,
    )

    assert swapped is True
    assert len(tool_completion.data["filesystem_effects"]) == 1
    assert manifest.entries[0].payload.get("command_artifacts") is not None
    assert artifact.read_text(encoding="utf-8") == "accepted"
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "failure"),
    (
        *(
            (field, True)
            for field in deliver_gate_module.RUNTIME_FAILURE_BOOLEAN_FIELDS
            if field != "is_error"
        ),
        ("failure", "yes"),
        ("is_error_invalid", "yes"),
        ("isError", "yes"),
        ("success", False),
        ("ok", False),
        ("ok", "yes"),
        ("status", "failed"),
        *((field, 9) for field in deliver_gate_module.RUNTIME_TOOL_EXIT_STATUS_FIELDS),
    ),
)
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_production_projection_cannot_launder_command_failure_verdict(
    tmp_path: Path,
    location: str,
    field: str,
    failure: object,
) -> None:
    call_id = f"projection-{location}-{field}"

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
                    "tool_call_id": call_id,
                    "tool_input": {"command": "printf accepted > claimed.txt"},
                },
            )
            subprocess.run(
                ["/bin/sh", "-c", "printf accepted > claimed.txt"],
                cwd=tmp_path,
                check=True,
            )
            completion_data: dict[str, Any] = {
                "subtype": "tool_result",
                "tool_call_id": call_id,
                "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
            }
            _set_verdict_payload(
                completion_data,
                location=location,
                field=field,
                value=failure,
            )
            yield AgentMessage(
                type="assistant",
                content="contradictory completion",
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
    tool_completion = next(
        event for event in store.events if event.type == "execution.tool.completed"
    )
    assert _verdict_payload_value(tool_completion.data, location=location, field=field) == failure

    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion_data", "poison_key"),
    (
        ({"meta": None, "tool_result": {"is_error": False, "meta": {"exit_status": 0}}}, "meta"),
        ({"is_error": False, "tool_result": None}, "tool_result"),
    ),
)
async def test_production_projection_preserves_present_null_container_as_poison(
    tmp_path: Path,
    completion_data: dict[str, Any],
    poison_key: str,
) -> None:
    tool_completion, manifest, fact = await _run_production_artifact_route(
        tmp_path,
        call_id=f"null-{poison_key}",
        completion_data=completion_data,
    )

    assert tool_completion.data[poison_key] is True
    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "neutral"),
    (
        ("ok", True),
        ("is_error_invalid", False),
        ("isError", False),
        ("exit", 0),
        ("errorCode", 0),
        ("error_code", 0),
    ),
)
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_production_projection_preserves_neutral_runtime_verdict_alias(
    tmp_path: Path,
    location: str,
    field: str,
    neutral: object,
) -> None:
    completion_data: dict[str, Any] = {
        "tool_result": {"is_error": False, "meta": {"exit_status": 0}}
    }
    _set_verdict_payload(
        completion_data,
        location=location,
        field=field,
        value=neutral,
    )

    tool_completion, manifest, fact = await _run_production_artifact_route(
        tmp_path,
        call_id=f"neutral-{location}-{field}",
        completion_data=completion_data,
    )

    assert _verdict_payload_value(tool_completion.data, location=location, field=field) == neutral
    assert len(manifest.entries) == 1
    assert "command_artifacts" in manifest.entries[0].payload
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
async def test_orphan_idless_terminal_blocks_late_named_capture_end_to_end(
    tmp_path: Path,
) -> None:
    """A terminal without an id closes the named command before a late mutation."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("before", encoding="utf-8")

    class StubRuntime:
        runtime_backend = "opencode"
        permission_mode = "acceptEdits"
        working_directory = str(tmp_path)

        async def execute_task(self, **_kwargs: Any):
            yield AgentMessage(
                type="assistant",
                content="start named command",
                tool_name="Bash",
                data={
                    "tool_call_id": "named-command",
                    "tool_input": {"command": "printf accepted > claimed.txt"},
                },
            )
            yield AgentMessage(
                type="tool_result",
                content="unassigned Bash completion",
                tool_name="Bash",
                data={
                    "subtype": "tool_result",
                    "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
                },
            )
            artifact.write_text("late mutation", encoding="utf-8")
            yield AgentMessage(
                type="tool_result",
                content="late matching completion",
                tool_name="Bash",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "named-command",
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
    completions = [event for event in store.events if event.type == "execution.tool.completed"]
    manifest, fact = await _artifact_fact(
        store.events,
        task_cwd=tmp_path,
        ac_id=str(tool_start.data["ac_id"]),
        session_attempt_id=str(tool_start.data["session_attempt_id"]),
    )

    assert len(completions) == 2
    assert "tool_call_id" not in completions[0].data
    assert completions[0].data["tool_name"] == "Bash"
    assert completions[1].data["tool_call_id"] == "named-command"
    assert "filesystem_effects" not in completions[1].data
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
        lambda effect: {key: value for key, value in effect.items() if key != "workspace_chain"},
        lambda effect: {**effect, "workspace_chain": []},
        lambda effect: {
            **effect,
            "workspace_chain": [
                effect["workspace_chain"][0],
                *effect["workspace_chain"],
            ],
        },
        lambda effect: {
            **effect,
            "workspace_chain": [
                {**effect["workspace_chain"][0], "st_ino": 0},
                *effect["workspace_chain"][1:],
            ],
        },
        lambda effect: {
            **effect,
            "workspace_chain": [
                {**effect["workspace_chain"][0], "unknown": "forbidden"},
                *effect["workspace_chain"][1:],
            ],
        },
        lambda effect: {
            **effect,
            "workspace_chain": [
                *effect["workspace_chain"][:-1],
                {**effect["workspace_chain"][-1], "name": "x" * 4096},
            ],
        },
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
async def test_crafted_workspace_chain_invalidates_durable_authority(tmp_path: Path) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    manifest, _fact = await _artifact_fact(
        _command_events(
            call_id="crafted-workspace",
            effects=[_effect(artifact, relative_path=artifact.name)],
        ),
        task_cwd=tmp_path,
    )
    entry = manifest.entries[0]
    payload = dict(entry.payload)
    provenance = dict(payload["command_artifacts"])
    workspace_chain = [dict(component) for component in provenance["workspace_chain"]]
    workspace_chain[-1]["st_ino"] += 1
    provenance["workspace_chain"] = workspace_chain
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
async def test_mixed_workspace_chains_invalidate_whole_command(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    first_effect = _effect(first, relative_path=first.name)
    second_effect = _effect(second, relative_path=second.name)
    second_chain = [dict(component) for component in second_effect["workspace_chain"]]
    second_chain[-1]["st_ino"] += 1
    second_effect["workspace_chain"] = second_chain

    manifest, fact = await _artifact_fact(
        _command_events(
            call_id="mixed-workspace",
            effects=[first_effect, second_effect],
        ),
        task_cwd=tmp_path,
        claim=first.name,
    )

    assert "command_artifacts" not in manifest.entries[0].payload
    assert fact.evidence_handle == "missing:files_touched:0"


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

    effect = _effect(outside, relative_path="escape.txt")
    effect["workspace_chain"] = _workspace_chain_payload(tmp_path)
    _manifest, fact = await _artifact_fact(
        _command_events(
            call_id="symlink",
            effects=[effect],
        ),
        task_cwd=tmp_path,
        claim="escape.txt",
    )

    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_parent_swap_after_workspace_component_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver_parent = tmp_path / "sub"
    receiver_parent.mkdir()
    artifact = receiver_parent / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    displaced_parent = tmp_path / "original-sub"
    outside_parent = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_parent.mkdir()
    os.link(artifact, outside_parent / artifact.name)
    observed = _effect(artifact, relative_path="sub/claimed.txt")
    assert observed == _effect(artifact, relative_path="sub/claimed.txt")
    original_open = deliver_gate_module.os.open
    swapped = False

    def adversarial_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and dir_fd is not None and os.fspath(path) == tmp_path.name:
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
@pytest.mark.parametrize("swap_on_open", (1, 2), ids=("lease", "rewalk"))
async def test_parent_swap_after_child_dirfd_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_on_open: int,
) -> None:
    receiver_parent = tmp_path / "sub"
    receiver_parent.mkdir()
    artifact = receiver_parent / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    displaced_parent = tmp_path / "original-sub"
    outside_parent = tmp_path.parent / f"{tmp_path.name}-outside-after-open"
    outside_parent.mkdir()
    os.link(artifact, outside_parent / artifact.name)
    observed = _effect(artifact, relative_path="sub/claimed.txt")
    assert observed == _effect(artifact, relative_path="sub/claimed.txt")
    original_open = deliver_gate_module.os.open
    swapped = False
    matching_open_count = 0

    def adversarial_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal matching_open_count, swapped
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and os.fspath(path) == "sub":
            matching_open_count += 1
            if not swapped and matching_open_count == swap_on_open:
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
        _command_events(call_id=f"parent-swap-after-child-open-{swap_on_open}", effects=[observed]),
        task_cwd=tmp_path,
        claim="sub/claimed.txt",
    )

    assert swapped is True
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("swap_on_open", (1, 2), ids=("lease", "rewalk"))
async def test_intermediate_workspace_ancestor_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_on_open: int,
) -> None:
    ancestor = tmp_path / "trusted-ancestor"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    artifact = workspace / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    displaced_ancestor = tmp_path / "original-ancestor"
    outside_ancestor = tmp_path / "outside-ancestor"
    outside_workspace = outside_ancestor / workspace.name
    outside_workspace.mkdir(parents=True)
    os.link(artifact, outside_workspace / artifact.name)
    observed = _effect(artifact, relative_path=artifact.name)
    assert observed == _effect(artifact, relative_path=artifact.name)
    original_open = deliver_gate_module.os.open
    swapped = False
    matching_open_count = 0

    def adversarial_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal matching_open_count, swapped
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and os.fspath(path) == ancestor.name:
            matching_open_count += 1
            if not swapped and matching_open_count == swap_on_open:
                assert observed == _effect(artifact, relative_path=artifact.name)
                ancestor.rename(displaced_ancestor)
                ancestor.symlink_to(outside_ancestor, target_is_directory=True)
                swapped = True
        return fd

    monkeypatch.setattr(deliver_gate_module.os, "open", adversarial_open)
    monkeypatch.setattr(
        deliver_gate_module.os,
        "supports_dir_fd",
        {*deliver_gate_module.os.supports_dir_fd, adversarial_open},
    )

    _manifest, fact = await _artifact_fact(
        _command_events(call_id=f"intermediate-ancestor-swap-{swap_on_open}", effects=[observed]),
        task_cwd=workspace,
    )

    assert swapped is True
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("swap_on_open", (1, 2), ids=("lease", "rewalk"))
async def test_workspace_swap_after_root_dirfd_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_on_open: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    displaced_workspace = tmp_path / "original-workspace"
    outside_workspace = tmp_path / "outside-workspace"
    outside_workspace.mkdir()
    os.link(artifact, outside_workspace / artifact.name)
    observed = _effect(artifact, relative_path=artifact.name)
    assert observed == _effect(artifact, relative_path=artifact.name)
    original_open = deliver_gate_module.os.open
    swapped = False
    matching_open_count = 0

    def adversarial_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal matching_open_count, swapped
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and os.fspath(path) == workspace.name:
            matching_open_count += 1
            if not swapped and matching_open_count == swap_on_open:
                assert observed == _effect(artifact, relative_path=artifact.name)
                workspace.rename(displaced_workspace)
                workspace.symlink_to(outside_workspace, target_is_directory=True)
                swapped = True
        return fd

    monkeypatch.setattr(deliver_gate_module.os, "open", adversarial_open)
    monkeypatch.setattr(
        deliver_gate_module.os,
        "supports_dir_fd",
        {*deliver_gate_module.os.supports_dir_fd, adversarial_open},
    )

    _manifest, fact = await _artifact_fact(
        _command_events(
            call_id=f"workspace-swap-after-root-open-{swap_on_open}", effects=[observed]
        ),
        task_cwd=workspace,
    )

    assert swapped is True
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_static_intermediate_task_cwd_symlink_is_rejected(tmp_path: Path) -> None:
    outside_ancestor = tmp_path / "outside-ancestor"
    outside_workspace = outside_ancestor / "workspace"
    outside_workspace.mkdir(parents=True)
    artifact = outside_workspace / "claimed.txt"
    artifact.write_text("outside", encoding="utf-8")
    ancestor = tmp_path / "linked-ancestor"
    ancestor.symlink_to(outside_ancestor, target_is_directory=True)
    linked_workspace = ancestor / outside_workspace.name

    _manifest, fact = await _artifact_fact(
        _command_events(
            call_id="static-intermediate-symlink",
            effects=[_effect(artifact, relative_path=artifact.name)],
        ),
        task_cwd=linked_workspace,
    )

    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("swap_target", ("parent", "workspace_root"))
async def test_namespace_swap_after_current_leaf_fstat_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_target: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if swap_target == "parent":
        namespace_path = workspace / "sub"
        namespace_path.mkdir()
        relative_path = "sub/claimed.txt"
    else:
        namespace_path = workspace
        relative_path = "claimed.txt"
    artifact = workspace / relative_path
    artifact.write_text("captured", encoding="utf-8")
    displaced_namespace = tmp_path / f"displaced-{swap_target}"
    outside_namespace = tmp_path / f"outside-{swap_target}"
    outside_namespace.mkdir()
    os.link(artifact, outside_namespace / artifact.name)
    observed = _effect(artifact, relative_path=relative_path)
    assert observed == _effect(artifact, relative_path=relative_path)
    original_fstat = deliver_gate_module.os.fstat
    regular_fstat_count = 0
    swapped = False

    def adversarial_fstat(fd: int):
        nonlocal regular_fstat_count, swapped
        current = original_fstat(fd)
        if stat.S_ISREG(current.st_mode):
            regular_fstat_count += 1
            if not swapped and regular_fstat_count == 2:
                namespace_path.rename(displaced_namespace)
                namespace_path.symlink_to(outside_namespace, target_is_directory=True)
                swapped = True
        return current

    monkeypatch.setattr(deliver_gate_module.os, "fstat", adversarial_fstat)

    _manifest, fact = await _artifact_fact(
        _command_events(
            call_id=f"namespace-swap-after-current-leaf-fstat-{swap_target}",
            effects=[observed],
        ),
        task_cwd=workspace,
        claim=relative_path,
    )

    assert regular_fstat_count >= 2
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
@pytest.mark.parametrize("orphan_tool_name", ("Bash", " Bash ", None, "", 7))
async def test_crafted_orphan_idless_terminal_vetoes_named_journal_pair(
    tmp_path: Path,
    orphan_tool_name: object,
) -> None:
    """Durable events cannot revive a named Bash call after an orphan terminal."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="named", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    orphan_data = {
        key: value
        for key, value in events[1].data.items()
        if key not in {"tool_call_id", "filesystem_effects", "tool_name"}
    }
    if orphan_tool_name is not None:
        orphan_data["tool_name"] = orphan_tool_name
    orphan = events[1].model_copy(
        update={
            "id": "orphan-idless-completion",
            "timestamp": events[0].timestamp + timedelta(microseconds=1),
            "data": orphan_data,
        }
    )
    events[1] = events[1].model_copy(
        update={"timestamp": events[0].timestamp + timedelta(microseconds=2)}
    )

    manifest, fact = await _artifact_fact([events[0], orphan, events[1]], task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
async def test_crafted_idless_other_tool_terminal_preserves_named_journal_pair(
    tmp_path: Path,
) -> None:
    """An explicit other-tool completion is not a Bash authority boundary."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    events = _command_events(
        call_id="named", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    orphan = events[1].model_copy(
        update={
            "id": "unrelated-edit-completion",
            "timestamp": events[0].timestamp + timedelta(microseconds=1),
            "data": {
                key: ("Edit" if key == "tool_name" else value)
                for key, value in events[1].data.items()
                if key not in {"tool_call_id", "filesystem_effects"}
            },
        }
    )
    events[1] = events[1].model_copy(
        update={"timestamp": events[0].timestamp + timedelta(microseconds=2)}
    )

    manifest, fact = await _artifact_fact([events[0], orphan, events[1]], task_cwd=tmp_path)

    assert manifest.entries[0].payload.get("command_artifacts") is not None
    assert fact.evidence_handle == manifest.entries[0].handle


@pytest.mark.asyncio
@pytest.mark.parametrize("idless_first", (False, True))
async def test_owned_idless_bash_pair_preserves_named_journal_authority(
    tmp_path: Path,
    idless_first: bool,
) -> None:
    """A unique id-less Bash pair remains independent of a concurrent named pair."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    named_start, named_completion = _command_events(
        call_id="named", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    identity = {
        key: named_start.data[key]
        for key in ("ac_id", "execution_id", "retry_attempt", "session_attempt_id")
    }
    idless_start = BaseEvent(
        id="idless-bash-start",
        type="execution.tool.started",
        timestamp=named_start.timestamp,
        aggregate_type="execution",
        aggregate_id="ac_1",
        data={
            **identity,
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
        },
    )
    idless_completion = BaseEvent(
        id="idless-bash-completion",
        type="execution.tool.completed",
        timestamp=named_start.timestamp,
        aggregate_type="execution",
        aggregate_id="ac_1",
        data={
            **identity,
            "tool_name": "Bash",
            "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
        },
    )
    ordered = (
        [idless_start, named_start, idless_completion, named_completion]
        if idless_first
        else [named_start, idless_start, idless_completion, named_completion]
    )
    events = [
        event.model_copy(
            update={"timestamp": named_start.timestamp + timedelta(microseconds=index)}
        )
        for index, event in enumerate(ordered)
    ]

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    artifact_entries = [
        entry for entry in manifest.entries if entry.payload.get("command_artifacts") is not None
    ]
    assert len(artifact_entries) == 1
    assert fact.evidence_handle == artifact_entries[0].handle


@pytest.mark.asyncio
async def test_owned_idless_edit_nameless_terminal_preserves_named_journal_authority(
    tmp_path: Path,
) -> None:
    """A nameless terminal owned by one id-less Edit is not an orphan Bash terminal."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    named_start, named_completion = _command_events(
        call_id="named", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    identity = {
        key: named_start.data[key]
        for key in ("ac_id", "execution_id", "retry_attempt", "session_attempt_id")
    }
    edit_start = BaseEvent(
        id="idless-edit-start",
        type="execution.tool.started",
        timestamp=named_start.timestamp + timedelta(microseconds=1),
        aggregate_type="execution",
        aggregate_id="ac_1",
        data={
            **identity,
            "tool_name": "Edit",
            "tool_input": {"file_path": "other.txt"},
        },
    )
    nameless_completion = BaseEvent(
        id="idless-edit-completion",
        type="execution.tool.completed",
        timestamp=named_start.timestamp + timedelta(microseconds=2),
        aggregate_type="execution",
        aggregate_id="ac_1",
        data={
            **identity,
            "tool_result": {"is_error": False},
        },
    )
    named_completion = named_completion.model_copy(
        update={"timestamp": named_start.timestamp + timedelta(microseconds=3)}
    )

    manifest, fact = await _artifact_fact(
        [named_start, edit_start, nameless_completion, named_completion],
        task_cwd=tmp_path,
    )

    artifact_entries = [
        entry for entry in manifest.entries if entry.payload.get("command_artifacts") is not None
    ]
    assert len(artifact_entries) == 1
    assert fact.evidence_handle == artifact_entries[0].handle


@pytest.mark.asyncio
async def test_duplicate_idless_starts_poison_named_journal_authority(tmp_path: Path) -> None:
    """Two active id-less starts cannot donate their ambiguous terminal to a named peer."""
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("captured", encoding="utf-8")
    named_start, named_completion = _command_events(
        call_id="named", effects=[_effect(artifact, relative_path=artifact.name)]
    )
    identity = {
        key: named_start.data[key]
        for key in ("ac_id", "execution_id", "retry_attempt", "session_attempt_id")
    }
    idless_events = [
        BaseEvent(
            id=f"ambiguous-idless-start-{index}",
            type="execution.tool.started",
            timestamp=named_start.timestamp + timedelta(microseconds=index),
            aggregate_type="execution",
            aggregate_id="ac_1",
            data={
                **identity,
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
            },
        )
        for index in (1, 2)
    ]
    idless_completion = BaseEvent(
        id="ambiguous-idless-completion",
        type="execution.tool.completed",
        timestamp=named_start.timestamp + timedelta(microseconds=3),
        aggregate_type="execution",
        aggregate_id="ac_1",
        data={
            **identity,
            "tool_name": "Bash",
            "tool_result": {"is_error": False, "meta": {"exit_status": 0}},
        },
    )
    named_completion = named_completion.model_copy(
        update={"timestamp": named_start.timestamp + timedelta(microseconds=4)}
    )

    manifest, fact = await _artifact_fact(
        [named_start, *idless_events, idless_completion, named_completion],
        task_cwd=tmp_path,
    )

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
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
@pytest.mark.parametrize("field", ("failure", "cancel", "abort", "isError", "is_error_invalid"))
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_boolean_failure_aliases_veto_journal_authority(
    tmp_path: Path,
    location: str,
    field: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("failed", encoding="utf-8")
    events = _command_events(
        call_id=f"{location}-{field}-true",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    _set_completion_verdict(events, location=location, field=field, value=True)

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("failure", "cancel", "abort", "isError", "is_error_invalid"))
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_false_boolean_failure_aliases_are_neutral(
    tmp_path: Path,
    location: str,
    field: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    events = _command_events(
        call_id=f"{location}-{field}-false",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    _set_completion_verdict(events, location=location, field=field, value=False)

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert len(manifest.entries) == 1
    assert "command_artifacts" in manifest.entries[0].payload
    assert fact.evidence_handle == manifest.entries[0].handle


@pytest.mark.asyncio
@pytest.mark.parametrize("field", deliver_gate_module.RUNTIME_SUCCESS_BOOLEAN_FIELDS)
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_true_success_aliases_are_neutral(
    tmp_path: Path,
    location: str,
    field: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    events = _command_events(
        call_id=f"{location}-{field}-true",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    _set_completion_verdict(events, location=location, field=field, value=True)

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert len(manifest.entries) == 1
    assert "command_artifacts" in manifest.entries[0].payload
    assert fact.evidence_handle == manifest.entries[0].handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    deliver_gate_module.RUNTIME_TOOL_EXIT_STATUS_FIELDS,
)
@pytest.mark.parametrize("failure", (True, "1", 9))
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_exit_status_aliases_veto_journal_authority(
    tmp_path: Path,
    location: str,
    failure: object,
    field: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("failed", encoding="utf-8")
    events = _command_events(
        call_id=f"{location}-{field}-failed",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    _set_completion_verdict(events, location=location, field=field, value=failure)

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert all("command_artifacts" not in entry.payload for entry in manifest.entries)
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    deliver_gate_module.RUNTIME_TOOL_EXIT_STATUS_FIELDS,
)
@pytest.mark.parametrize(
    "location", ("top_level", "top_level_meta", "tool_result", "tool_result_meta")
)
async def test_zero_exit_status_aliases_are_neutral(
    tmp_path: Path,
    location: str,
    field: str,
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    events = _command_events(
        call_id=f"{location}-{field}-zero",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    _set_completion_verdict(events, location=location, field=field, value=0)

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert len(manifest.entries) == 1
    assert "command_artifacts" in manifest.entries[0].payload
    assert fact.evidence_handle == manifest.entries[0].handle


def test_exit_status_alias_vocabulary_matches_runtime_normalizer() -> None:
    aliases = deliver_gate_module.RUNTIME_TOOL_EXIT_STATUS_FIELDS
    normalizer = ast.parse(
        textwrap.dedent(inspect.getsource(CodexCliRuntime._resolve_item_completion_is_error))
    )
    declared_aliases: set[str] = set()
    for node in ast.walk(normalizer):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in {"exit_key", "fail_key"}:
            continue
        if not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        declared_aliases.update(
            item.value
            for item in node.iter.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )

    assert declared_aliases
    assert declared_aliases <= set(aliases)
    assert {"exit", "errorCode", "error_code"} <= declared_aliases
    assert len(aliases) == len(set(aliases))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "failure"),
    (
        ("top_level", {"status": "mystery"}),
        ("top_level", {"status": "running"}),
        ("top_level", {"runtime_signal": "tool_mystery"}),
        ("tool_result", {"status": "mystery"}),
        ("tool_result_meta", {"status": "mystery"}),
        ("top_level_meta", {"runtime_status": "pending"}),
        (
            "tool_result_meta",
            {"status": "completed", "runtime_signal": "tool_mystery"},
        ),
    ),
)
async def test_unknown_or_in_progress_status_vetoes_journal_authority(
    tmp_path: Path,
    location: str,
    failure: dict[str, str],
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("failed", encoding="utf-8")
    events = _command_events(
        call_id="unknown-status",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    completion_data = dict(events[1].data)
    if location == "top_level":
        completion_data.update(failure)
    elif location == "top_level_meta":
        completion_data["meta"] = failure
    else:
        tool_result = dict(completion_data["tool_result"])
        if location == "tool_result":
            tool_result.update(failure)
        else:
            tool_result["meta"] = {**tool_result["meta"], **failure}
        completion_data["tool_result"] = tool_result
    events[1] = events[1].model_copy(update={"data": completion_data})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries == ()
    assert fact.evidence_handle == "missing:files_touched:0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "success_status",
    (
        {"status": "completed"},
        {"status": "success"},
        {"status": "succeeded"},
        {"runtime_event_type": "tool.completed"},
        {"runtime_event_type": "tool.output"},
        {"runtime_event_type": "tool.result"},
        {"runtime_event_type": "run.completed"},
        {"runtime_signal": "tool_completed", "runtime_status": "running"},
    ),
)
async def test_closed_terminal_success_status_preserves_journal_authority(
    tmp_path: Path,
    success_status: dict[str, str],
) -> None:
    artifact = tmp_path / "claimed.txt"
    artifact.write_text("accepted", encoding="utf-8")
    events = _command_events(
        call_id="closed-success",
        effects=[_effect(artifact, relative_path=artifact.name)],
    )
    events[1] = events[1].model_copy(update={"data": {**events[1].data, **success_status}})

    manifest, fact = await _artifact_fact(events, task_cwd=tmp_path)

    assert manifest.entries[0].payload.get("command_artifacts") is not None
    assert fact.evidence_handle == manifest.entries[0].handle


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
