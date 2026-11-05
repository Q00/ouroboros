"""Background QA owns a durable job rather than the MCP request lifetime."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.tools.job_handlers import JobResultHandler
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.qa_background import StartQAHandler
from ouroboros.mcp.tools.query_handlers import SessionStatusHandler
from ouroboros.persistence.event_store import EventStore


@pytest.mark.asyncio
async def test_start_requires_durable_store_and_rejects_host_and_plugin(tmp_path: Path) -> None:
    memory = EventStore("sqlite+aiosqlite:///:memory:")
    await memory.initialize()
    try:
        h = StartQAHandler(
            QAHandler(event_store=memory),
            memory,
            JobManager(memory),
            project_dir=str(tmp_path),
            reconstructible_backend=True,
        )
        assert (await h.handle({"artifact": "a", "quality_bar": "b"})).is_err
        assert (await h.handle({"artifact": "a"})).is_err
    finally:
        await memory.close()
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    await store.initialize()
    try:
        manager = JobManager(store, durable_jobs=True)
        for runtime, mode in (("host", None), ("opencode", "plugin")):
            h = StartQAHandler(
                QAHandler(event_store=store),
                store,
                manager,
                agent_runtime_backend=runtime,
                opencode_mode=mode,
                project_dir=str(tmp_path),
                reconstructible_backend=True,
            )
            result = await h.handle({"artifact": "a", "quality_bar": "b"})
            assert result.is_err
        assert await store.query_events(aggregate_type="job") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_durable_request_is_accepted_without_waiting_for_qa(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    await store.initialize()
    try:
        manager = JobManager(store, durable_jobs=True)
        inner = MagicMock(spec=QAHandler)
        inner.handle = AsyncMock()
        snapshot = MagicMock(job_id="job_synthetic", cursor=0)
        snapshot.status.value = "queued"
        h = StartQAHandler(
            inner,
            store,
            manager,
            llm_backend="codex",
            agent_runtime_backend="codex",
            project_dir=str(tmp_path),
            reconstructible_backend=True,
        )
        with patch(
            "ouroboros.mcp.tools.background.launch_detached_job",
            new=AsyncMock(return_value=snapshot),
        ) as launch:
            result = await h.handle({"artifact": "synthetic artifact", "quality_bar": "testable"})
        assert result.is_ok
        assert result.value.meta["job_id"] == "job_synthetic"
        assert launch.await_args.kwargs["request"].tool_name == "ouroboros_start_qa"
        inner.handle.assert_not_awaited()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_injected_adapter_is_rejected_before_acceptance(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    await store.initialize()
    injected = MagicMock()
    handler = StartQAHandler(
        QAHandler(llm_adapter=injected, event_store=store),
        store,
        JobManager(store, durable_jobs=True),
        llm_backend="codex",
        agent_runtime_backend="codex",
        project_dir=str(tmp_path),
    )
    try:
        with patch("ouroboros.mcp.tools.background.launch_detached_job", new=AsyncMock()) as launch:
            result = await handler.handle({"artifact": "a", "quality_bar": "pass"})
        assert result.is_err
        assert "reconstructible backend" in result.error.message
        launch.assert_not_awaited()
        assert await store.query_events(aggregate_type="job") == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="fake executable uses a POSIX shebang")
@pytest.mark.parametrize(
    ("valid", "artifact"), [(True, "SYNTHETIC_ARTIFACT"), (False, "SYNTHETIC_ARTIFACT"), (True, "")]
)
async def test_detached_qa_survives_accepting_store_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool, artifact: str
) -> None:
    """Use a real detached worker and a local fake CLI; no provider or user data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("OUROBOROS_DASHBOARD", "0")
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    cli = tmp_path / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli 1.0.0'); sys.exit(0)\n"
        "time.sleep(1.5)\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "out.write_text("
        + (
            "json.dumps({'score': 0.9, 'verdict': 'pass', 'dimensions': {}, "
            "'differences': [], 'suggestions': [], 'reasoning': 'synthetic'})"
            if valid
            else "'not a verdict'"
        )
        + ", encoding='utf-8')\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", str(cli))
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    store = EventStore(db_url)
    await store.initialize()
    manager = JobManager(store, durable_jobs=True)
    h = StartQAHandler(
        QAHandler(event_store=store),
        store,
        manager,
        llm_backend="codex",
        agent_runtime_backend="codex",
        project_dir=str(tmp_path),
        reconstructible_backend=True,
    )
    result = await h.handle({"artifact": artifact, "quality_bar": "pass"})
    assert result.is_ok
    job_id = result.value.meta["job_id"]
    # Shut down the accepting server before the deliberately delayed CLI completes.
    await manager.drain()
    await store.close()
    # Reopen the database through a fresh owner, as after server restart.
    observer_store = EventStore(db_url)
    await observer_store.initialize()
    observer = JobManager(observer_store)
    try:
        initial = await JobResultHandler(event_store=observer_store, job_manager=observer).handle(
            {"job_id": job_id}
        )
        assert initial.is_ok
        assert initial.value.meta["result_available"] is False
        for _ in range(200):
            snapshot = await observer.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.1)
        assert snapshot.status.value == ("completed" if valid else "failed"), snapshot
        result = await JobResultHandler(event_store=observer_store, job_manager=observer).handle(
            {"job_id": job_id}
        )
        assert result.is_ok
        if valid:
            assert "0.90" in result.value.text_content
            qa_session_id = result.value.meta["qa_session_id"]
            assert qa_session_id.startswith("qa-")
            status = await SessionStatusHandler(event_store=observer_store).handle(
                {"session_id": qa_session_id}
            )
            assert status.is_ok
            assert status.value.meta["score"] == 0.9
        else:
            assert result.value.is_error
            assert "Failed to parse QA verdict" in result.value.text_content
        events = await observer_store.query_events()
        assert "SYNTHETIC_ARTIFACT" not in repr(events)
    finally:
        await observer_store.close()


@pytest.mark.asyncio
async def test_sdk_tool_discovery_and_schema() -> None:
    """The new tool is discoverable via the SDK with the QA input schema."""
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    store = EventStore("sqlite+aiosqlite:///:memory:")
    manager = JobManager(store)
    qa = QAHandler(event_store=store)
    adapter = MCPServerAdapter()
    adapter.register_tool(StartQAHandler(qa, store, manager))
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await adapter.serve(transport="stdio")
    tools = await adapter._mcp_server.list_tools()
    tool = next(item for item in tools if item.name == "ouroboros_start_qa")
    assert {"artifact", "quality_bar"}.issubset(tool.input_schema["required"])
    assert (
        tool.input_schema["properties"]["pass_threshold"]
        == qa.definition.to_input_schema()["properties"]["pass_threshold"]
    )


@pytest.mark.asyncio
async def test_sdk_call_returns_pollable_job_receipt(tmp_path: Path) -> None:
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    manager = JobManager(store, durable_jobs=True)
    adapter = MCPServerAdapter()
    adapter.register_tool(
        StartQAHandler(
            QAHandler(event_store=store),
            store,
            manager,
            llm_backend="codex",
            agent_runtime_backend="codex",
            project_dir=str(tmp_path),
            reconstructible_backend=True,
        )
    )
    snapshot = MagicMock(job_id="job_synthetic", cursor=0)
    snapshot.status.value = "queued"
    try:
        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")
        with patch(
            "ouroboros.mcp.tools.background.launch_detached_job",
            new=AsyncMock(return_value=snapshot),
        ):
            result = await adapter._mcp_server.call_tool(
                "ouroboros_start_qa", {"artifact": "a", "quality_bar": "b"}
            )
        assert result.is_error is False
        assert result.structured_content["job_id"] == "job_synthetic"
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="fake executable uses a POSIX shebang")
async def test_external_cancel_stops_detached_qa_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh controller can cancel a real worker while its fake CLI is running."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("OUROBOROS_DASHBOARD", "0")
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    marker = tmp_path / "started"
    cli = tmp_path / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli 1.0.0'); sys.exit(0)\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(15)\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", str(cli))
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    store = EventStore(db_url)
    await store.initialize()
    owner = JobManager(store, durable_jobs=True)
    h = StartQAHandler(
        QAHandler(event_store=store),
        store,
        owner,
        llm_backend="codex",
        agent_runtime_backend="codex",
        project_dir=str(tmp_path),
        reconstructible_backend=True,
    )
    receipt = await h.handle({"artifact": "synthetic", "quality_bar": "pass"})
    assert receipt.is_ok
    job_id = receipt.value.meta["job_id"]
    await owner.drain()
    await store.close()
    observer_store = EventStore(db_url)
    await observer_store.initialize()
    observer = JobManager(observer_store)
    try:
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.1)
        assert marker.exists()
        child_pid = int(marker.read_text(encoding="utf-8"))
        requested = await observer.cancel_job(job_id)
        assert requested.status.value in {"cancel_requested", "cancelled"}
        for _ in range(150):
            terminal = await observer.get_snapshot(job_id)
            if terminal.is_terminal:
                break
            await asyncio.sleep(0.1)
        assert terminal.status.value == "cancelled", terminal
        result = await JobResultHandler(event_store=observer_store, job_manager=observer).handle(
            {"job_id": job_id}
        )
        assert result.is_ok
        assert result.value.is_error
        assert result.value.meta["status"] == "cancelled"
        events = await observer_store.query_events()
        assert not any(event.type == "qa.evaluated" for event in events)
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("the fake CLI remained alive after cancellation")
    finally:
        await observer_store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="fake executable uses a POSIX shebang")
async def test_detached_qa_uses_server_project_dir_without_rebasing_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync and detached QA use the same project-local CLI context."""
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    home, launch, project = (tmp_path / name for name in ("home", "launch", "project"))
    for path in (home, launch, project):
        path.mkdir()
    for path, label in ((launch, "LAUNCH_CONFIG"), (project, "PROJECT_CONFIG")):
        (path / ".codex").mkdir()
        (path / ".codex" / "synthetic-config").write_text(label, encoding="utf-8")
    cli = tmp_path / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli 1.0.0'); sys.exit(0)\n"
        "context = pathlib.Path('.codex/synthetic-config').read_text(encoding='utf-8')\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "out.write_text(json.dumps({'score': 0.9, 'verdict': 'pass', 'dimensions': {}, "
        "'differences': [], 'suggestions': [], 'reasoning': context}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.chdir(launch)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[4] / "src"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", str(cli))
    monkeypatch.setenv("OUROBOROS_DASHBOARD", "0")
    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    server = create_ouroboros_server(
        event_store=store, project_dir=project, runtime_backend="codex", llm_backend="codex"
    )
    try:
        args = {"artifact": "synthetic", "quality_bar": "pass"}
        sync_result = await server._tool_handlers["ouroboros_qa"].handle(args)
        assert sync_result.is_ok
        assert "PROJECT_CONFIG" in sync_result.value.text_content
        receipt = await server._tool_handlers["ouroboros_start_qa"].handle(args)
        assert receipt.is_ok
        job_id = receipt.value.meta["job_id"]
        await server.job_manager.drain()
        await store.close()
        observer_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
        await observer_store.initialize()
        observer = JobManager(observer_store)
        try:
            for _ in range(200):
                snapshot = await observer.get_snapshot(job_id)
                if snapshot.is_terminal:
                    break
                await asyncio.sleep(0.1)
            assert snapshot.status.value == "completed", snapshot
            assert "PROJECT_CONFIG" in snapshot.result_text
            assert "LAUNCH_CONFIG" not in snapshot.result_text
        finally:
            await observer_store.close()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_servers_keep_distinct_project_dirs_without_chdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_dir = tmp_path / "launch"
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    for directory in (launch_dir, project_a, project_b):
        directory.mkdir()
    monkeypatch.chdir(launch_dir)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    manager = JobManager(store, durable_jobs=True)
    snapshot = MagicMock(job_id="job_synthetic", cursor=0)
    snapshot.status.value = "queued"
    first = StartQAHandler(
        QAHandler(event_store=store),
        store,
        manager,
        llm_backend="codex",
        agent_runtime_backend="codex",
        project_dir=str(project_a),
        reconstructible_backend=True,
    )
    second = StartQAHandler(
        QAHandler(event_store=store),
        store,
        manager,
        llm_backend="codex",
        agent_runtime_backend="codex",
        project_dir=str(project_b),
        reconstructible_backend=True,
    )
    try:
        with patch(
            "ouroboros.mcp.tools.background.launch_detached_job",
            new=AsyncMock(return_value=snapshot),
        ) as launch:
            results = await asyncio.gather(
                first.handle({"artifact": "a", "quality_bar": "pass"}),
                second.handle({"artifact": "b", "quality_bar": "pass"}),
            )
        assert all(result.is_ok for result in results)
        requests = [call.kwargs["request"] for call in launch.await_args_list]
        assert {request.project_dir for request in requests} == {str(project_a), str(project_b)}
        assert all(request.cwd == str(launch_dir) for request in requests)
        assert Path.cwd() == launch_dir
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_project_dir_is_rejected_before_worker_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original"
    changed = tmp_path / "changed"
    original.mkdir()
    changed.mkdir()
    monkeypatch.chdir(original)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    manager = JobManager(store, durable_jobs=True)
    handler = StartQAHandler(
        QAHandler(event_store=store),
        store,
        manager,
        llm_backend="codex",
        agent_runtime_backend="codex",
        reconstructible_backend=True,
    )
    monkeypatch.chdir(changed)
    snapshot = MagicMock(job_id="job_synthetic", cursor=0)
    snapshot.status.value = "queued"
    try:
        with patch(
            "ouroboros.mcp.tools.background.launch_detached_job",
            new=AsyncMock(return_value=snapshot),
        ) as launch:
            result = await handler.handle({"artifact": "a", "quality_bar": "pass"})
        assert result.is_err
        assert "project directory" in result.error.message
        launch.assert_not_awaited()
        assert Path.cwd() == changed
    finally:
        await store.close()
