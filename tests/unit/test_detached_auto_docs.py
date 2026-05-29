"""Tests for detached auto user-facing documentation."""

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


def test_cli_docs_describe_detached_auto_as_tracked_non_terminal_background_work() -> None:
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    compact = " ".join(docs.split())

    assert "Detached `auto` wait and retrieve" in docs
    assert "Detached `auto` work is non-terminal tracked background work" in compact
    assert "Starting it does not mean the workflow has completed" in compact
    assert "returned `job_id` is a handle for a tracked job" in compact
    assert "reaches a terminal lifecycle status" in compact

    for cli_command in (
        "ouroboros job status JOB_ID",
        "ouroboros job wait JOB_ID",
        "ouroboros job result JOB_ID",
    ):
        assert cli_command in docs

    for mcp_tool in (
        'ouroboros_job_status(job_id="JOB_ID")',
        'ouroboros_job_wait(job_id="JOB_ID")',
        'ouroboros_job_result(job_id="JOB_ID")',
    ):
        assert mcp_tool in docs

    assert "non-zero status" in compact
    assert "error response" in compact


def test_mcp_docs_describe_detached_auto_as_tracked_non_terminal_background_work() -> None:
    """Runnable artifact check for MCP detached auto wait/retrieve docs."""
    docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    compact = " ".join(docs.split())

    assert "Detached `auto` Jobs" in docs
    assert (
        "`ouroboros_start_auto` starts detached `auto` work as non-terminal tracked background work"
        in compact
    )
    assert (
        "that handle identifies tracked background work, not a completed workflow result" in compact
    )
    assert "terminal state such as `completed`, `failed`, or `cancelled`" in compact
    assert "Expired retention is reported by `ouroboros_job_result`" in compact

    for mcp_tool in (
        'ouroboros_job_status(job_id="JOB_ID")',
        'ouroboros_job_wait(job_id="JOB_ID")',
        'ouroboros_job_result(job_id="JOB_ID")',
    ):
        assert mcp_tool in docs

    assert "Treat `running` or other non-terminal status output as progress only" in compact
    assert "Wait with `ouroboros_job_wait`, then fetch the completed result" in compact
    assert "MCP error response" in compact


def test_docs_verify_running_state_includes_stable_wait_and_retrieve_guidance() -> None:
    """Docs artifact check for detached running-state wait/retrieve guidance."""
    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")

    cli_compact = " ".join(cli_docs.split())
    mcp_compact = " ".join(mcp_docs.split())

    assert "its `running` lifecycle status is non-terminal tracked background work" in cli_compact
    assert "Treat status output as progress, not as the final `auto` result" in cli_compact
    assert (
        "Retrieve the result only after the job reaches a terminal lifecycle status" in cli_compact
    )

    assert "`running` lifecycle status is non-terminal tracked background work" in mcp_compact
    assert "Treat `running` or other non-terminal status output as progress only" in mcp_compact
    assert "Wait with `ouroboros_job_wait`, then fetch the completed result" in mcp_compact

    for cli_command in (
        "ouroboros job status JOB_ID",
        "ouroboros job wait JOB_ID",
        "ouroboros job result JOB_ID",
    ):
        assert cli_command in cli_docs

    for mcp_tool in (
        'ouroboros_job_status(job_id="JOB_ID")',
        'ouroboros_job_wait(job_id="JOB_ID")',
        'ouroboros_job_result(job_id="JOB_ID")',
    ):
        assert mcp_tool in mcp_docs


def test_cli_docs_verify_completed_state_has_stable_result_retrieval_semantics() -> None:
    """Docs artifact check for completed detached auto CLI result retrieval."""
    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    compact = " ".join(docs.split())

    assert "Detached `auto` wait and retrieve" in docs
    assert "ouroboros job status JOB_ID" in docs
    assert "ouroboros job wait JOB_ID" in docs
    assert "ouroboros job result JOB_ID" in docs
    assert "When CLI status reports `completed`" in compact
    assert "`ouroboros job result JOB_ID` retrieves the stable completed `auto` result" in compact
    assert "that job handle" in compact


def test_cli_docs_api_check_verifies_completed_detached_auto_result_output(
    monkeypatch, tmp_path
) -> None:
    """Runnable docs/API check for completed detached auto CLI result retrieval."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-docs-check.db'}"
    job_id = "job_auto_docs_done"
    auto_session_id = "auto_docs_done"
    result_text = "detached auto result artifact: seed.yaml"
    # Anchor to wall-clock now so the terminal handle stays inside the 1h
    # retrieval TTL (``_JOB_TTL``); a hardcoded absolute date silently expires
    # once real time advances past it. Mirrors the relative-time pattern used
    # by the expired-state test below.
    timestamp = datetime.now(UTC) - timedelta(minutes=3)

    async def _persist_completed_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_docs_created",
                    type="mcp.job.created",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
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
    assert f"$ ouroboros job result {job_id}" in docs
    assert result_text in docs


def test_mcp_docs_verify_completed_state_has_stable_result_retrieval_semantics() -> None:
    """Docs artifact check for completed detached auto MCP result retrieval."""
    docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    compact = " ".join(docs.split())

    assert "Detached `auto` Jobs" in docs
    assert 'ouroboros_job_status(job_id="JOB_ID")' in docs
    assert 'ouroboros_job_wait(job_id="JOB_ID")' in docs
    assert 'ouroboros_job_result(job_id="JOB_ID")' in docs
    assert "When MCP status reports `completed`" in compact
    assert (
        '`ouroboros_job_result(job_id="JOB_ID")` retrieves the stable completed `auto` result'
        in compact
    )
    assert "that job handle" in compact
    assert 'ouroboros_job_result(job_id="job_auto_docs_done")' in docs
    assert 'content[0].text = "detached auto result artifact: seed.yaml"' in docs
    assert 'content[1].uri = "file:///tmp/detached-auto-result.json"' in docs
    assert 'meta.status = "completed"' in docs
    assert "meta.is_terminal = true" in docs


def test_mcp_docs_api_check_verifies_completed_detached_auto_result_response(
    tmp_path,
) -> None:
    """Runnable docs/API check for completed detached auto MCP result retrieval."""
    docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-mcp-docs-check.db'}"
    job_id = "job_auto_docs_done"
    auto_session_id = "auto_docs_done"
    result_text = "detached auto result artifact: seed.yaml"
    artifact_uri = "file:///tmp/detached-auto-result.json"
    # Relative to now: keeps the completed handle inside the 1h retrieval TTL
    # regardless of wall-clock date (see the expired-state test for the
    # complementary past-TTL case).
    timestamp = datetime.now(UTC) - timedelta(minutes=3)

    async def _persist_completed_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_mcp_docs_created",
                    type="mcp.job.created",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
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

    assert f'ouroboros_job_result(job_id="{job_id}")' in docs
    assert f'content[0].text = "{result_text}"' in docs
    assert f'content[1].uri = "{artifact_uri}"' in docs
    assert 'meta.status = "completed"' in docs
    assert "meta.is_terminal = true" in docs


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


def test_docs_verify_failed_state_has_stable_status_semantics_and_next_steps() -> None:
    """Docs artifact check for failed detached auto status/result guidance."""
    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")

    cli_compact = " ".join(cli_docs.split())
    mcp_compact = " ".join(mcp_docs.split())

    assert (
        "When CLI status reports `failed`, the job is terminal and still observable" in cli_compact
    )
    assert (
        "`ouroboros job result JOB_ID` returns the stable failure output or error details"
        in cli_compact
    )
    assert "not a successful `auto` result" in cli_compact
    assert "Next steps are to inspect `ouroboros job status JOB_ID`" in cli_compact
    assert "`ouroboros job result JOB_ID`" in cli_compact
    assert (
        "resume or retry from the surfaced auto session, execution, or lineage handle"
        in cli_compact
    )

    assert (
        "When MCP status reports `failed`, the job is terminal and still observable" in mcp_compact
    )
    assert (
        '`ouroboros_job_result(job_id="JOB_ID")` returns the stable failure output or error details'
        in mcp_compact
    )
    assert "`is_error=true`" in mcp_compact
    assert "not a successful `auto` result" in mcp_compact
    assert 'Next steps are to inspect `ouroboros_job_status(job_id="JOB_ID")`' in mcp_compact
    assert 'ouroboros_job_result(job_id="JOB_ID")' in mcp_compact
    assert (
        "resume or retry from the surfaced auto session, execution, or lineage handle"
        in mcp_compact
    )
    assert 'ouroboros_job_result(job_id="job_auto_docs_failed")' in mcp_docs
    assert "is_error = true" in mcp_docs
    assert 'content[0].text = "detached auto failed: seed repair budget exhausted"' in mcp_docs
    assert 'meta.status = "failed"' in mcp_docs
    assert 'meta.error = "seed repair budget exhausted"' in mcp_docs


def test_mcp_docs_api_check_verifies_failed_detached_auto_result_response(
    tmp_path,
) -> None:
    """Runnable MCP docs/API check for failed detached auto result retrieval."""
    docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-mcp-failed-docs-check.db'}"
    job_id = "job_auto_docs_failed"
    auto_session_id = "auto_docs_failed"
    failure_text = "detached auto failed: seed repair budget exhausted"
    success_text = "detached auto result artifact: seed.yaml"
    source_error = "seed repair budget exhausted"
    # Relative to now so the failed handle stays inside the 1h retrieval TTL.
    timestamp = datetime.now(UTC) - timedelta(minutes=2)

    async def _persist_failed_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_mcp_failed_docs_created",
                    type="mcp.job.created",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
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

    assert f'ouroboros_job_result(job_id="{job_id}")' in docs
    assert f'content[0].text = "{failure_text}"' in docs
    assert "is_error = true" in docs
    assert 'meta.status = "failed"' in docs
    assert "meta.is_terminal = true" in docs
    assert f'meta.error = "{source_error}"' in docs


def test_docs_verify_cancelled_state_has_stable_status_semantics_and_next_steps() -> None:
    """Docs artifact check for cancelled detached auto status/result guidance."""
    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")

    cli_compact = " ".join(cli_docs.split())
    mcp_compact = " ".join(mcp_docs.split())

    assert (
        "When CLI status reports `cancelled`, the job is terminal and still observable"
        in cli_compact
    )
    assert (
        "`ouroboros job result JOB_ID` returns stable cancellation output or error details"
        in cli_compact
    )
    assert "cancellation reason when one is available" in cli_compact
    assert "not a successful `auto` result" in cli_compact
    assert "Next steps are to inspect `ouroboros job status JOB_ID`" in cli_compact
    assert "`ouroboros job result JOB_ID`" in cli_compact
    assert "restart the detached auto flow or resume from the surfaced auto session" in cli_compact
    assert "exits non-zero because the terminal result is an error result" in cli_compact
    assert "ouroboros job result job_auto_docs_cancelled" in cli_docs
    assert "detached auto cancelled: user requested cancellation" in cli_docs

    assert (
        "When MCP status reports `cancelled`, the job is terminal and still observable"
        in mcp_compact
    )
    assert (
        '`ouroboros_job_result(job_id="JOB_ID")` returns stable cancellation '
        "output or error details" in mcp_compact
    )
    assert "`is_error=true`" in mcp_compact
    assert "cancellation reason when one is available" in mcp_compact
    assert "not a successful `auto` result" in mcp_compact
    assert 'Next steps are to inspect `ouroboros_job_status(job_id="JOB_ID")`' in mcp_compact
    assert 'ouroboros_job_result(job_id="JOB_ID")' in mcp_compact
    assert "restart the detached auto flow or resume from the surfaced auto session" in mcp_compact
    assert 'ouroboros_job_result(job_id="job_auto_docs_cancelled")' in mcp_docs
    assert "is_error = true" in mcp_docs
    assert 'content[0].text = "detached auto cancelled: user requested cancellation"' in mcp_docs
    assert 'meta.status = "cancelled"' in mcp_docs
    assert 'meta.lifecycle_status = "cancelled"' in mcp_docs
    assert "meta.is_terminal = true" in mcp_docs
    assert 'meta.error = "user requested cancellation"' in mcp_docs


def test_mcp_docs_api_check_verifies_cancelled_detached_auto_result_response(
    tmp_path,
) -> None:
    """Runnable MCP docs/API check for cancelled detached auto result retrieval."""
    docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-mcp-cancelled-docs-check.db'}"
    job_id = "job_auto_docs_cancelled"
    auto_session_id = "auto_docs_cancelled"
    cancellation_text = "detached auto cancelled: user requested cancellation"
    success_text = "detached auto result artifact: seed.yaml"
    source_error = "user requested cancellation"
    # Relative to now so the cancelled handle stays inside the 1h retrieval TTL.
    timestamp = datetime.now(UTC) - timedelta(minutes=1)

    async def _persist_cancelled_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_mcp_cancelled_docs_created",
                    type="mcp.job.created",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
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

    assert f'ouroboros_job_result(job_id="{job_id}")' in docs
    assert f'content[0].text = "{cancellation_text}"' in docs
    assert "is_error = true" in docs
    assert 'meta.status = "cancelled"' in docs
    assert 'meta.lifecycle_status = "cancelled"' in docs
    assert "meta.is_terminal = true" in docs
    assert f'meta.error = "{source_error}"' in docs


def test_docs_verify_expired_state_has_stable_status_semantics_and_next_steps() -> None:
    """Docs artifact check for expired detached auto status/result guidance."""
    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")

    cli_compact = " ".join(cli_docs.split())
    mcp_compact = " ".join(mcp_docs.split())

    assert "When `ouroboros job result JOB_ID` reports `expired`" in cli_compact
    assert "retained result is no longer available through that job handle" in cli_compact
    assert "`ouroboros job status JOB_ID` still reports the stored terminal" in cli_compact
    assert "result retrieval returns stable expiration error details" in cli_compact
    assert "rather than a detached `auto` result" in cli_compact
    assert "Next steps are to inspect any surfaced auto session" in cli_compact
    assert "resume from that handle or restart the detached auto flow" in cli_compact
    assert "when no recoverable handle is present" in cli_compact

    assert 'When `ouroboros_job_result(job_id="JOB_ID")` reports `expired`' in mcp_compact
    assert "retained result is no longer available through that job handle" in mcp_compact
    assert "`ouroboros_job_status` still reports the stored terminal" in mcp_compact
    assert "result retrieval returns stable expiration error details" in mcp_compact
    assert "rather than a detached `auto` result" in mcp_compact
    assert "Next steps are to inspect any surfaced auto session" in mcp_compact
    assert "resume from that handle or restart the detached auto flow" in mcp_compact
    assert "when no recoverable handle is present" in mcp_compact
    assert "ouroboros job result job_auto_docs_expired" in cli_docs
    assert "Job handle expired: job_auto_docs_expired. Result unavailable." in cli_docs
    assert 'ouroboros_job_result(job_id="job_auto_docs_expired")' in mcp_docs
    assert (
        'error.message = "Job handle expired: job_auto_docs_expired. Result unavailable."'
        in mcp_docs
    )
    assert 'error.error_code = "job_handle_expired"' in mcp_docs
    assert 'error.details.lifecycle_status = "expired"' in mcp_docs
    assert "error.details.is_terminal = true" in mcp_docs
    assert "error.details.result_available = false" in mcp_docs
    assert 'error.details.reason = "expired"' in mcp_docs


def test_docs_api_check_verifies_expired_detached_work_observable_contract(
    monkeypatch, tmp_path
) -> None:
    """Runnable docs/API check for expired detached auto handles."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'expired-detached-auto-docs-check.db'}"
    job_id = "job_auto_docs_expired"
    auto_session_id = "auto_docs_expired"
    expired_at = datetime.now(UTC) - timedelta(hours=2)

    async def _persist_expired_auto_job_and_fetch_result():
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_expired_docs_created",
                    type="mcp.job.created",
                    timestamp=expired_at,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
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

    assert api_result.is_err
    assert api_result.error.tool_name == "ouroboros_job_result"
    assert api_result.error.error_code == "job_handle_expired"
    assert api_result.error.message == (
        "Job handle expired: job_auto_docs_expired. Result unavailable."
    )
    assert api_result.error.details == {
        "job_id": job_id,
        "lifecycle_status": "expired",
        "is_terminal": True,
        "result_available": False,
        "reason": "expired",
    }

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )
    cli_result = runner.invoke(app, ["job", "result", job_id])

    assert cli_result.exit_code == 1
    assert api_result.error.message in cli_result.output
    assert f"$ ouroboros job result {job_id}" in cli_docs
    assert api_result.error.message in cli_docs
    assert f'ouroboros_job_result(job_id="{job_id}")' in mcp_docs
    assert f'error.message = "{api_result.error.message}"' in mcp_docs
    assert f'error.error_code = "{api_result.error.error_code}"' in mcp_docs
    assert 'error.details.lifecycle_status = "expired"' in mcp_docs
    assert "error.details.is_terminal = true" in mcp_docs
    assert "error.details.result_available = false" in mcp_docs
    assert 'error.details.reason = "expired"' in mcp_docs


def test_docs_verify_invalid_detached_work_has_stable_status_semantics_and_next_steps() -> None:
    """Docs artifact check for invalid detached auto handles."""
    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")

    cli_compact = " ".join(cli_docs.split())
    mcp_compact = " ".join(mcp_docs.split())

    assert "Unknown, expired, or otherwise unavailable handles fail" in cli_compact
    assert (
        "When CLI status cannot resolve the supplied handle, treat the detached work "
        "as `invalid` or unavailable rather than as running or completed" in cli_compact
    )
    assert "stable observable status is the non-zero CLI exit" in cli_compact
    assert "human-readable error for that handle" in cli_compact
    assert "Next steps are to check the copied `job_id`" in cli_compact
    assert "inspect any surfaced auto session, execution, or lineage handle" in cli_compact
    assert "restart the detached auto flow when no valid handle can be recovered" in cli_compact

    assert (
        "Unknown, expired, or otherwise unavailable handles return an MCP error response"
        in mcp_compact
    )
    assert (
        "When MCP status cannot resolve the supplied handle, treat the detached work "
        "as `invalid` or unavailable rather than as running or completed" in mcp_compact
    )
    assert "stable observable status is the MCP error response for that handle" in mcp_compact
    assert "not a detached `auto` result" in mcp_compact
    assert "Next steps are to check the copied `job_id`" in mcp_compact
    assert "inspect any surfaced auto session, execution, or lineage handle" in mcp_compact
    assert "restart the detached auto flow when no valid handle can be recovered" in mcp_compact
    assert 'ouroboros_job_result(job_id="missing_detached_auto")' in mcp_docs
    assert (
        'error.message = "Job handle not found: missing_detached_auto. Result unavailable."'
        in mcp_docs
    )
    assert 'error.error_code = "job_handle_not_found"' in mcp_docs
    assert 'error.details.lifecycle_status = "invalid"' in mcp_docs
    assert "error.details.is_terminal = true" in mcp_docs
    assert "error.details.result_available = false" in mcp_docs
    assert 'error.details.reason = "not_found"' in mcp_docs

    assert "$ ouroboros job result missing_detached_auto" in cli_docs
    assert "Job handle not found: missing_detached_auto. Result unavailable." in cli_docs


def test_docs_api_check_verifies_invalid_detached_work_observable_contract(
    monkeypatch, tmp_path
) -> None:
    """Runnable docs/API check for invalid detached auto handles."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    cli_docs = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    mcp_docs = Path("docs/api/mcp.md").read_text(encoding="utf-8")
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
    assert f"$ ouroboros job result {job_id}" in cli_docs
    assert api_result.error.message in cli_docs
    assert f'ouroboros_job_result(job_id="{job_id}")' in mcp_docs
    assert f'error.message = "{api_result.error.message}"' in mcp_docs
    assert f'error.error_code = "{api_result.error.error_code}"' in mcp_docs
    assert 'error.details.lifecycle_status = "invalid"' in mcp_docs
    assert "error.details.is_terminal = true" in mcp_docs
    assert "error.details.result_available = false" in mcp_docs
