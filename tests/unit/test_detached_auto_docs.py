"""Tests for detached auto user-facing documentation and result retrieval.

Docs tests parse bounded lifecycle examples and durable semantic anchors, so
rewording surrounding prose never breaks them while state guidance cannot drift.
Behavior tests exercise the real JobResultHandler/CLI contract per terminal
state.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools.job_handlers import JobResultHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

runner = CliRunner()

CLI_JOB_COMMANDS = (
    "ouroboros job status JOB_ID",
    "ouroboros job wait JOB_ID",
    "ouroboros job result JOB_ID",
)
MCP_JOB_TOOLS = (
    'ouroboros_job_status(job_id="JOB_ID")',
    'ouroboros_job_wait(job_id="JOB_ID")',
    'ouroboros_job_result(job_id="JOB_ID")',
)


def _status_guidance(section: str, status: str, next_status: str | None = None) -> str:
    anchor = f"status reports `{status}`"
    start = section.index(anchor)
    if next_status is None:
        return section[start:]
    end = section.index(f"status reports `{next_status}`", start + len(anchor))
    return section[start:end]


def test_cli_docs_cover_detached_auto_wait_and_retrieve() -> None:
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    section = docs.split("### Detached `auto` wait and retrieve", 1)[1].split("## ", 1)[0]
    compact = " ".join(section.split())

    assert "Detached `auto` wait and retrieve" in docs
    for cli_command in CLI_JOB_COMMANDS:
        assert cli_command in docs

    # Short, durable phrase anchors for the lifecycle semantics an operator
    # must not lose: a running job is non-terminal, failed/cancelled are
    # terminal-but-observable, every terminal-failure path names its "Next
    # steps", and an expired in-memory handle still resolves. These survive
    # rewording of the surrounding prose but still fail if the underlying
    # semantic content is deleted.
    assert "running" in compact and "non-terminal" in compact
    for status, next_status in (
        ("completed", "failed"),
        ("failed", "cancelled"),
        ("cancelled", None),
    ):
        state = _status_guidance(compact, status, next_status)
        assert "ouroboros job result JOB_ID" in state
        if status == "completed":
            assert "stable completed `auto` result" in state
        else:
            assert "terminal and still observable" in state
            assert "Next steps" in state
    assert "Job handle not found" in compact and "invalid" in compact
    assert "in-memory handle TTL" in compact
    assert "job_auto_docs_done" in compact and "detached auto result artifact" in compact


def test_mcp_docs_cover_detached_auto_jobs() -> None:
    docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    section = docs.split("### Detached `auto` Jobs", 1)[1].split("### Read-only Project Status", 1)[
        0
    ]
    compact = " ".join(section.split())

    assert "Detached `auto` Jobs" in docs
    for mcp_tool in MCP_JOB_TOOLS:
        assert mcp_tool in docs

    assert "running" in compact and "non-terminal" in compact
    completed = _status_guidance(compact, "completed", "failed")
    assert 'ouroboros_job_result(job_id="JOB_ID")' in completed
    assert "stable completed `auto` result" in completed
    for status, next_status in (("failed", "cancelled"), ("cancelled", None)):
        state = _status_guidance(compact, status, next_status)
        assert 'ouroboros_job_result(job_id="JOB_ID")' in state
        assert "terminal and still observable" in state
        assert "Next steps" in state
        assert "is_error=true" in state
    assert '"job_handle_not_found"' in compact and 'lifecycle_status = "invalid"' in compact
    assert "in-memory handle TTL" in compact
    assert 'meta.status = "completed"' in compact and "meta.is_terminal = true" in compact


def _job_created_event(
    event_id: str, job_id: str, session_id: str, timestamp: datetime
) -> BaseEvent:
    return BaseEvent(
        id=event_id,
        type="mcp.job.created",
        timestamp=timestamp,
        aggregate_type="job",
        aggregate_id=job_id,
        data={
            "job_type": "auto",
            "status": JobStatus.QUEUED.value,
            "message": "Queued detached auto",
            "links": {
                "session_id": session_id,
                "execution_id": None,
                "lineage_id": None,
            },
        },
    )


def test_cli_retrieves_stable_completed_detached_auto_result(monkeypatch, tmp_path) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-docs-check.db'}"
    job_id = "job_auto_docs_done"
    auto_session_id = "auto_docs_done"
    result_text = "detached auto result artifact: seed.yaml"
    # Anchor the persisted job within the 1h terminal-retention TTL relative to
    # "now" rather than a fixed calendar date: a hardcoded timestamp silently
    # crosses the TTL once the wall clock advances past it + ``_JOB_TTL``,
    # turning a fresh completed handle into a spurious "Job handle expired".
    timestamp = datetime.now(UTC) - timedelta(minutes=1)

    async def _persist_completed_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                _job_created_event("evt_auto_docs_created", job_id, auto_session_id, timestamp)
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_docs_completed",
                    type="mcp.job.completed",
                    timestamp=timestamp + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.COMPLETED.value,
                        "message": "Job complete",
                        "result_text": result_text,
                        "result_meta": {"auto_session_id": auto_session_id},
                        "result_payload": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": result_text,
                                    "data": None,
                                    "mime_type": None,
                                    "uri": None,
                                }
                            ],
                            "is_error": False,
                            "meta": {"auto_session_id": auto_session_id},
                            "text_content": result_text,
                        },
                        "is_error": False,
                    },
                )
            )
        finally:
            await store.close()

    asyncio.run(_persist_completed_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    result = runner.invoke(app, ["job", "result", job_id])
    repeated = runner.invoke(app, ["job", "result", job_id])

    assert result.exit_code == 0
    assert repeated.exit_code == 0
    assert result.output == repeated.output == f"{result_text}\n"


def test_mcp_retrieves_stable_completed_detached_auto_result(tmp_path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-mcp-docs-check.db'}"
    job_id = "job_auto_docs_done"
    auto_session_id = "auto_docs_done"
    result_text = "detached auto result artifact: seed.yaml"
    artifact_uri = "file:///tmp/detached-auto-result.json"
    # Relative to "now" so the handle stays inside the 1h retention TTL on any
    # run date (see the completed-CLI variant above).
    timestamp = datetime.now(UTC) - timedelta(minutes=1)

    async def _persist_completed_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                _job_created_event("evt_auto_mcp_docs_created", job_id, auto_session_id, timestamp)
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_mcp_docs_completed",
                    type="mcp.job.completed",
                    timestamp=timestamp + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.COMPLETED.value,
                        "message": "Job complete",
                        "result_text": result_text,
                        "result_meta": {
                            "auto_session_id": auto_session_id,
                            "result": {
                                "artifact": "detached-auto-result.json",
                                "ok": True,
                            },
                        },
                        "result_payload": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": result_text,
                                    "data": None,
                                    "mime_type": None,
                                    "uri": None,
                                },
                                {
                                    "type": "resource",
                                    "text": None,
                                    "data": None,
                                    "mime_type": None,
                                    "uri": artifact_uri,
                                },
                            ],
                            "is_error": False,
                            "meta": {
                                "auto_session_id": auto_session_id,
                                "result": {
                                    "artifact": "detached-auto-result.json",
                                    "ok": True,
                                },
                            },
                            "text_content": result_text,
                        },
                        "is_error": False,
                    },
                )
            )

            handler = JobResultHandler(event_store=store)
            first = await handler.handle({"job_id": job_id})
            second = await handler.handle({"job_id": job_id})
            return first, second
        finally:
            await store.close()

    first, second = asyncio.run(_persist_completed_auto_job_and_fetch_result())

    assert first.is_ok
    assert second.is_ok
    assert first.value.is_error is False
    assert second.value.is_error is False
    assert (
        first.value.content
        == second.value.content
        == (
            MCPContentItem(type=ContentType.TEXT, text=result_text),
            MCPContentItem(type=ContentType.RESOURCE, uri=artifact_uri),
        )
    )
    assert first.value.meta == second.value.meta
    assert first.value.meta["job_id"] == job_id
    assert first.value.meta["status"] == "completed"
    assert first.value.meta["is_terminal"] is True
    assert first.value.meta["session_id"] == auto_session_id
    assert first.value.meta["auto_session_id"] == auto_session_id
    assert first.value.meta["result"] == {
        "artifact": "detached-auto-result.json",
        "ok": True,
    }
    assert first.value.meta["result_payload"]["content"][1]["uri"] == artifact_uri


def test_completed_detached_auto_result_retrieval_leaves_inspectable_event_trace(
    tmp_path,
) -> None:
    """Runnable artifact/log check for completed detached auto retrieval."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-artifact-trace.db'}"
    result_text = "detached auto result artifact: seed.yaml"
    artifact_uri = f"file://{tmp_path / 'seed.yaml'}"

    async def _run_completed_job_and_verify_trace() -> None:
        store = EventStore(db_url)
        manager = JobManager(store)
        try:

            async def _detached_auto_runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=result_text),
                        MCPContentItem(type=ContentType.RESOURCE, uri=artifact_uri),
                    ),
                    meta={
                        "auto_session_id": "auto_artifact_trace",
                        "result": {"artifact": "seed.yaml", "ok": True},
                    },
                )

            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued auto",
                runner=_detached_auto_runner(),
                links=JobLinks(session_id="auto_artifact_trace"),
            )

            deadline = asyncio.get_running_loop().time() + 1
            snapshot = await manager.get_snapshot(started.job_id)
            while snapshot.status is not JobStatus.COMPLETED:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"job {started.job_id} did not complete; last={snapshot.status}"
                    )
                await asyncio.sleep(0.01)
                snapshot = await manager.get_snapshot(started.job_id)

            handler = JobResultHandler(event_store=store, job_manager=manager)
            retrieved = await handler.handle({"job_id": started.job_id})
            assert retrieved.is_ok
            assert retrieved.value.content == (
                MCPContentItem(type=ContentType.TEXT, text=result_text),
                MCPContentItem(type=ContentType.RESOURCE, uri=artifact_uri),
            )
            assert retrieved.value.meta["result"]["artifact"] == "seed.yaml"

            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            completed_events = [event for event in events if event.type == "mcp.job.completed"]
            assert len(completed_events) == 1
            trace = completed_events[0].data
            assert trace["status"] == JobStatus.COMPLETED.value
            assert trace["result_text"] == result_text
            assert trace["result_payload"]["content"][0]["text"] == result_text
            assert trace["result_payload"]["content"][1]["uri"] == artifact_uri
            assert trace["result_meta"]["result"] == {"artifact": "seed.yaml", "ok": True}
        finally:
            await store.close()

    asyncio.run(_run_completed_job_and_verify_trace())


def test_mcp_retrieves_stable_failed_detached_auto_result(tmp_path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-mcp-failed-docs-check.db'}"
    job_id = "job_auto_docs_failed"
    auto_session_id = "auto_docs_failed"
    failure_text = "detached auto failed: seed repair budget exhausted"
    success_text = "detached auto result artifact: seed.yaml"
    source_error = "seed repair budget exhausted"
    # Relative to "now" so the failed handle stays inside the 1h retention TTL
    # on any run date (a fixed date silently expires once now > date + _JOB_TTL).
    timestamp = datetime.now(UTC) - timedelta(minutes=1)

    async def _persist_failed_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                _job_created_event(
                    "evt_auto_mcp_failed_docs_created", job_id, auto_session_id, timestamp
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_mcp_failed_docs_terminal",
                    type="mcp.job.failed",
                    timestamp=timestamp + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.FAILED.value,
                        "message": "Job failed",
                        "error": source_error,
                        "result_text": failure_text,
                        "result_meta": {
                            "auto_session_id": auto_session_id,
                            "failure_kind": "seed_repair_budget",
                        },
                        "result_payload": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": failure_text,
                                    "data": None,
                                    "mime_type": None,
                                    "uri": None,
                                },
                            ],
                            "is_error": True,
                            "meta": {
                                "auto_session_id": auto_session_id,
                                "failure_kind": "seed_repair_budget",
                            },
                            "text_content": failure_text,
                        },
                        "is_error": True,
                    },
                )
            )

            handler = JobResultHandler(event_store=store)
            first = await handler.handle({"job_id": job_id})
            second = await handler.handle({"job_id": job_id})
            return first, second
        finally:
            await store.close()

    first, second = asyncio.run(_persist_failed_auto_job_and_fetch_result())

    assert first.is_ok
    assert second.is_ok
    assert first.value.is_error is True
    assert second.value.is_error is True
    assert (
        first.value.content
        == second.value.content
        == (MCPContentItem(type=ContentType.TEXT, text=failure_text),)
    )
    assert first.value.text_content == failure_text
    assert success_text not in first.value.text_content
    assert first.value.meta == second.value.meta
    assert first.value.meta["job_id"] == job_id
    assert first.value.meta["status"] == "failed"
    assert first.value.meta["lifecycle_status"] == "failed"
    assert first.value.meta["is_terminal"] is True
    assert first.value.meta["result_available"] is True
    assert first.value.meta["error"] == source_error
    assert first.value.meta["session_id"] == auto_session_id
    assert first.value.meta["auto_session_id"] == auto_session_id
    assert first.value.meta["failure_kind"] == "seed_repair_budget"
    assert first.value.meta["result_payload"]["is_error"] is True
    assert first.value.meta["result_payload"]["content"][0]["text"] == failure_text


def test_mcp_retrieves_stable_cancelled_detached_auto_result(tmp_path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-mcp-cancelled-docs-check.db'}"
    job_id = "job_auto_docs_cancelled"
    auto_session_id = "auto_docs_cancelled"
    cancellation_text = "detached auto cancelled: user requested cancellation"
    success_text = "detached auto result artifact: seed.yaml"
    source_error = "user requested cancellation"
    # Relative to "now" so the cancelled handle stays inside the 1h retention
    # TTL on any run date (a fixed date silently expires once now > date + TTL).
    timestamp = datetime.now(UTC) - timedelta(minutes=1)

    async def _persist_cancelled_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                _job_created_event(
                    "evt_auto_mcp_cancelled_docs_created", job_id, auto_session_id, timestamp
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_mcp_cancelled_docs_terminal",
                    type="mcp.job.cancelled",
                    timestamp=timestamp + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.CANCELLED.value,
                        "message": "Job cancelled",
                        "error": source_error,
                        "result_text": cancellation_text,
                        "result_meta": {
                            "auto_session_id": auto_session_id,
                            "cancellation_reason": source_error,
                        },
                        "result_payload": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": cancellation_text,
                                    "data": None,
                                    "mime_type": None,
                                    "uri": None,
                                },
                            ],
                            "is_error": True,
                            "meta": {
                                "auto_session_id": auto_session_id,
                                "cancellation_reason": source_error,
                            },
                            "text_content": cancellation_text,
                        },
                        "is_error": True,
                    },
                )
            )

            handler = JobResultHandler(event_store=store)
            first = await handler.handle({"job_id": job_id})
            second = await handler.handle({"job_id": job_id})
            return first, second
        finally:
            await store.close()

    first, second = asyncio.run(_persist_cancelled_auto_job_and_fetch_result())

    assert first.is_ok
    assert second.is_ok
    assert first.value.is_error is True
    assert second.value.is_error is True
    assert (
        first.value.content
        == second.value.content
        == (MCPContentItem(type=ContentType.TEXT, text=cancellation_text),)
    )
    assert first.value.text_content == cancellation_text
    assert success_text not in first.value.text_content
    assert first.value.meta == second.value.meta
    assert first.value.meta["job_id"] == job_id
    assert first.value.meta["status"] == "cancelled"
    assert first.value.meta["lifecycle_status"] == "cancelled"
    assert first.value.meta["is_terminal"] is True
    assert first.value.meta["result_available"] is True
    assert first.value.meta["error"] == source_error
    assert first.value.meta["session_id"] == auto_session_id
    assert first.value.meta["auto_session_id"] == auto_session_id
    assert first.value.meta["cancellation_reason"] == source_error
    assert first.value.meta["result_payload"]["is_error"] is True
    assert first.value.meta["result_payload"]["content"][0]["text"] == cancellation_text


def test_expired_detached_auto_handle_still_returns_persisted_terminal_result(
    monkeypatch, tmp_path
) -> None:
    """Terminal jobs older than the in-memory handle TTL stay retrievable."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'expired-detached-auto-docs-check.db'}"
    job_id = "job_auto_docs_expired"
    auto_session_id = "auto_docs_expired"
    expired_at = datetime.now(UTC) - timedelta(hours=2)

    async def _persist_expired_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                _job_created_event(
                    "evt_auto_expired_docs_created", job_id, auto_session_id, expired_at
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_expired_docs_completed",
                    type="mcp.job.completed",
                    timestamp=expired_at + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.COMPLETED.value,
                        "message": "Job complete",
                        "result_text": "detached auto result artifact: expired seed.yaml",
                        "result_meta": {"auto_session_id": auto_session_id},
                        "is_error": False,
                    },
                )
            )

            handler = JobResultHandler(event_store=store)
            return await handler.handle({"job_id": job_id})
        finally:
            await store.close()

    api_result = asyncio.run(_persist_expired_auto_job_and_fetch_result())

    assert api_result.is_ok
    assert api_result.value.content[0].text == ("detached auto result artifact: expired seed.yaml")
    assert api_result.value.meta["job_id"] == job_id
    assert api_result.value.meta["lifecycle_status"] == "completed"
    assert api_result.value.meta["is_terminal"] is True
    assert api_result.value.meta["result_available"] is True

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )
    cli_result = runner.invoke(app, ["job", "result", job_id])

    assert cli_result.exit_code == 0
    assert api_result.value.content[0].text in cli_result.output


def test_invalid_detached_auto_handle_fails_with_stable_error_contract(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'invalid-detached-auto-docs-check.db'}"
    job_id = "missing_detached_auto"

    async def _fetch_invalid_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            handler = JobResultHandler(event_store=store)
            return await handler.handle({"job_id": job_id})
        finally:
            await store.close()

    api_result = asyncio.run(_fetch_invalid_result())

    assert api_result.is_err
    assert api_result.error.tool_name == "ouroboros_job_result"
    assert api_result.error.error_code == "job_handle_not_found"
    assert api_result.error.message == (
        "Job handle not found: missing_detached_auto. Result unavailable."
    )
    assert api_result.error.details == {
        "job_id": job_id,
        "lifecycle_status": "invalid",
        "is_terminal": True,
        "result_available": False,
        "reason": "not_found",
        "source_error": f"Job not found: {job_id}",
    }

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )
    cli_result = runner.invoke(app, ["job", "result", job_id])

    assert cli_result.exit_code == 1
    assert api_result.error.message in cli_result.output
