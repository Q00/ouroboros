"""Real SDK and Seed path for local, non-authoritative material references."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import Client
from mcp.server import MCPServer
import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.seed import Seed
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import render_seed_contract_for_execution
from ouroboros.core.types import Result
from ouroboros.interview_adapters.material_evidence import confirmation_prompt, revision
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from tests.unit.bigbang.test_requirement_distillation import (
    _low_ambiguity,
    _reference_state,
    _requirements,
)
from tests.unit.mcp.tools.test_interview_batch_prototype import _proposal


@pytest.mark.asyncio
async def test_sdk_local_material_reference_to_seed_and_resolution(tmp_path):
    quote = "SYNTHETIC_PRIVATE_SOURCE_123"
    assumption = "SYNTHETIC_UNCONFIRMED_GUESS"
    draft = json.dumps({"source_quotes": [quote], "inferred_assumptions": [assumption]})
    llm = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content=draft)),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    generator = SeedGenerator(llm_adapter=llm, model="synthetic", output_dir=tmp_path / "seeds")
    seed_handler = GenerateSeedHandler(
        interview_engine=engine, seed_generator=generator, llm_adapter=llm, data_dir=tmp_path
    )
    seed_handler._load_stored_ambiguity_score = lambda _state: _low_ambiguity()
    server = MCPServerAdapter(name="material-evidence-test")
    server.register_tool(handler)
    server.register_tool(seed_handler)
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
        material = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_independence_confirmed": True,
                "batch_answers": [
                    {"question_id": item["question_id"], "answer": "A user decision"}
                    for item in started.meta["questions"]
                ],
                "inline_material_text": f"Before {quote} after",
                "inline_material_consent": True,
            },
        )
        assert not material.is_error
        state = (await engine.load_state(sid)).value
        source = state.inline_material
        rev, prompt = (
            revision(source),
            confirmation_prompt(source, "source_quote", 0, "include", session_id=sid),
        )
        assert any(rev in item.text and prompt in item.text for item in material.content)
        decision = {
            "session_id": sid,
            "material_evidence_action": "include",
            "material_evidence_kind": "source_quote",
            "material_evidence_index": 0,
            "material_evidence_revision": rev,
            "material_evidence_prompt": prompt,
            "material_evidence_response": "INCLUDE",
        }
        other_sid = "interview_abcdef0123456789"
        other_state = (
            await engine.start_interview("Build another tool", interview_id=other_sid)
        ).value
        other_state.inline_material = source
        assert (await engine.save_state(other_state)).is_ok
        assert revision((await engine.load_state(other_sid)).value.inline_material) == rev
        cross_replay = await client.call_tool(
            "ouroboros_interview", {**decision, "session_id": other_sid}
        )
        assert cross_replay.is_error
        assert not (await engine.load_state(other_sid)).value.inline_material.evidence_decisions
        invalid = await client.call_tool(
            "ouroboros_interview", {**decision, "material_evidence_response": "YES"}
        )
        assert invalid.is_error
        accepted = await client.call_tool("ouroboros_interview", decision)
        assert not accepted.is_error
        repeated = await client.call_tool("ouroboros_interview", decision)
        assert repeated.meta["material_evidence_id"] == accepted.meta["material_evidence_id"]
        state = (await engine.load_state(sid)).value
        assert len(state.inline_material.evidence_decisions) == 1
        assert quote not in generator._build_interview_context(state)
        assert quote not in object.__new__(AmbiguityScorer)._build_interview_context(state)
        with patch.object(
            generator,
            "_extract_requirements",
            new=AsyncMock(return_value=Result.ok(_requirements())),
        ):
            result = await generator.generate(state, _low_ambiguity())
            sdk_seed = await client.call_tool("ouroboros_generate_seed", {"session_id": sid})
        assert result.is_ok
        assert not sdk_seed.is_error
        assert "material_evidence_refs" in sdk_seed.content[0].text
        assert quote not in sdk_seed.content[0].text and assumption not in sdk_seed.content[0].text
        seed = result.value
        assert seed.metadata.interview_id == sid
        serialized = seed.to_dict()
        assert len(serialized["material_evidence_refs"]) == 1
        assert quote not in str(serialized) and assumption not in str(serialized)
        assert quote not in render_seed_contract_for_execution(SeedContract.from_seed(seed))
        assert Seed.from_dict(serialized).material_evidence_refs == seed.material_evidence_refs
        from pydantic import ValidationError as PydanticValidationError

        missing_session = {
            **serialized,
            "metadata": {**serialized["metadata"], "interview_id": None},
        }
        with pytest.raises(PydanticValidationError):
            Seed.from_dict(missing_session)
        duplicated = {
            **serialized,
            "material_evidence_refs": serialized["material_evidence_refs"] * 2,
        }
        with pytest.raises(PydanticValidationError):
            Seed.from_dict(duplicated)
        reference = _reference_state(
            confirmation="For the Linear-like reference, keyboard-first navigation is required."
        )
        reference.inline_material = state.inline_material
        reference.interview_id = sid
        ref_result = await generator.generate(reference, _low_ambiguity())
        assert (
            ref_result.is_ok
            and ref_result.value.material_evidence_refs == seed.material_evidence_refs
        )
        assert ref_result.value.metadata.interview_id == sid
        from ouroboros.evolution.reflect import ReflectOutput

        evolved = generator.generate_from_reflect(seed, ReflectOutput(refined_goal=seed.goal))
        assert evolved.is_ok
        assert evolved.value.material_evidence_refs == seed.material_evidence_refs
        assert evolved.value.metadata.interview_id == sid
        ref = serialized["material_evidence_refs"][0]
        query = {
            "session_id": sid,
            "material_evidence_action": "resolve",
            "material_evidence_source_id": ref["source_id"],
            "material_evidence_id": ref["evidence_id"],
        }
        challenge = await client.call_tool("ouroboros_interview", query)
        assert challenge.meta["material_evidence_status"] == "confirmation_required"
        assert quote not in str(challenge)
        shown = await client.call_tool(
            "ouroboros_interview",
            {
                **query,
                "material_evidence_prompt": challenge.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert shown.meta["material_evidence_status"] == "resolved"
        assert quote in shown.content[0].text and assumption not in shown.content[0].text
        assert "not confirmation of truth or a requirement" in shown.content[0].text

    # Plugin Seed production must not silently discard active local references.
    plugin_server = MCPServerAdapter(name="material-evidence-plugin-test")
    plugin_server.register_tool(
        GenerateSeedHandler(
            data_dir=tmp_path,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
    )
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await plugin_server.serve(transport="stdio")
    async with Client(plugin_server._mcp_server, mode="auto") as client:
        blocked = await client.call_tool(
            "ouroboros_generate_seed", {"session_id": sid, "ambiguity_score": 0.1}
        )
        assert blocked.is_error and quote not in str(blocked)

    # A fresh engine, handler and SDK server resolve the persisted reference.
    fresh_engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    fresh_handler = InterviewHandler(
        interview_engine=fresh_engine, llm_adapter=llm, data_dir=tmp_path
    )
    fresh_handler._create_interview_engine = lambda: (fresh_engine, llm)
    fresh_server = MCPServerAdapter(name="material-evidence-restart-test")
    fresh_server.register_tool(fresh_handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await fresh_server.serve(transport="stdio")
    async with Client(fresh_server._mcp_server, mode="auto") as client:
        challenge = await client.call_tool("ouroboros_interview", query)
        shown = await client.call_tool(
            "ouroboros_interview",
            {
                **query,
                "material_evidence_prompt": challenge.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert (
            shown.meta["material_evidence_status"] == "resolved" and quote in shown.content[0].text
        )
        other = {**query, "session_id": other_sid}
        other_prompt = await client.call_tool("ouroboros_interview", other)
        unknown = await client.call_tool(
            "ouroboros_interview",
            {
                **other,
                "material_evidence_prompt": other_prompt.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert unknown.meta["material_evidence_status"] == "unknown" and quote not in str(unknown)
        missing = {**query, "session_id": "interview_0000000000000000"}
        missing_prompt = await client.call_tool("ouroboros_interview", missing)
        missing_result = await client.call_tool(
            "ouroboros_interview",
            {
                **missing,
                "material_evidence_prompt": missing_prompt.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert missing_result.meta["material_evidence_status"] == "unknown" and quote not in str(
            missing_result
        )
        wrong_source = {**query, "material_evidence_source_id": "0" * 32}
        wrong_prompt = await client.call_tool("ouroboros_interview", wrong_source)
        wrong_result = await client.call_tool(
            "ouroboros_interview",
            {
                **wrong_source,
                "material_evidence_prompt": wrong_prompt.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert wrong_result.meta["material_evidence_status"] == "unknown" and quote not in str(
            wrong_result
        )
        altered = (await fresh_engine.load_state(sid)).value
        altered.inline_material = altered.inline_material.model_copy(
            update={"text": altered.inline_material.text + " modified"}
        )
        assert (await fresh_engine.save_state(altered)).is_ok
        unknown = await client.call_tool(
            "ouroboros_interview",
            {
                **query,
                "material_evidence_prompt": challenge.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert unknown.meta["material_evidence_status"] == "unknown" and quote not in str(unknown)
        restored = (await fresh_engine.load_state(sid)).value
        restored.inline_material = restored.inline_material.model_copy(
            update={"text": f"Before {quote} after"}
        )
        assert (await fresh_engine.save_state(restored)).is_ok
        restored = (await fresh_engine.load_state(sid)).value
        removal = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "material_evidence_action": "remove",
                "material_evidence_kind": "source_quote",
                "material_evidence_index": 0,
                "material_evidence_revision": revision(restored.inline_material),
                "material_evidence_prompt": confirmation_prompt(
                    restored.inline_material, "source_quote", 0, "remove", session_id=sid
                ),
                "material_evidence_response": "REMOVE",
            },
        )
        assert not removal.is_error
        removed = await client.call_tool(
            "ouroboros_interview",
            {
                **query,
                "material_evidence_prompt": challenge.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert removed.meta["material_evidence_status"] == "unknown" and quote not in str(removed)
        after_removal = (await fresh_engine.load_state(sid)).value
        with patch.object(
            generator,
            "_extract_requirements",
            new=AsyncMock(return_value=Result.ok(_requirements())),
        ):
            regenerated = await generator.generate(after_removal, _low_ambiguity())
        assert regenerated.is_ok
        assert regenerated.value.material_evidence_refs == ()


@pytest.mark.asyncio
async def test_generated_timestamp_session_concurrent_decisions_use_cas(tmp_path):
    import asyncio

    from ouroboros.interview_adapters.inline_materials import InlineMaterial, MaterialDraft

    llm = SimpleNamespace(complete=AsyncMock())
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    state = (await engine.start_interview("Build a tool")).value
    sid = state.interview_id
    state.inline_material = InlineMaterial(
        text="First synthetic quote. Second synthetic quote.",
        status="ready",
        draft=MaterialDraft(
            source_quotes=("First synthetic quote", "Second synthetic quote"),
            inferred_assumptions=(),
        ),
    )
    assert (await engine.save_state(state)).is_ok
    state = (await engine.load_state(sid)).value
    rev = revision(state.inline_material)
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="material-evidence-cas-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    entered = 0
    both = asyncio.Event()
    original_save = engine.save_state

    async def synchronized_save(value):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return await original_save(value)

    async with Client(server._mcp_server, mode="auto") as client:
        decisions = [
            {
                "session_id": sid,
                "material_evidence_action": "include",
                "material_evidence_kind": "source_quote",
                "material_evidence_index": index,
                "material_evidence_revision": rev,
                "material_evidence_prompt": confirmation_prompt(
                    state.inline_material, "source_quote", index, "include", session_id=sid
                ),
                "material_evidence_response": "INCLUDE",
            }
            for index in (0, 1)
        ]
        with patch.object(engine, "save_state", side_effect=synchronized_save):
            outcomes = await asyncio.gather(
                *(client.call_tool("ouroboros_interview", value) for value in decisions)
            )
        assert sum(not outcome.is_error for outcome in outcomes) == 1
        saved = (await engine.load_state(sid)).value
        assert len(saved.inline_material.evidence_decisions) == 1
        successful = next(outcome for outcome in outcomes if not outcome.is_error)
        query = {
            "session_id": sid,
            "material_evidence_action": "resolve",
            "material_evidence_source_id": successful.meta["material_evidence_source_id"],
            "material_evidence_id": successful.meta["material_evidence_id"],
        }
        prompt = await client.call_tool("ouroboros_interview", query)
        result = await client.call_tool(
            "ouroboros_interview",
            {
                **query,
                "material_evidence_prompt": prompt.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert result.meta["material_evidence_status"] == "resolved"


@pytest.mark.parametrize("field", ["inline_material_text", "material_evidence_prompt"])
def test_material_prose_exemption_is_limited_to_interview_root(field):
    from ouroboros.mcp.server.security import InputValidator

    value = "Reference example; subprocess.run()"
    validator = InputValidator()
    assert validator.validate("ouroboros_interview", {field: value}).is_err
    assert validator.validate("other_tool", {field: value}).is_err
    assert validator.validate("ouroboros_interview", {"wrapper": {field: value}}).is_err


@pytest.mark.asyncio
async def test_legacy_model_assumption_is_only_an_unverified_local_reference(tmp_path):
    from ouroboros.bigbang.interview import InterviewRound
    from ouroboros.interview_adapters.inline_materials import InlineMaterial, MaterialDraft

    marker = "SYNTHETIC_LEGACY_ASSUMPTION; subprocess.run()"
    llm = SimpleNamespace(complete=AsyncMock())
    engine = InterviewEngine(llm_adapter=llm, state_dir=tmp_path, model="synthetic")
    state = (await engine.start_interview("Build a tool")).value
    sid = state.interview_id
    state.rounds.append(
        InterviewRound(round_number=1, question="What is needed?", user_response="A tool")
    )
    state.inline_material = InlineMaterial(
        text="Synthetic legacy source",
        status="ready",
        draft=MaterialDraft(
            source_quotes=("Synthetic legacy source",), inferred_assumptions=(marker,)
        ),
    )
    assert (await engine.save_state(state)).is_ok
    state = (await engine.load_state(sid)).value
    handler = InterviewHandler(interview_engine=engine, llm_adapter=llm, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, llm)
    server = MCPServerAdapter(name="legacy-material-evidence")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        accepted = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "material_evidence_action": "include",
                "material_evidence_kind": "model_assumption",
                "material_evidence_index": 0,
                "material_evidence_revision": revision(state.inline_material),
                "material_evidence_prompt": confirmation_prompt(
                    state.inline_material, "model_assumption", 0, "include", session_id=sid
                ),
                "material_evidence_response": "INCLUDE",
            },
        )
        assert not accepted.is_error
        saved = (await engine.load_state(sid)).value
        generator = SeedGenerator(llm_adapter=llm, model="synthetic", output_dir=tmp_path / "seeds")
        with patch.object(
            generator,
            "_extract_requirements",
            new=AsyncMock(return_value=Result.ok(_requirements())),
        ):
            result = await generator.generate(saved, _low_ambiguity())
        assert result.is_ok
        assert [ref.kind for ref in result.value.material_evidence_refs] == ["model_assumption"]
        assert marker not in str(result.value.to_dict())
        assert marker not in render_seed_contract_for_execution(
            SeedContract.from_seed(result.value)
        )
        ref = result.value.material_evidence_refs[0]
        query = {
            "session_id": sid,
            "material_evidence_action": "resolve",
            "material_evidence_source_id": ref.source_id,
            "material_evidence_id": ref.evidence_id,
        }
        prompt = await client.call_tool("ouroboros_interview", query)
        shown = await client.call_tool(
            "ouroboros_interview",
            {
                **query,
                "material_evidence_prompt": prompt.meta["material_evidence_prompt"],
                "material_evidence_response": "SHOW",
            },
        )
        assert shown.meta["material_evidence_status"] == "resolved"
        assert "Unverified model assumption" in shown.content[0].text
        assert marker in shown.content[0].text
