"""CI-safe ACP process → messages → existing durable event store → replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.orchestrator.copilot_acp_runtime import CopilotAcpRuntime
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message
from ouroboros.persistence.event_store import EventStore, sqlite_database_url

_FAKE = Path(__file__).parents[1] / "fixtures" / "fake_copilot_acp.py"


@pytest.mark.integration
async def test_stream_is_persisted_before_completion_and_replay_preserves_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "client-observed"
    wire = tmp_path / "wire.ndjson"
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "normal")
    monkeypatch.setenv("FAKE_COPILOT_ACP_GATE", str(gate))
    monkeypatch.setenv("FAKE_COPILOT_ACP_LOG", str(wire))
    adapter = CopilotAcpRuntime(cli_path=_FAKE, cwd=tmp_path, permission_mode="acceptEdits")
    database = sqlite_database_url(tmp_path / "events.db")
    store = EventStore(database)
    await store.initialize()

    async def append(event) -> bool:
        await store.append(event)
        return True

    emitter = ExecutionEventEmitter(store, safe_emit_event=append)
    try:
        count = 0
        async for message in adapter.execute_task("Run the fixture tests", tools=["Bash"]):
            count += 1
            projected = project_runtime_message(message)
            # Both real executor paths must retain every delta, not just every tenth.
            direct = OrchestratorRunner._should_emit_progress_event(None, message, count)
            parallel = ParallelACExecutor._should_emit_session_progress_event(
                message, projected=projected, messages_processed=count
            )
            assert direct == parallel
            if direct:
                event = emitter.build_session_progress_event(
                    "ouroboros-session", message, projected=projected
                )
                await store.append(event)
            if projected.is_tool_call:
                rows = await store.replay("session", "ouroboros-session")
                assert any(row.data.get("runtime_event_type") == "tool.started" for row in rows)
                wire_rows = [json.loads(line) for line in wire.read_text().splitlines()]
                assert not any(row.get("prompt_completed") for row in wire_rows)
                gate.touch()
    finally:
        await store.close()

    replay = EventStore(database)
    await replay.initialize()
    try:
        events = await replay.replay("session", "ouroboros-session")
    finally:
        await replay.close()
    assert gate.exists()
    assert [e.data["content_delta"] for e in events if "content_delta" in e.data] == [
        "Investigating\n",
        "All ",
        "done.\n",
    ]
    tool_events = [e for e in events if e.data.get("tool_call_id") == "tool-1"]
    assert [e.data["runtime_event_type"] for e in tool_events] == [
        "tool.started",
        "acp.permission_resolved",
        "tool.progress",
        "tool.result",
    ]
    assert tool_events[-1].data["agent_id"] == "root"
    assert tool_events[-1].data["exit_code"] == 0
    assert tool_events[-1].data["acp_session_id"].startswith("session-")
    assert tool_events[-1].data["tool_input"] == {"command": "python3 -m unittest"}
    assert tool_events[-2].data["tool_output"] == "step one\n"
    assert events[-1].data["runtime_status"] == "completed"
    assert not any("PRIVATE_REASONING" in str(event.data) for event in events)
