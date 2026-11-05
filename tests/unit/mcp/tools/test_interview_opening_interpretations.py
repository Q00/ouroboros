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
