"""Synthetic vertical tests for non-authoritative inline source material."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import Client
from mcp.server import MCPServer
import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from tests.unit.mcp.tools.test_interview_batch_prototype import _proposal


@pytest.mark.asyncio
async def test_sdk_material_is_optional_separate_and_resumable(tmp_path):
    material = "A previous note says synthetic apples are green."
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="materials-prototype")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        start = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "structured_first_batch": True},
        )
        sid, digest = start.meta["session_id"], start.meta["batch_digest"]
        material_only = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_answers": [],
                "inline_material_text": material,
                "inline_material_consent": True,
            },
        )
        assert not material_only.is_error
        assert material_only.meta["inline_material_status"] == "ready"
        assert not material_only.meta["independence_confirmed"]
        assert any("synthetic apples are green" in item.text for item in material_only.content)
        answered = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_independence_confirmed": True,
                "batch_answers": [
                    {"question_id": item["question_id"], "answer": "A user decision"}
                    for item in start.meta["questions"]
                ],
            },
        )
        assert not answered.is_error and answered.meta["batch_complete"]
        reloaded = await engine.load_state(sid)
        assert reloaded.is_ok
        assert reloaded.value.inline_material.text == material
        assert engine._state_file_path(sid).stat().st_mode & 0o777 == 0o600
        seed_context = object.__new__(SeedGenerator)._build_interview_context(reloaded.value)
        score_context = object.__new__(AmbiguityScorer)._build_interview_context(reloaded.value)
        assert material not in seed_context and "synthetic apples" not in seed_context
        assert material not in score_context and "synthetic apples" not in score_context
        replay = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_answers": [],
                "inline_material_text": material,
                "inline_material_consent": True,
            },
        )
        assert not replay.is_error
        assert adapter.complete.await_count == 1
        assert material not in str(adapter.complete.await_args_list)
        conflict = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_answers": [],
                "inline_material_text": "A different source",
                "inline_material_consent": True,
            },
        )
        assert conflict.is_error
        assert (await engine.load_state(sid)).value.inline_material.text == material
        ignored_input = await client.call_tool(
            "ouroboros_interview",
            {"session_id": sid, "inline_material_text": material, "inline_material_consent": True},
        )
        assert ignored_input.is_error
        before_rejected = engine._state_file_path(sid).read_bytes()
        simultaneous = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "yes",
                "inline_material_user_followup_question": "Can we add export?",
                "opening_user_followup_question": "Can we add export?",
            },
        )
        assert simultaneous.is_error
        assert engine._state_file_path(sid).read_bytes() == before_rejected
        for candidate in ("synthetic apples are green", "Can we use synthetic apples are green?"):
            rejected = await client.call_tool(
                "ouroboros_interview",
                {
                    "session_id": sid,
                    "answer": "yes",
                    "last_question": candidate,
                    "confused_terms": ["ontology"],
                },
            )
            assert rejected.is_error
            assert engine._state_file_path(sid).read_bytes() == before_rejected
        adapter.complete.side_effect = None
        from tests.unit.bigbang.test_turn_planner import _payload

        adapter.complete.return_value = Result.ok(SimpleNamespace(content=json.dumps(_payload())))
        reopened = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "yes",
                "inline_material_user_followup_question": "Can we use synthetic apples are green?",
            },
        )
        assert not reopened.is_error
        after_reopen = (await engine.load_state(sid)).value
        assert any(
            round_data.question == "Can we use synthetic apples are green?"
            for round_data in after_reopen.rounds
        )
        loaded = (await engine.load_state(sid)).value
        loaded.rounds.append(
            InterviewRound(round_number=len(loaded.rounds) + 1, question="Next question?")
        )
        loaded.mark_updated()
        assert (await engine.save_state(loaded)).is_ok
        echo = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "A user response",
                "last_question": "Next question? synthetic apples are green",
            },
        )
        assert echo.is_error
        assert (await engine.load_state(sid)).value.rounds[-1].user_response is None


@pytest.mark.asyncio
async def test_cancelled_draft_keeps_material_and_answer(tmp_path):
    entered_draft = asyncio.Event()
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    events = []
    handler._emit_event_bg = events.append
    from ouroboros.mcp.tools.interview_batch import answer_batch, start_batch

    started = await start_batch(
        handler, {"initial_context": "Build a tool", "structured_first_batch": True}
    )
    assert started.is_ok
    meta = started.value.meta
    original_save = engine.save_state

    async def interrupted_save(state):
        if state.inline_material is not None and state.inline_material.status == "ready":
            entered_draft.set()
            await asyncio.Future()
        return await original_save(state)

    with patch.object(engine, "save_state", side_effect=interrupted_save):
        task = asyncio.create_task(
            answer_batch(
                handler,
                {
                    "session_id": meta["session_id"],
                    "batch_confirm_digest": meta["batch_digest"],
                    "batch_independence_confirmed": True,
                    "batch_answers": [
                        {
                            "question_id": meta["questions"][0]["question_id"],
                            "answer": "User decision",
                        }
                    ],
                    "inline_material_text": "Synthetic material",
                    "inline_material_consent": True,
                },
            )
        )
        await asyncio.wait_for(entered_draft.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    loaded = await engine.load_state(meta["session_id"])
    assert loaded.is_ok
    assert loaded.value.inline_material.text == "Synthetic material"
    assert loaded.value.inline_material.status == "pending"
    assert any(item.user_response == "User decision" for item in loaded.value.rounds)
    assert len(events) >= 2
    event_count = len(events)
    retried = await answer_batch(
        handler,
        {
            "session_id": meta["session_id"],
            "batch_confirm_digest": meta["batch_digest"],
            "batch_answers": [],
            "inline_material_retry": True,
        },
    )
    assert retried.is_ok
    assert (await engine.load_state(meta["session_id"])).value.inline_material.status == "ready"
    assert len(events) == event_count
    assert adapter.complete.await_count == 1


@pytest.mark.asyncio
async def test_sdk_retries_unavailable_draft_only_when_requested(tmp_path, capfd, caplog):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="materials-retry")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a tool",
                "structured_first_batch": True,
            },
        )
        sid, digest = started.meta["session_id"], started.meta["batch_digest"]
        invalid = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_answers": [],
                "inline_material_text": "SYNTHETIC_SECRET_456\ud800",
                "inline_material_consent": True,
            },
        )
        assert invalid.is_error
        assert "SYNTHETIC_SECRET_456" not in str(invalid)
        captured = capfd.readouterr()
        assert "SYNTHETIC_SECRET_456" not in captured.out + captured.err + caplog.text
        response = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_answers": [],
                "inline_material_text": "\u2066",
                "inline_material_consent": True,
            },
        )
        assert response.meta["inline_material_status"] == "unavailable"
        replay = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_answers": [],
                "inline_material_text": "\u2066",
                "inline_material_consent": True,
            },
        )
        assert replay.meta["inline_material_status"] == "unavailable"
        assert adapter.complete.await_count == 1
        rejected = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_reject": True,
            },
        )
        assert not rejected.is_error
        mixed = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "A user answer",
                "inline_material_retry": True,
            },
        )
        assert mixed.is_error
        retried = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "inline_material_retry": True,
            },
        )
        assert retried.meta["inline_material_status"] == "unavailable"
        assert adapter.complete.await_count == 1


@pytest.mark.asyncio
async def test_owned_event_store_survives_draft_cancellation(tmp_path):
    from ouroboros.persistence.event_store import EventStore

    entered = asyncio.Event()
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    handler._event_store = EventStore(url)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {"initial_context": "Build a tool", "structured_first_batch": True}
    )
    assert started.is_ok
    meta = started.value.meta
    original_save = engine.save_state

    async def interrupted_save(state):
        if state.inline_material is not None and state.inline_material.status == "ready":
            entered.set()
            await asyncio.Future()
        return await original_save(state)

    with patch.object(engine, "save_state", side_effect=interrupted_save):
        task = asyncio.create_task(
            handler.handle(
                {
                    "session_id": meta["session_id"],
                    "batch_confirm_digest": meta["batch_digest"],
                    "batch_independence_confirmed": True,
                    "batch_answers": [
                        {
                            "question_id": meta["questions"][0]["question_id"],
                            "answer": "User decision",
                        }
                    ],
                    "inline_material_text": "Synthetic material",
                    "inline_material_consent": True,
                }
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    reopened = EventStore(url)
    await reopened.initialize()
    try:
        events, _ = await reopened.get_events_after("interview", meta["session_id"])
        assert any(event.type == "interview.response.recorded" for event in events)
        assert all("Synthetic material" not in str(event) for event in events)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_corrupt_material_is_absent_from_sdk_error_and_logs(tmp_path, capfd, caplog):
    from ouroboros.mcp.tools.authoring_handlers import _plugin_load_state, _plugin_save_state

    sid = "interview_0123456789abcdef"
    engine = InterviewEngine(llm_adapter=SimpleNamespace(), state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, None)
    server = MCPServerAdapter(name="corrupt-material-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    engine._state_file_path(sid).write_text(
        json.dumps(
            {
                "interview_id": sid,
                "initial_context": "Build a tool",
                "inline_material": {
                    "text": "SYNTHETIC_SECRET_123",
                    "status": "ready",
                    "draft": None,
                },
            }
        )
    )
    async with Client(server._mcp_server, mode="auto") as client:
        result = await client.call_tool("ouroboros_interview", {"session_id": sid})
        assert result.is_error
        assert "SYNTHETIC_SECRET_123" not in str(result)
    plugin_load = await _plugin_load_state(tmp_path, sid)
    state = InterviewState(interview_id=sid, initial_context="Build a tool")
    save = await engine.save_state(state)
    plugin_save = await _plugin_save_state(tmp_path, state)
    assert plugin_load.is_err and save.is_err and plugin_save.is_err
    assert "SYNTHETIC_SECRET_123" not in str((plugin_load.error, save.error, plugin_save.error))
    assert await engine.list_interviews() == []
    captured = capfd.readouterr()
    assert "SYNTHETIC_SECRET_123" not in captured.out + captured.err
    assert "SYNTHETIC_SECRET_123" not in caplog.text


def test_local_excerpts_are_literal_bounded_and_relevance_ranked():
    from ouroboros.mcp.tools.interview_materials import _local_excerpts

    text = (
        "Unrelated intro\nBudget is fixed\nPriority budget is reviewed\nBudget is fixed\n"
        + "x" * 500
        + "\n\u2066hidden"
    )
    draft = _local_excerpts(text, "Budget priority")
    assert draft is not None
    assert draft.source_quotes[:2] == ("Priority budget is reviewed", "Budget is fixed")
    assert len(draft.source_quotes) == 4
    assert all(len(item) <= 400 and item in text for item in draft.source_quotes)
    assert draft.inferred_assumptions == ()
    assert draft.origin == "local_excerpts"
    assert _local_excerpts("\u2066", "Anything") is None


def test_local_excerpts_rank_and_show_late_matching_words():
    from ouroboros.mcp.tools.interview_materials import _local_excerpts

    relevant = "x" * 401 + " target and context"
    text = "\n".join([f"plain{i}" for i in range(5)] + [relevant])
    draft = _local_excerpts(text, "target")
    assert draft is not None
    assert "target" in draft.source_quotes[0]
    assert len(draft.source_quotes[0]) <= 400
    assert draft.source_quotes[0] in relevant
    assert draft.source_quotes[1:] == ("plain0", "plain1", "plain2", "plain3")


def test_local_excerpts_reject_unsafe_character_beyond_display_limit():
    from ouroboros.mcp.tools.interview_materials import _local_excerpts

    assert _local_excerpts("x" * 401 + "\u2066 target", "target") is None


def test_legacy_model_draft_is_not_mislabeled_as_local():
    from ouroboros.interview_adapters.inline_materials import InlineMaterial, MaterialDraft
    from ouroboros.mcp.tools.interview_materials import material_content

    draft = MaterialDraft.model_validate(
        {
            "source_quotes": ["Earlier source"],
            "inferred_assumptions": ["An earlier unconfirmed guess"],
        }
    )
    state = InterviewState(
        interview_id="interview_0123456789abcdef",
        initial_context="A tool",
        inline_material=InlineMaterial(text="Earlier source", status="ready", draft=draft),
    )
    text = material_content(state)[0].text
    assert draft.origin == "legacy_model"
    assert "earlier model-assisted draft" in text
    assert "Earlier model assumptions (unconfirmed, not requirements)" in text
    assert "An earlier unconfirmed guess" in text
    assert "ranked by word overlap" not in text
    with pytest.raises(ValueError):
        MaterialDraft(
            origin="local_excerpts",
            source_quotes=("Earlier source",),
            inferred_assumptions=("guess",),
        )


@pytest.mark.asyncio
async def test_stale_state_retains_actionable_conflict_message(tmp_path):
    engine = InterviewEngine(llm_adapter=SimpleNamespace(), state_dir=tmp_path, model="synthetic")
    started = await engine.start_interview("Build a tool")
    sid = started.value.interview_id
    first = (await engine.load_state(sid)).value
    stale = (await engine.load_state(sid)).value
    assert (await engine.save_state(first)).is_ok
    rejected = await engine.save_state(stale)
    assert rejected.is_err
    assert "interview state changed; reload before retrying" in str(rejected.error)


@pytest.mark.asyncio
async def test_retry_respects_other_process_lock_and_recovers(tmp_path):
    from ouroboros.interview_adapters.inline_materials import InlineMaterial
    from ouroboros.mcp.tools.interview_batch import retry_material

    adapter = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(
                SimpleNamespace(
                    content='{"source_quotes":["Synthetic source"],"inferred_assumptions":[]}',
                )
            )
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    state = (
        await engine.start_interview("Build a tool", interview_id="interview_0123456789abcdef")
    ).value
    state.inline_material = InlineMaterial(text="Synthetic source")
    assert (await engine.save_state(state)).is_ok
    lock_target = engine._state_file_path(state.interview_id).with_suffix(".material")
    code = (
        "from pathlib import Path; from ouroboros.core.file_lock import file_lock; "
        "import sys; lock=file_lock(Path(sys.argv[1]),blocking=False); "
        "lock.__enter__(); print('ready',flush=True); sys.stdin.readline(); lock.__exit__(None,None,None)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(lock_target),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), timeout=5) == b"ready\n"
        args = {"session_id": state.interview_id, "inline_material_retry": True}
        busy = await retry_material(handler, args)
        assert busy.is_err
        assert adapter.complete.await_count == 0
    finally:
        process.kill()
        assert await process.wait() != 0
    retried = await retry_material(handler, args)
    assert retried.is_ok
    assert adapter.complete.await_count == 0
    assert (await engine.load_state(state.interview_id)).value.inline_material.status == "ready"


@pytest.mark.asyncio
async def test_concurrent_retry_fails_fast_and_cancel_releases_lock(tmp_path):
    from ouroboros.interview_adapters.inline_materials import InlineMaterial
    from ouroboros.mcp.tools.interview_batch import retry_material

    entered = asyncio.Event()

    adapter = SimpleNamespace(complete=AsyncMock())
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    state = (
        await engine.start_interview("Build a tool", interview_id="interview_0123456789abcdef")
    ).value
    state.inline_material = InlineMaterial(text="Synthetic source")
    assert (await engine.save_state(state)).is_ok
    args = {"session_id": state.interview_id, "inline_material_retry": True}
    original_save = engine.save_state

    async def interrupted_save(state):
        if state.inline_material is not None and state.inline_material.status == "ready":
            entered.set()
            await asyncio.Future()
        return await original_save(state)

    with patch.object(engine, "save_state", side_effect=interrupted_save):
        first = asyncio.create_task(retry_material(handler, args))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await retry_material(handler, args)
        assert second.is_err and adapter.complete.await_count == 0
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    retried = await retry_material(handler, args)
    assert retried.is_ok and adapter.complete.await_count == 0


@pytest.mark.asyncio
async def test_sdk_busy_optional_draft_keeps_batch_progress_available(tmp_path):
    from ouroboros.core.file_lock import file_lock

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="busy-material-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a tool",
                "structured_first_batch": True,
            },
        )
        sid = started.meta["session_id"]
        qid = started.meta["questions"][0]["question_id"]
        with file_lock(engine._state_file_path(sid).with_suffix(".material"), blocking=False):
            response = await client.call_tool(
                "ouroboros_interview",
                {
                    "session_id": sid,
                    "batch_confirm_digest": started.meta["batch_digest"],
                    "batch_independence_confirmed": True,
                    "batch_answers": [{"question_id": qid, "answer": "User decision"}],
                    "inline_material_text": "Synthetic source",
                    "inline_material_consent": True,
                },
            )
        assert not response.is_error
        assert qid in response.meta["answered_question_ids"]
        assert response.meta["inline_material_status"] == "pending"
        assert adapter.complete.await_count == 1
        resumed = await client.call_tool("ouroboros_interview", {"session_id": sid})
        assert not resumed.is_error and qid in resumed.meta["answered_question_ids"]
        from ouroboros.core.errors import ValidationError

        next_qid = started.meta["questions"][1]["question_id"]
        with patch.object(
            engine,
            "save_state",
            new=AsyncMock(return_value=Result.err(ValidationError("synthetic failure"))),
        ):
            failed = await client.call_tool(
                "ouroboros_interview",
                {
                    "session_id": sid,
                    "batch_confirm_digest": started.meta["batch_digest"],
                    "batch_independence_confirmed": True,
                    "batch_answers": [{"question_id": next_qid, "answer": "Another decision"}],
                },
            )
        assert failed.is_error
        persisted = (await engine.load_state(sid)).value
        assert next(r for r in persisted.rounds if r.question_id == next_qid).user_response is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reload", "discard"])
async def test_nongating_fallback(tmp_path, failure, capfd, caplog):
    from ouroboros.core.errors import ValidationError

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="fallback-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "structured_first_batch": True},
        )
        sid = started.meta["session_id"]
        qid = started.meta["questions"][0]["question_id"]
        original = engine.load_state
        count = 0

        async def conditional_load(interview_id):
            nonlocal count
            count += 1
            if failure == "reload" and count > 1:
                return Result.err(ValidationError("synthetic unavailable"))
            return await original(interview_id)

        async def deferred(engine_arg, state):
            if failure == "discard":
                state.rounds = [r for r in state.rounds if not r.batch_id]
                state.batch_confirmed_digest = None
                assert (await engine_arg.save_state(state)).is_ok
            return state, "busy"

        with (
            patch.object(engine, "load_state", side_effect=conditional_load),
            patch(
                "ouroboros.mcp.tools.interview_materials.draft_inline_material",
                side_effect=deferred,
            ),
        ):
            result = await client.call_tool(
                "ouroboros_interview",
                {
                    "session_id": sid,
                    "batch_confirm_digest": started.meta["batch_digest"],
                    "batch_independence_confirmed": True,
                    "batch_answers": [{"question_id": qid, "answer": "decision"}],
                    "inline_material_text": "SYNTHETIC_FALLBACK_789",
                    "inline_material_consent": True,
                },
            )
        assert not result.is_error
        assert result.meta["inline_material_status"] == "unknown"
        assert any("Your batch update was saved before" in item.text for item in result.content)
        assert "SYNTHETIC_FALLBACK_789" not in str(result)
        assert (await original(sid)).value.inline_material.text == "SYNTHETIC_FALLBACK_789"
    output = capfd.readouterr()
    assert "SYNTHETIC_FALLBACK_789" not in output.out + output.err + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [True, False])
async def test_material_safe_default_does_not_store_source_echo(tmp_path, pending):
    from ouroboros.interview_adapters.inline_materials import InlineMaterial

    adapter = SimpleNamespace(complete=AsyncMock())
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    handler._score_interview_state = AsyncMock(return_value=None)
    state = (
        await engine.start_interview("Build a tool", interview_id="interview_0123456789abcdef")
    ).value
    state.inline_material = InlineMaterial(text="Synthetic private source")
    state.rounds.append(
        InterviewRound(
            round_number=1,
            question="What is needed?",
            user_response=None if pending else "A tool",
        )
    )
    assert (await engine.save_state(state)).is_ok
    server = MCPServerAdapter(name="material-synthesis-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        result = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": state.interview_id,
                "answer": "[from-auto][safe-default-synthesis] done",
                "last_question": "Synthetic private source",
            },
        )
        assert not result.is_error
    loaded = (await engine.load_state(state.interview_id)).value
    context = object.__new__(SeedGenerator)._build_interview_context(loaded)
    assert "Synthetic private source" not in context


@pytest.mark.asyncio
async def test_sdk_material_is_never_sent_to_question_provider_on_resume(tmp_path):
    marker = "SYNTHETIC_MATERIAL_PRIVACY_498"
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content="What outcome matters?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="material-provider-boundary")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a tool",
                "structured_first_batch": True,
            },
        )
        sid, digest = started.meta["session_id"], started.meta["batch_digest"]
        submitted = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_independence_confirmed": True,
                "batch_answers": [
                    {
                        "question_id": started.meta["questions"][0]["question_id"],
                        "answer": "A user decision",
                    }
                ],
                "inline_material_text": marker,
                "inline_material_consent": True,
            },
        )
        assert not submitted.is_error and submitted.meta["inline_material_status"] == "ready"
        assert marker in str(submitted.content)
        assert adapter.complete.await_count == 1
        discarded = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_reject": True,
            },
        )
        assert not discarded.is_error
        resumed = await client.call_tool("ouroboros_interview", {"session_id": sid})
        assert not resumed.is_error
        assert adapter.complete.await_count == 2
        assert marker not in str(adapter.complete.await_args_list)
