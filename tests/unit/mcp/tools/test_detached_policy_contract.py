"""A real detached worker persists the policy accepted before a config change."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ouroboros.config.loader import get_config_dir
from ouroboros.mcp.detached_jobs import DetachedJobRequest, launch_detached_job
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.mcp.tools import execution_handlers
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore

SEED = """\
goal: Synthetic policy handoff
acceptance_criteria: [Task completes]
constraints: []
execution_mode: legacy
ontology_schema: {name: Synthetic, description: Synthetic, fields: []}
metadata:
  seed_id: synthetic-policy
  version: '1.0.0'
  created_at: '2024-01-01T00:00:00Z'
  ambiguity_score: 0.1
"""


def _write_config(policy: str | None) -> None:
    directory = get_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if policy is None:
        (directory / "config.yaml").unlink(missing_ok=True)
        return
    (directory / "config.yaml").write_text(
        f"execution:\n  default_policy: {policy}\n"
        "  auto_evaluate: false\n  auto_evolve: false\n"
        "orchestrator:\n  runtime_backend: pi\n",
        encoding="utf-8",
    )


def _synthetic_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Only inference is synthetic; use the real Pi runtime and worker launcher."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "pi"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "if '--help' in sys.argv:\n"
        " print('--append-system-prompt --tools --no-tools')\n"
        "else:\n"
        f" with pathlib.Path({str(tmp_path / 'inference.jsonl')!r}).open('a') as f:\n"
        "  f.write(json.dumps({'synthetic': True}) + '\\n')\n"
        " print(json.dumps({'type': 'session', 'id': 'synthetic-native'}))\n"
        " print(json.dumps({'type': 'agent_end', 'messages': [{'role': 'assistant',"
        " 'stopReason': 'error', 'errorMessage': 'Controlled synthetic inference rejection'}]}))\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return cli


@pytest.fixture
async def durable_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
    _synthetic_pi(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic",
        ],
        cwd=project,
        check=True,
    )
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    server = create_ouroboros_server(event_store=store, runtime_backend="pi", llm_backend="pi")
    try:
        yield server, store, project
    finally:
        await server.shutdown()


async def _terminal(manager: JobManager, job_id: str):
    for _ in range(600):
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot
        await asyncio.sleep(0.05)
    pytest.fail(f"Detached job did not settle: {snapshot}")


@pytest.mark.parametrize(
    "selected,later,explicit,assurance,expected",
    [
        (None, "efficient", None, None, "quality_first"),
        ("quality_first", "efficient", None, None, "quality_first"),
        ("efficient", "quality_first", None, None, "adaptive"),
        ("ask", "quality_first", None, None, "adaptive"),
        ("quality_first", "efficient", "adaptive", "strict", "adaptive"),
        ("efficient", "quality_first", "quality_first", None, "quality_first"),
    ],
)
async def test_default_detached_policy_survives_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: str | None,
    later: str,
    explicit: str | None,
    assurance: str | None,
    expected: str,
    durable_server,
) -> None:
    _write_config(selected)
    server, store, project = durable_server
    # This factory's production DEFAULT is durable_jobs=True.
    manager = server.job_manager
    assert manager.durable_jobs_enabled is True
    real_start = execution_handlers.start_background_tool_job

    async def change_config_before_dispatch(**kwargs):
        _write_config(later)
        return await real_start(**kwargs)

    monkeypatch.setattr(
        execution_handlers, "start_background_tool_job", change_config_before_dispatch
    )
    start = server._tool_handlers["ouroboros_start_execute_seed"]
    args = {
        "seed_content": SEED,
        "cwd": str(project),
        "use_worktree": False,
        "skip_qa": True,
        "auto_evaluate": False,
        "auto_evolve": False,
        "idempotency_key": "synthetic-policy",
    }
    if explicit is not None:
        args["efficiency_mode"] = explicit
    if assurance is not None:
        args["frugality_assurance"] = assurance
    observer = EventStore(store.database_url)
    try:
        receipt = await start.handle(args)
        assert receipt.is_ok, receipt
        meta = receipt.value.meta
        replay = await start.handle(args)
        assert replay.is_ok and replay.value.meta == meta
        # Closing the accepting server must not own or cancel the detached job.
        await server.shutdown()
        await observer.initialize()
        reopened = JobManager(observer)
        snapshot = await _terminal(reopened, meta["job_id"])
        assert (tmp_path / "inference.jsonl").is_file(), snapshot
        session = await SessionRepository(observer).reconstruct_session(snapshot.links.session_id)
        assert session.is_ok, session
        contract = session.value.progress["execution_contract"]
        preferences = contract["execution_preferences"]
        created = await observer.query_events(
            aggregate_id=meta["job_id"], event_type="mcp.job.created"
        )
        assert len(created) == 1
        assert created[0].data["owner_pid"] != os.getpid()
        print(
            json.dumps(
                {
                    "selected": selected,
                    "later": later,
                    "explicit": explicit,
                    "receipt_mode": meta["efficiency_mode"],
                    "persisted": preferences,
                    "job_status": snapshot.status.value,
                    "worker_pid": created[0].data["owner_pid"],
                }
            )
        )
        assert meta["efficiency_mode"] == expected
        assert preferences == {
            "efficiency_mode": expected,
            "frugality_assurance": assurance or ("observe" if expected == "adaptive" else "off"),
            "frugality_assurance_explicit": assurance is not None,
        }
        assert f"Efficiency Mode: {expected}" in receipt.value.text_content
        assert f"Efficiency Mode: {expected}" in snapshot.result_text
    finally:
        await server.shutdown()
        await observer.close()


async def test_legacy_detached_request_without_policy_still_runs(durable_server) -> None:
    """An old legitimate request lacks the new producer's explicit mode argument."""
    server, store, project = durable_server
    _write_config("efficient")
    await store.initialize()
    request = DetachedJobRequest.from_json(
        {
            "job_id": await server.job_manager.allocate_job_id(),
            "tool_name": "ouroboros_start_execute_seed",
            "arguments": {
                "seed_content": SEED,
                "cwd": str(project),
                "use_worktree": False,
                "skip_qa": True,
                "auto_evaluate": False,
                "auto_evolve": False,
            },
            "database_url": store.database_url,
            "cwd": str(project),
            "runtime_backend": "pi",
            "llm_backend": "pi",
        }
    )
    assert "efficiency_mode" not in request.arguments
    await launch_detached_job(job_manager=server.job_manager, event_store=store, request=request)
    snapshot = await _terminal(server.job_manager, request.job_id)
    session = await SessionRepository(store).reconstruct_session(snapshot.links.session_id)
    assert session.is_ok
    assert session.value.progress["execution_contract"]["execution_preferences"] == {
        "efficiency_mode": "adaptive",
        "frugality_assurance": "observe",
        "frugality_assurance_explicit": False,
    }


async def test_detached_resume_retains_persisted_policy(durable_server) -> None:
    """A detached resume keeps preferences and the existing live-authority rejection."""
    import yaml

    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator.pi_runtime import PiRuntime
    from ouroboros.orchestrator.runner import OrchestratorRunner

    server, store, project = durable_server
    _write_config("efficient")
    await store.initialize()
    runner = OrchestratorRunner(
        adapter=PiRuntime(cwd=project),
        event_store=store,
        task_cwd=str(project),
        efficiency_mode="adaptive",
        fat_harness_mode=False,
    )
    prepared = await runner.prepare_session(Seed.from_dict(yaml.safe_load(SEED)))
    assert prepared.is_ok, prepared
    sessions = SessionRepository(store)
    paused_result = await sessions.mark_paused(prepared.value.session_id, "Synthetic pause fixture")
    assert paused_result.is_ok and paused_result.value
    start = server._tool_handlers["ouroboros_start_execute_seed"]
    args = {
        "seed_content": SEED,
        "cwd": str(project),
        "use_worktree": False,
        "skip_qa": True,
        "auto_evaluate": False,
        "auto_evolve": False,
    }
    paused = await sessions.reconstruct_session(prepared.value.session_id)
    assert paused.is_ok and paused.value.status.value == "paused", paused
    original = paused.value.progress["execution_contract"]["execution_preferences"]
    _write_config("quality_first")
    resumed = await start.handle({**args, "session_id": paused.value.session_id})
    assert resumed.is_ok, resumed
    assert resumed.value.meta["efficiency_mode"] == "adaptive"
    resumed_snapshot = await _terminal(server.job_manager, resumed.value.meta["job_id"])
    assert resumed_snapshot.links.session_id == paused.value.session_id
    final = await sessions.reconstruct_session(paused.value.session_id)
    assert final.is_ok
    assert final.value.progress["execution_contract"]["execution_preferences"] == original
    failures = await store.query_events(
        aggregate_id=paused.value.session_id, event_type="orchestrator.session.failed"
    )
    assert failures[0].data["error_details"]["resume_blocked"] == "process_local_resume_unavailable"
    assert resumed_snapshot.result_meta["resume_requested"] is True
    assert resumed_snapshot.result_meta["efficiency_mode"] == "adaptive"
    created = await store.query_events(event_type="mcp.job.created", limit=10)
    assert len(created) == 1
