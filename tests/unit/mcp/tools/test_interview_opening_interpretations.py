"""An opt-in opening keeps generated interpretations out of user intent."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


def _proposal():
    return json.dumps(
        {
            "interpretations": [
                {
                    "interpretation": "SENTINEL_ALPHA",
                    "evidence": "User asked for a tool",
                    "uncertainty": "Who uses it?",
                },
                {
                    "interpretation": "SENTINEL_BETA",
                    "evidence": "User asked for a tool",
                    "uncertainty": "How is it shared?",
                },
            ]
        }
    )


@pytest.mark.asyncio
async def test_sdk_start_resume_and_projection_preserve_proposals(tmp_path):
    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="opening-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        schema = (await client.list_tools()).tools[0].input_schema["properties"]
        assert schema["propose_opening_interpretations"]["type"] == "boolean"
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        assert not started.is_error
        text = started.content[0].text
        assert text.index("SENTINEL_ALPHA") < text.index("SENTINEL_BETA") < text.index("Neither")
        assert started.meta["opening_interpretations"]["provenance"] == "proposed"
        items = started.meta["opening_interpretations"]["interpretations"]
        assert len({item["item_id"] for item in items}) == 2
        assert all(item["provenance"] == "proposed" for item in items)
        sid = started.meta["session_id"]
        resumed = await client.call_tool("ouroboros_interview", {"session_id": sid})
        assert not resumed.is_error
        assert "SENTINEL_ALPHA" in resumed.content[0].text
        assert resumed.meta["opening_interpretations"] == started.meta["opening_interpretations"]
    state = (await engine.load_state(sid)).value
    assert state.proposed_opening.provenance == "proposed"
    generator = SeedGenerator(llm_adapter=llm, model="synthetic", output_dir=tmp_path / "seeds")
    context = generator._build_interview_context(state)
    assert "SENTINEL_ALPHA" not in context and "SENTINEL_BETA" not in context
    # Neither a label nor an ordinary approval supplies any proposal content to extraction.
    for answer in ("1", "yes", "neither"):
        snapshot = state.model_copy(deep=True)
        updated = await engine.record_response(snapshot, answer, snapshot.rounds[-1].question)
        assert updated.is_ok
        context = generator._build_interview_context(snapshot)
        assert "SENTINEL_ALPHA" not in context and "SENTINEL_BETA" not in context
        assert snapshot.proposed_opening.provenance == "proposed"


@pytest.mark.asyncio
async def test_invalid_provider_does_not_invent_or_confirm(tmp_path):
    llm = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(SimpleNamespace(content='{"interpretations": []}'))
        )
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    result = await handler.handle(
        {"initial_context": "Build a tool", "propose_opening_interpretations": True}
    )
    assert result.is_ok and result.value.is_error
    state = (await engine.load_state(result.value.meta["session_id"])).value
    assert state.proposed_opening is None
    assert state.rounds == []


@pytest.mark.parametrize(
    "payload",
    [
        {"interpretations": [{"interpretation": "same", "evidence": "x", "uncertainty": "y"}] * 2},
        {
            "interpretations": [
                {
                    "interpretation": "a",
                    "evidence": "x",
                    "uncertainty": "y",
                    "provenance": "confirmed",
                },
                {"interpretation": "b", "evidence": "x", "uncertainty": "y"},
            ]
        },
        {
            "interpretations": [
                {"interpretation": "[from-user] invented", "evidence": "x", "uncertainty": "y"},
                {"interpretation": "b", "evidence": "x", "uncertainty": "y"},
            ]
        },
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_forged_proposals_are_not_persisted(tmp_path, payload):
    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=json.dumps(payload))))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    result = await handler.handle(
        {"initial_context": "A tool", "propose_opening_interpretations": True}
    )
    assert result.is_ok and result.value.is_error
    state = (await engine.load_state(result.value.meta["session_id"])).value
    assert state.proposed_opening is None and not state.rounds


@pytest.mark.asyncio
async def test_incompatible_presentation_is_rejected_before_creation(tmp_path):
    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    result = await handler.handle(
        {
            "initial_context": "A tool",
            "propose_opening_interpretations": True,
            "structured_first_batch": True,
        }
    )
    assert result.is_err
    assert not list(tmp_path.glob("interview_*.json"))
    llm.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_question_rephrase_keeps_proposals_and_seed_projection_clean(tmp_path):
    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    started = await handler.handle(
        {"initial_context": "A tool", "propose_opening_interpretations": True}
    )
    assert started.is_ok and not started.value.is_error
    sid = started.value.meta["session_id"]
    state = (await engine.load_state(sid)).value
    state.rounds[-1].question = "Please clarify in your own words."
    assert (await engine.save_state(state)).is_ok
    resumed = await handler.handle({"session_id": sid})
    assert resumed.is_ok and "SENTINEL_ALPHA" in resumed.value.content[0].text
    assert "Please clarify in your own words." in resumed.value.content[0].text
    generator = SeedGenerator(llm_adapter=llm, model="synthetic", output_dir=tmp_path / "seeds")
    assert "SENTINEL_ALPHA" not in generator._build_interview_context(
        (await engine.load_state(sid)).value
    )


@pytest.mark.asyncio
async def test_calibration_does_not_drop_or_rephrase_proposed_opening(tmp_path):
    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    started = await handler.handle(
        {"initial_context": "A tool", "propose_opening_interpretations": True}
    )
    sid = started.value.meta["session_id"]
    calibrated = await handler.handle({"session_id": sid, "calibration_input": "I am a beginner"})
    assert calibrated.is_ok
    assert calibrated.value.meta["question_rephrased"] is False
    assert (
        calibrated.value.meta["opening_interpretations"]
        == started.value.meta["opening_interpretations"]
    )
    assert "SENTINEL_ALPHA" in calibrated.value.content[0].text
    assert "Neither" in calibrated.value.content[0].text
    assert llm.complete.await_count == 1


@pytest.mark.asyncio
async def test_failed_proposal_save_does_not_report_or_persist_it(tmp_path):
    from ouroboros.core.errors import ValidationError

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    original_save = engine.save_state
    calls = 0

    async def fail_second_save(state):
        nonlocal calls
        calls += 1
        if calls == 2:
            return Result.err(ValidationError("synthetic save failure"))
        return await original_save(state)

    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    with patch.object(engine, "save_state", side_effect=fail_second_save):
        result = await handler.handle(
            {"initial_context": "A tool", "propose_opening_interpretations": True}
        )
    assert result.is_err
    saved = list(tmp_path.glob("interview_*.json"))
    assert len(saved) == 1
    assert "SENTINEL_ALPHA" not in saved[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_sdk_echo_of_opening_cannot_promote_proposals(tmp_path):
    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    llm = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content="What else?")),
                Result.ok(SimpleNamespace(content="Anything more?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="opening-echo-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        sid = started.meta["session_id"]
        display = (await engine.load_state(sid)).value.proposed_opening.render()
        assert display in started.content[0].text
        assert "SENTINEL_ALPHA" in display
        before = engine._state_file_path(sid).read_bytes()
        answered = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "Neither; I want a simple personal tool.",
                "last_question": display,
                "confused_terms": ["ontology"],
                "references": [
                    {"reference_id": "example", "label": "Example", "origin": "user_text"}
                ],
            },
        )
        assert answered.is_error
        assert engine._state_file_path(sid).read_bytes() == before
        assert (await engine.load_state(sid)).value.rounds[0].user_response is None
        answered = await client.call_tool(
            "ouroboros_interview",
            {"session_id": sid, "answer": "Neither; I want a simple personal tool."},
        )
        assert not answered.is_error
        later = await client.call_tool(
            "ouroboros_interview",
            {"session_id": sid, "answer": "It is for me.", "last_question": display},
        )
        assert later.is_error
        later = await client.call_tool(
            "ouroboros_interview", {"session_id": sid, "answer": "It is for me."}
        )
        assert not later.is_error
        completed = (await engine.load_state(sid)).value
        completed.rounds.pop()  # No server-issued question is pending on reopen.
        assert (await engine.save_state(completed)).is_ok
        reopened = await client.call_tool(
            "ouroboros_interview",
            {"session_id": sid, "answer": "yes", "last_question": display},
        )
        assert reopened.is_error
    state = (await engine.load_state(sid)).value
    assert state.rounds[1].question == "What else?"
    assert "SENTINEL_ALPHA" not in state.rounds[0].question
    assert "SENTINEL_BETA" not in state.rounds[0].question
    context = SeedGenerator._build_interview_context(object.__new__(SeedGenerator), state)
    assert "SENTINEL_ALPHA" not in context
    assert "SENTINEL_BETA" not in context
    assert "personal tool" in context


@pytest.mark.parametrize("pending", [True, False])
@pytest.mark.asyncio
async def test_sdk_safe_default_synthesis_does_not_store_opening_echo(tmp_path, pending):
    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    handler._score_interview_state = AsyncMock(return_value=None)
    server = MCPServerAdapter(name="opening-synthesis-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        sid = started.meta["session_id"]
        state = (await engine.load_state(sid)).value
        display = state.proposed_opening.render()
        if not pending:
            state.rounds.clear()
            assert (await engine.save_state(state)).is_ok
        result = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "[from-auto][safe-default-synthesis] done",
                "last_question": display,
                "confused_terms": ["ontology"],
                "references": [
                    {"reference_id": "example", "label": "Example", "origin": "user_text"}
                ],
            },
        )
        assert not result.is_error
    state = (await engine.load_state(sid)).value
    context = SeedGenerator._build_interview_context(object.__new__(SeedGenerator), state)
    assert "SENTINEL_ALPHA" not in context
    assert "SENTINEL_BETA" not in context
    assert [cue.reference_id for cue in state.reference_cues] == ["example"]
    assert "ontology" in state.pending_confused_terms


@pytest.mark.asyncio
async def test_plugin_rejects_existing_opening_before_mutation_or_dispatch(tmp_path):
    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    in_process = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    in_process._create_interview_engine = lambda: (engine, llm)
    started = await in_process.handle(
        {"initial_context": "Build a tool", "propose_opening_interpretations": True}
    )
    sid = started.value.meta["session_id"]
    state = (await engine.load_state(sid)).value
    display = state.proposed_opening.render()
    before = engine._state_file_path(sid).read_bytes()
    plugin = InterviewHandler(
        interview_engine=engine,
        data_dir=tmp_path,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    with patch(
        "ouroboros.mcp.tools.authoring_handlers.dispatch_plugin_terminal", new=AsyncMock()
    ) as dispatch:
        result = await plugin.handle({"session_id": sid, "answer": "yes", "last_question": display})
        assert result.is_err
        dispatch.assert_not_awaited()
    assert engine._state_file_path(sid).read_bytes() == before


@pytest.mark.asyncio
async def test_pm_answer_recorder_rejects_opening_without_mutation(tmp_path):
    from ouroboros.bigbang.pm_interview import PMInterviewEngine
    from ouroboros.mcp.tools.pm_batch import record_turn_answers
    from ouroboros.mcp.tools.pm_handler import PMInterviewHandler

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    started = await handler.handle(
        {"initial_context": "Build a tool", "propose_opening_interpretations": True}
    )
    state = (await engine.load_state(started.value.meta["session_id"])).value
    before = state.model_dump_json()
    result = await record_turn_answers(None, state, [(state.proposed_opening.render(), "yes")])
    assert result.is_err
    assert state.model_dump_json() == before
    sid = state.interview_id
    disk_before = engine._state_file_path(sid).read_bytes()
    pm_engine = PMInterviewEngine.create(llm_adapter=llm, model="synthetic", state_dir=tmp_path)
    pm_handler = PMInterviewHandler(pm_engine=pm_engine, data_dir=tmp_path)
    result = await pm_handler._handle_answer(
        pm_engine, sid, "yes", str(tmp_path), last_question=state.proposed_opening.render()
    )
    assert result.is_err
    plugin = PMInterviewHandler(
        data_dir=tmp_path, agent_runtime_backend="opencode", opencode_mode="plugin"
    )
    with patch(
        "ouroboros.mcp.tools.pm_handler.dispatch_plugin_terminal", new=AsyncMock()
    ) as dispatch:
        result = await plugin.handle(
            {
                "action": "resume",
                "session_id": sid,
                "answer": "yes",
                "last_question": state.proposed_opening.render(),
            }
        )
        assert result.is_err
        dispatch.assert_not_awaited()
    assert engine._state_file_path(sid).read_bytes() == disk_before


@pytest.mark.asyncio
async def test_sdk_explicit_user_question_reopens_without_model_echo(tmp_path):
    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    llm = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content="What else?")),
                Result.ok(SimpleNamespace(content="Any further details?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="opening-user-reopen-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        schema = (await client.list_tools()).tools[0].input_schema["properties"]
        assert "opening_user_followup_question" in schema
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        sid = started.meta["session_id"]
        assert not (
            await client.call_tool(
                "ouroboros_interview", {"session_id": sid, "answer": "A personal tool"}
            )
        ).is_error
        state = (await engine.load_state(sid)).value
        state.rounds.pop()
        assert (await engine.save_state(state)).is_ok
        before = engine._state_file_path(sid).read_bytes()
        for text in (state.proposed_opening.render(), "SENTINEL_ALPHA", "User asked for a tool"):
            result = await client.call_tool(
                "ouroboros_interview",
                {"session_id": sid, "answer": "yes", "opening_user_followup_question": text},
            )
            assert result.is_error
            assert engine._state_file_path(sid).read_bytes() == before
        conflict = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "yes",
                "last_question": "Old question",
                "opening_user_followup_question": "Can we add export?",
            },
        )
        assert conflict.is_error
        assert engine._state_file_path(sid).read_bytes() == before
        for extra in (
            {"initial_context": "Other project"},
            {"structured_first_question": True},
            {"answer": "done"},
        ):
            invalid = await client.call_tool(
                "ouroboros_interview",
                {
                    "session_id": sid,
                    "answer": "yes",
                    "opening_user_followup_question": "Can we add export?",
                    **extra,
                },
            )
            assert invalid.is_error
            assert engine._state_file_path(sid).read_bytes() == before
        result = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "answer": "yes",
                "opening_user_followup_question": "Can we add export?",
            },
        )
        assert not result.is_error
    state = (await engine.load_state(sid)).value
    assert state.rounds[1].question == "Can we add export?"
    context = SeedGenerator._build_interview_context(object.__new__(SeedGenerator), state)
    assert "Can we add export?" in context
    assert "SENTINEL_ALPHA" not in context and "SENTINEL_BETA" not in context


@pytest.mark.asyncio
async def test_valid_answer_and_turn_context_share_persisted_state(tmp_path):
    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="opening-context-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        sid = started.meta["session_id"]
        with patch.object(
            engine, "ask_next_question", new=AsyncMock(return_value=Result.ok("More?"))
        ):
            result = await client.call_tool(
                "ouroboros_interview",
                {
                    "session_id": sid,
                    "answer": "A personal tool",
                    "confused_terms": ["ontology"],
                    "references": [
                        {"reference_id": "example", "label": "Example", "origin": "user_text"}
                    ],
                },
            )
        assert not result.is_error
    state = (await engine.load_state(sid)).value
    assert state.rounds[0].user_response == "A personal tool"
    assert "ontology" in state.pending_confused_terms
    assert [cue.reference_id for cue in state.reference_cues] == ["example"]


@pytest.mark.asyncio
async def test_sdk_reopen_rejects_rendered_opening_with_short_fields(tmp_path):
    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    payload = json.dumps(
        {
            "interpretations": [
                {"interpretation": "A", "evidence": "B", "uncertainty": "C"},
                {"interpretation": "D", "evidence": "E", "uncertainty": "F"},
            ]
        }
    )
    llm = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=payload)),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="short-opening-echo-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        sid = started.meta["session_id"]
        assert not (
            await client.call_tool(
                "ouroboros_interview", {"session_id": sid, "answer": "A personal tool"}
            )
        ).is_error
        state = (await engine.load_state(sid)).value
        state.rounds.pop()
        assert (await engine.save_state(state)).is_ok
        before = engine._state_file_path(sid).read_bytes()
        display = state.proposed_opening.render()
        result = await client.call_tool(
            "ouroboros_interview",
            {"session_id": sid, "answer": "yes", "opening_user_followup_question": display},
        )
        assert result.is_error
        assert engine._state_file_path(sid).read_bytes() == before
    state = (await engine.load_state(sid)).value
    assert display not in SeedGenerator._build_interview_context(
        object.__new__(SeedGenerator), state
    )
