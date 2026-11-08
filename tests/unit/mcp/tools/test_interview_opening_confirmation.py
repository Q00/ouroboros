"""Item decisions are explicit, revision-bound, and persist across SDK calls."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import Client
from mcp.server import MCPServer
import pytest

from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from tests.unit.bigbang.test_requirement_distillation import (
    _low_ambiguity,
    _reference_state,
    _requirements,
)
from tests.unit.mcp.tools.test_interview_opening_interpretations import _proposal


@pytest.mark.asyncio
async def test_sdk_item_confirmation_retry_and_neither(tmp_path):
    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="opening-confirmation-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        started = await client.call_tool(
            "ouroboros_interview",
            {"initial_context": "Build a tool", "propose_opening_interpretations": True},
        )
        assert not started.is_error
        meta = started.meta
        sid = meta["session_id"]
        item = meta["opening_interpretations"]["interpretations"][0]["item_id"]
        call = {
            "session_id": sid,
            "opening_revision": meta["opening_revision"],
            "opening_confirm_item_id": item,
            "opening_user_confirmed": True,
            "opening_confirmation_text": "YES",
            "opening_confirmation_prompt": meta["opening_confirmation_prompts"][item],
        }
        rejected = await client.call_tool(
            "ouroboros_interview", {**call, "opening_confirmation_text": "No, not that"}
        )
        assert rejected.is_error
        assert not (await engine.load_state(sid)).value.opening_decisions
        original_fingerprint = (await engine.load_state(sid)).value.requirement_input_fingerprint()
        accepted = await client.call_tool("ouroboros_interview", call)
        assert not accepted.is_error
        assert accepted.meta["opening_confirmed_item_ids"] == [item]
        retry = await client.call_tool("ouroboros_interview", call)
        assert not retry.is_error
        assert retry.meta["opening_event_id"] == accepted.meta["opening_event_id"]
        state = (await engine.load_state(sid)).value
        assert len(state.opening_decisions) == 1
        assert state.requirement_input_fingerprint() != original_fingerprint
        generator = SeedGenerator(llm_adapter=llm, model="synthetic", output_dir=tmp_path / "seeds")
        context = generator._build_interview_context(state)
        assert "SENTINEL_ALPHA" in context
        assert "SENTINEL_BETA" not in context
        with patch.object(
            generator,
            "_extract_requirements",
            new=AsyncMock(return_value=Result.ok(_requirements())),
        ):
            regular = await generator.generate(state, _low_ambiguity())
        assert regular.is_ok
        assert any(
            entry.question == call["opening_confirmation_prompt"] and entry.answer == "YES"
            for entry in regular.value.requester_answers
        )
        assert "SENTINEL_BETA" not in str(regular.value.requester_answers)
        pm = PMInterviewEngine.create(llm_adapter=llm, model="synthetic", state_dir=tmp_path)
        pm_result = await pm.generate_pm_seed(state)
        assert pm_result.is_err
        assert "empty interview" in str(pm_result.error)
        reference = _reference_state(
            confirmation="For the Linear-like reference, keyboard-first navigation is required."
        )
        reference.proposed_opening = state.proposed_opening
        reference.opening_decisions = state.opening_decisions
        seed_result = await generator.generate(reference, _low_ambiguity())
        assert seed_result.is_ok
        answers = seed_result.value.requester_answers
        assert any(
            entry.question == call["opening_confirmation_prompt"] and entry.answer == "YES"
            for entry in answers
        )
        assert "SENTINEL_BETA" not in str(answers)
        neither = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "opening_revision": accepted.meta["opening_revision"],
                "opening_neither": True,
                "opening_confirmation_text": "NEITHER",
                "opening_confirmation_prompt": accepted.meta["opening_neither_prompt"],
            },
        )
        assert not neither.is_error
        assert neither.meta["opening_confirmed_item_ids"] == []
        stale = await client.call_tool("ouroboros_interview", call)
        assert stale.is_error
        state = (await engine.load_state(sid)).value
        assert len(state.opening_decisions) == 2
        assert "SENTINEL_ALPHA" not in generator._build_interview_context(state)

    path = engine._state_file_path(sid)
    corrupt = json.loads(path.read_text())
    corrupt["opening_decisions"][0]["user_response"] = "NO"
    path.write_text(json.dumps(corrupt))
    assert (await engine.load_state(sid)).is_err


@pytest.mark.asyncio
async def test_competing_decisions_are_cas_protected(tmp_path):
    import asyncio

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    started = await handler.handle(
        {"initial_context": "Build a tool", "propose_opening_interpretations": True}
    )
    meta = started.value.meta
    items = meta["opening_interpretations"]["interpretations"]
    original_save = engine.save_state
    both_loaded = asyncio.Event()
    calls = 0

    async def synchronized_save(state):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_loaded.set()
        await both_loaded.wait()
        return await original_save(state)

    def arguments(item):
        item_id = item["item_id"]
        return {
            "session_id": meta["session_id"],
            "opening_revision": meta["opening_revision"],
            "opening_confirm_item_id": item_id,
            "opening_user_confirmed": True,
            "opening_confirmation_text": "YES",
            "opening_confirmation_prompt": meta["opening_confirmation_prompts"][item_id],
        }

    with patch.object(engine, "save_state", side_effect=synchronized_save):
        results = await asyncio.wait_for(
            asyncio.gather(*(handler.handle(arguments(item)) for item in items)), timeout=10
        )
    assert sum(result.is_ok for result in results) == 1
    state = (await engine.load_state(meta["session_id"])).value
    assert len(state.opening_decisions) == 1
