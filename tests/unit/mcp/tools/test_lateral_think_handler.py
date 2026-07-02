"""Regression tests for :class:`LateralThinkHandler`.

Verifies the multi-persona fan-out path honours the shared
``should_dispatch_via_plugin`` contract:

* Plugin-gated (OpenCode runtime + ``opencode_mode="plugin"`` explicitly) →
  emits a ``_subagents`` envelope for the bridge plugin to consume.
* Non-plugin (``opencode_mode="subprocess"``, unset/None, or non-OpenCode
  runtime) → falls back to inline concatenation of persona prompts so the
  caller gets a useful text response instead of a dead envelope.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler
from ouroboros.mcp.tools.subagent import (
    continue_interview_after_lateral_persona_synthesis,
    lateral_persona_panel_metadata_from_capability_definitions,
    lateral_review_response_to_interview_orchestration_entries,
    lateral_review_response_to_interview_orchestration_metadata,
    synthesize_lateral_persona_panel_when_complete,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


@pytest.mark.asyncio
async def test_multi_persona_plugin_mode_emits_subagents_envelope() -> None:
    """Plugin mode → the ``_subagents`` envelope is produced for the bridge."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    # Envelope is present on meta and as JSON text.
    assert "_subagents" in payload.meta
    subagents = payload.meta["_subagents"]
    assert len(subagents) == 2
    assert [subagent["agent"] for subagent in subagents] == ["hacker", "contrarian"]
    assert [subagent["title"] for subagent in subagents] == [
        "Lateral (hacker)",
        "Lateral (contrarian)",
    ]
    assert [subagent["context"]["persona"] for subagent in subagents] == [
        "hacker",
        "contrarian",
    ]
    text = payload.content[0].text
    decoded = json.loads(text)
    assert "_subagents" in decoded
    assert [
        (subagent["agent"], subagent["context"]["persona"]) for subagent in decoded["_subagents"]
    ] == [("hacker", "hacker"), ("contrarian", "contrarian")]


@pytest.mark.asyncio
async def test_plugin_lateral_response_converts_to_interview_persona_panel_entries() -> None:
    """Plugin lateral-review responses become per-persona interview metadata."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "interview crossed a milestone",
            "current_approach": "continue with the next question",
            "personas": ["researcher", "contrarian", "simplifier"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    entries = lateral_review_response_to_interview_orchestration_entries(
        payload,
        session_id="sess-panel",
        runtime_supports_parallel_subagents=True,
    )

    assert [entry["persona_id"] for entry in entries] == [
        "researcher",
        "contrarian",
        "simplifier",
    ]
    assert {entry["panel_id"] for entry in entries} == {"lateral_persona_panel.v1"}
    assert {entry["mcp_tool"] for entry in entries} == {"ouroboros_lateral_think"}
    assert {entry["dispatch_mode"] for entry in entries} == {"plugin"}
    assert {entry["response_payload_source"] for entry in entries} == {"meta"}
    assert {entry["execution_mode"] for entry in entries} == {"parallel_subagent_panel"}
    assert all(entry["requires_prose_parsing"] is False for entry in entries)
    assert all(entry["sequential_fallback_used"] is False for entry in entries)
    assert [entry["execution_order"] for entry in entries] == [1, 2, 3]
    for entry in entries:
        assert entry["session_id"] == "sess-panel"
        assert entry["parallel_group"] == "sess-panel:lateral_persona_panel.v1"
        assert entry["prompt"]
        assert entry["context"]["persona"] == entry["persona_id"]
        assert entry["persona_role"]


@pytest.mark.asyncio
async def test_parallel_lateral_synthesis_waits_for_all_persona_outputs() -> None:
    """Parallel persona collection gates synthesis until every output is present."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "interview crossed a milestone",
            "current_approach": "continue only after lateral synthesis",
            "personas": ["researcher", "contrarian", "simplifier"],
        }
    )

    assert result.is_ok, result
    entries = lateral_review_response_to_interview_orchestration_entries(
        result.unwrap(),
        session_id="sess-aggregate",
        runtime_supports_parallel_subagents=True,
    )
    assert [entry["persona_id"] for entry in entries] == [
        "researcher",
        "contrarian",
        "simplifier",
    ]

    synthesis_calls = []

    def synthesize(aggregated_outputs: list[dict]) -> dict:
        synthesis_calls.append(aggregated_outputs)
        return {
            "persona_ids": [item["persona_id"] for item in aggregated_outputs],
            "combined": "\n".join(str(item["output"]) for item in aggregated_outputs),
        }

    partial = synthesize_lateral_persona_panel_when_complete(
        entries,
        {
            "contrarian": "challenge the assumption",
            "researcher": "verify the current implementation",
        },
        synthesize,
    )

    assert partial["ready_for_synthesis"] is False
    assert partial["missing_personas"] == ["simplifier"]
    assert [item["persona_id"] for item in partial["aggregated_outputs"]] == [
        "researcher",
        "contrarian",
    ]
    assert partial["synthesis"] is None
    assert synthesis_calls == []

    complete = synthesize_lateral_persona_panel_when_complete(
        entries,
        {
            "contrarian": "challenge the assumption",
            "simplifier": "reduce the next question",
            "researcher": "verify the current implementation",
        },
        synthesize,
    )

    assert complete["ready_for_synthesis"] is True
    assert complete["missing_personas"] == []
    assert complete["synthesis"] == {
        "persona_ids": ["researcher", "contrarian", "simplifier"],
        "combined": (
            "verify the current implementation\nchallenge the assumption\nreduce the next question"
        ),
    }
    assert len(synthesis_calls) == 1
    assert [item["persona_id"] for item in synthesis_calls[0]] == [
        "researcher",
        "contrarian",
        "simplifier",
    ]


@pytest.mark.asyncio
async def test_parallel_lateral_synthesis_precedes_interview_continuation() -> None:
    """Parallel persona results are synthesized before the interview turn resumes."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "interview crossed a milestone",
            "current_approach": "continue only after synthesized lateral review",
            "personas": ["researcher", "contrarian", "simplifier"],
        }
    )

    assert result.is_ok, result
    entries = lateral_review_response_to_interview_orchestration_entries(
        result.unwrap(),
        session_id="sess-continuation-order",
        runtime_supports_parallel_subagents=True,
    )

    timeline: list[str] = []

    def synthesize(aggregated_outputs: list[dict]) -> dict:
        timeline.append("synthesis")
        return {
            "persona_ids": [item["persona_id"] for item in aggregated_outputs],
            "summary": "lateral review is ready",
        }

    def continue_interview(synthesis: dict) -> dict:
        assert timeline == ["synthesis"]
        assert synthesis["persona_ids"] == ["researcher", "contrarian", "simplifier"]
        timeline.append("interview_continuation")
        return {
            "next_question": "What risk should we clarify next?",
            "lateral_synthesis": synthesis,
        }

    partial = continue_interview_after_lateral_persona_synthesis(
        entries,
        {
            "researcher": "verify current implementation",
            "contrarian": "challenge milestone assumption",
        },
        synthesize,
        continue_interview,
    )

    assert partial["ready_for_synthesis"] is False
    assert partial["continued_interview"] is False
    assert partial["interview_continuation"] is None
    assert timeline == []

    complete = continue_interview_after_lateral_persona_synthesis(
        entries,
        {
            "simplifier": "reduce follow-up scope",
            "contrarian": "challenge milestone assumption",
            "researcher": "verify current implementation",
        },
        synthesize,
        continue_interview,
    )

    assert complete["ready_for_synthesis"] is True
    assert complete["continued_interview"] is True
    assert complete["interview_continuation"] == {
        "next_question": "What risk should we clarify next?",
        "lateral_synthesis": complete["synthesis"],
    }
    assert timeline == ["synthesis", "interview_continuation"]


@pytest.mark.asyncio
async def test_interview_orchestration_reader_uses_capability_panel_metadata() -> None:
    """Panel identity, roles, and fallback mode come from capability metadata."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "interview crossed a milestone",
            "current_approach": "continue with the next question",
            "personas": ["researcher", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    capability_panel = lateral_persona_panel_metadata_from_capability_definitions()

    entries = lateral_review_response_to_interview_orchestration_entries(
        payload,
        session_id="sess-capability",
        runtime_supports_parallel_subagents=True,
        lateral_panel_metadata=capability_panel,
    )

    assert entries
    assert {entry["panel_id"] for entry in entries} == {capability_panel["panel_id"]}
    assert {entry["mcp_tool"] for entry in entries} == {capability_panel["mcp_tool"]}
    assert {entry["parallel_group"] for entry in entries} == {
        f"sess-capability:{capability_panel['panel_id']}"
    }
    roles_by_id = {
        persona["persona_id"]: persona["role"] for persona in capability_panel["personas"]
    }
    for entry in entries:
        assert entry["persona_role"] == roles_by_id[entry["persona_id"]]
        assert (
            entry["requires_prose_parsing"]
            is capability_panel["response_payload_refs"]["requires_prose_parsing"]
        )


@pytest.mark.asyncio
async def test_interview_orchestration_reader_honors_custom_capability_metadata() -> None:
    """Regression guard against hard-coded lateral panel metadata in the reader."""
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs a lightweight review",
            "current_approach": "ask the next Socratic question",
            "personas": ["researcher"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    custom_panel = {
        "panel_id": "custom_lateral_panel.v9",
        "mcp_tool": "custom_lateral_tool",
        "sequential_fallback": {
            "supported": True,
            "mode": "custom_sequential_mode",
            "trigger": "test_runtime_without_parallel_subagents",
        },
        "personas": [
            {"persona_id": "researcher", "role": "Custom research role"},
        ],
        "response_payload_refs": {"requires_prose_parsing": True},
    }

    metadata = lateral_review_response_to_interview_orchestration_metadata(
        payload,
        session_id="sess-custom",
        runtime_supports_parallel_subagents=False,
        lateral_panel_metadata=custom_panel,
    )

    panel = metadata["lateral_panel"]
    entries = panel["entries"]
    assert panel["panel_id"] == "custom_lateral_panel.v9"
    assert panel["mcp_tool"] == "custom_lateral_tool"
    assert panel["execution_mode"] == "custom_sequential_mode"
    assert panel["requires_prose_parsing"] is True
    assert entries[0]["panel_id"] == "custom_lateral_panel.v9"
    assert entries[0]["mcp_tool"] == "custom_lateral_tool"
    assert entries[0]["persona_role"] == "Custom research role"
    assert entries[0]["execution_mode"] == "custom_sequential_mode"
    assert entries[0]["parallel_group"] == "sess-custom:custom_lateral_panel.v9"


@pytest.mark.asyncio
async def test_inline_lateral_content_converts_to_sequential_persona_panel_entries() -> None:
    """Content-only inline fallback still yields structured panel entries."""
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs a lightweight review",
            "current_approach": "ask the next Socratic question",
            "personas": ["researcher", "simplifier"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    # Simulate an MCP transport that preserved only visible content and dropped meta.
    content_only = MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text=payload.text_content),),
        is_error=False,
        meta={},
    )

    metadata = lateral_review_response_to_interview_orchestration_metadata(
        content_only,
        session_id="sess-inline",
        runtime_supports_parallel_subagents=False,
    )

    panel = metadata["lateral_panel"]
    entries = panel["entries"]
    assert panel["panel_id"] == "lateral_persona_panel.v1"
    assert panel["mcp_tool"] == "ouroboros_lateral_think"
    assert panel["entry_count"] == 2
    assert panel["execution_mode"] == "sequential_persona_payload_dispatch"
    assert panel["requires_prose_parsing"] is False
    assert [entry["persona_id"] for entry in entries] == ["researcher", "simplifier"]
    assert {entry["dispatch_mode"] for entry in entries} == {"sequential"}
    assert {entry["response_payload_source"] for entry in entries} == {"inline_content"}
    assert {entry["execution_mode"] for entry in entries} == {"sequential_persona_payload_dispatch"}
    assert all(entry["sequential_fallback_used"] is True for entry in entries)


@pytest.mark.asyncio
async def test_codex_runtime_emits_host_driven_spawn_directive() -> None:
    """Codex has a native subagent primitive but no passive bridge → host_driven.

    Regression guard for the real user config (``runtime=codex`` with
    ``opencode_mode=plugin``): codex must NOT be misrouted to the passive
    ``plugin`` envelope path; it must get an explicit host-driven spawn stamp.
    """
    handler = LateralThinkHandler(
        agent_runtime_backend="codex",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs a lightweight review",
            "current_approach": "ask the next Socratic question",
            "personas": ["researcher", "simplifier"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta["dispatch_mode"] == "host_driven"
    assert payload.meta["host_action"] == "spawn_subagents"
    assert payload.meta["result_correlation_key"] == "context.persona"
    assert payload.meta["persona_count"] == 2
    assert len(payload.meta["payloads"]) == 2
    # Visible deterministic cue for transports that drop ``meta``.
    assert "Host action — spawn subagents" in payload.text_content
    # The inline base64 dispatch block still rides in content for older consumers.
    assert "ouroboros-lateral-inline-dispatch-v1" in payload.text_content


@pytest.mark.asyncio
async def test_claude_code_runtime_emits_host_driven_spawn_directive() -> None:
    """Claude Code has Task/Agent fan-out but no passive bridge → host_driven."""
    handler = LateralThinkHandler(
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs a lightweight review",
            "current_approach": "ask the next Socratic question",
            "personas": ["researcher", "simplifier"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta["dispatch_mode"] == "host_driven"
    assert payload.meta["host_action"] == "spawn_subagents"
    assert payload.meta["result_correlation_key"] == "context.persona"
    assert len(payload.meta["payloads"]) == 2
    assert "Host action — spawn subagents" in payload.text_content


@pytest.mark.asyncio
async def test_sequential_lateral_consumer_receives_persona_payloads_in_order() -> None:
    """Sequential fallback consumers receive persona payloads in request order."""
    requested_personas = ["simplifier", "hacker", "researcher"]
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs ordered persona review",
            "current_approach": "dispatch sequentially when parallel panes are unavailable",
            "personas": requested_personas,
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    entries = lateral_review_response_to_interview_orchestration_entries(
        payload,
        session_id="sess-sequential-consumer",
        runtime_supports_parallel_subagents=False,
    )

    sequential_consumer_received = []
    for entry in entries:
        assert entry["execution_mode"] == "sequential_persona_payload_dispatch"
        assert entry["sequential_fallback_used"] is True
        sequential_consumer_received.append(
            {
                "execution_order": entry["execution_order"],
                "persona_id": entry["persona_id"],
                "agent": entry["agent"],
                "context_persona": entry["context"]["persona"],
                "prompt": entry["prompt"],
            }
        )

    assert [item["execution_order"] for item in sequential_consumer_received] == [1, 2, 3]
    assert [item["persona_id"] for item in sequential_consumer_received] == requested_personas
    assert [item["agent"] for item in sequential_consumer_received] == requested_personas
    assert [item["context_persona"] for item in sequential_consumer_received] == requested_personas
    for item in sequential_consumer_received:
        assert f"**{item['persona_id']}** persona" in item["prompt"]
        assert "Task for you (subagent)" in item["prompt"]


@pytest.mark.asyncio
async def test_sequential_lateral_synthesis_waits_for_configured_sequence_outputs() -> None:
    """Sequential fallback gates synthesis until every ordered persona output arrives."""
    requested_personas = ["simplifier", "hacker", "researcher"]
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs ordered persona synthesis",
            "current_approach": "continue only after sequential review completes",
            "personas": requested_personas,
        }
    )

    assert result.is_ok, result
    entries = lateral_review_response_to_interview_orchestration_entries(
        result.unwrap(),
        session_id="sess-sequential-aggregate",
        runtime_supports_parallel_subagents=False,
    )
    assert [entry["persona_id"] for entry in entries] == requested_personas
    assert {entry["execution_mode"] for entry in entries} == {"sequential_persona_payload_dispatch"}
    assert all(entry["sequential_fallback_used"] is True for entry in entries)

    synthesis_calls = []

    def synthesize(aggregated_outputs: list[dict]) -> dict:
        synthesis_calls.append(aggregated_outputs)
        return {
            "persona_ids": [item["persona_id"] for item in aggregated_outputs],
            "combined": "\n".join(str(item["output"]) for item in aggregated_outputs),
        }

    consumed_outputs: dict[str, str] = {}
    for index, persona_id in enumerate(requested_personas[:-1], start=1):
        consumed_outputs[persona_id] = f"{persona_id} output"
        partial = synthesize_lateral_persona_panel_when_complete(
            entries,
            consumed_outputs,
            synthesize,
        )

        assert partial["ready_for_synthesis"] is False
        assert partial["missing_personas"] == requested_personas[index:]
        assert [item["persona_id"] for item in partial["aggregated_outputs"]] == (
            requested_personas[:index]
        )
        assert partial["synthesis"] is None
        assert synthesis_calls == []

    consumed_outputs[requested_personas[-1]] = "researcher output"
    complete = synthesize_lateral_persona_panel_when_complete(
        entries,
        consumed_outputs,
        synthesize,
    )

    assert complete["ready_for_synthesis"] is True
    assert complete["missing_personas"] == []
    assert complete["synthesis"] == {
        "persona_ids": requested_personas,
        "combined": "simplifier output\nhacker output\nresearcher output",
    }
    assert len(synthesis_calls) == 1
    assert [item["persona_id"] for item in synthesis_calls[0]] == requested_personas


@pytest.mark.asyncio
async def test_sequential_lateral_synthesis_precedes_interview_continuation() -> None:
    """Sequential fallback also synthesizes before the interview turn resumes."""
    requested_personas = ["simplifier", "hacker", "researcher"]
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "interview needs ordered persona review",
            "current_approach": "continue only after sequential synthesis",
            "personas": requested_personas,
        }
    )

    assert result.is_ok, result
    entries = lateral_review_response_to_interview_orchestration_entries(
        result.unwrap(),
        session_id="sess-sequential-continuation-order",
        runtime_supports_parallel_subagents=False,
    )
    assert all(entry["sequential_fallback_used"] is True for entry in entries)

    timeline: list[str] = []

    def synthesize(aggregated_outputs: list[dict]) -> dict:
        timeline.append("synthesis")
        return {
            "persona_ids": [item["persona_id"] for item in aggregated_outputs],
            "summary": "ordered lateral synthesis is ready",
        }

    def continue_interview(synthesis: dict) -> dict:
        assert timeline == ["synthesis"]
        assert synthesis["persona_ids"] == requested_personas
        timeline.append("interview_continuation")
        return {
            "next_question": "Which assumption remains unclear?",
            "lateral_synthesis": synthesis,
        }

    incomplete = continue_interview_after_lateral_persona_synthesis(
        entries,
        {
            "simplifier": "reduce follow-up scope",
            "hacker": "identify workaround risk",
        },
        synthesize,
        continue_interview,
    )

    assert incomplete["ready_for_synthesis"] is False
    assert incomplete["continued_interview"] is False
    assert timeline == []

    complete = continue_interview_after_lateral_persona_synthesis(
        entries,
        {
            "researcher": "verify implementation facts",
            "hacker": "identify workaround risk",
            "simplifier": "reduce follow-up scope",
        },
        synthesize,
        continue_interview,
    )

    assert complete["ready_for_synthesis"] is True
    assert complete["continued_interview"] is True
    assert complete["interview_continuation"] == {
        "next_question": "Which assumption remains unclear?",
        "lateral_synthesis": complete["synthesis"],
    }
    assert timeline == ["synthesis", "interview_continuation"]


@pytest.mark.asyncio
async def test_multi_persona_subprocess_mode_falls_back_inline() -> None:
    """Subprocess mode → no envelope, inline concatenated prompt text."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    # No envelope in the subprocess fallback path.
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("dispatch_mode") == "sequential"
    assert payload.meta.get("legacy_dispatch_mode") == "inline_fallback"
    assert payload.meta.get("host_action") == "process_payloads_sequentially"
    assert payload.meta.get("result_correlation_key") == "context.persona"
    assert payload.meta.get("persona_count") == 2
    text = payload.content[0].text
    # Each persona section is separated by the canonical delimiter.
    assert text.count("\n\n---\n\n") == 1
    assert "Lateral Thinking" in text


@pytest.mark.asyncio
async def test_multi_persona_non_opencode_runtime_falls_back_inline() -> None:
    """Non-OpenCode runtime → inline fallback regardless of ``opencode_mode``."""
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "all",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("dispatch_mode") == "sequential"
    assert payload.meta.get("legacy_dispatch_mode") == "inline_fallback"
    assert payload.meta.get("host_action") == "process_payloads_sequentially"
    assert payload.meta.get("result_correlation_key") == "context.persona"
    # persona='all' expands to every ThinkingPersona (5).
    assert payload.meta.get("persona_count") == 5


@pytest.mark.asyncio
async def test_single_persona_path_unchanged() -> None:
    """Single-persona (default) path does not touch the dispatch gate."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "contrarian",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    # Single-persona path returns inline text unconditionally.
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("persona") == "contrarian"


@pytest.mark.asyncio
async def test_stagnation_pattern_suggests_persona_when_persona_omitted() -> None:
    """stagnation_pattern selects an affinity persona when persona is omitted."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "progress is flat",
            "current_approach": "rerun the same checks",
            "stagnation_pattern": "no_drift",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta.get("persona") == "researcher"


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["", "   "])
async def test_blank_persona_is_invalid(persona: str) -> None:
    """Blank persona values are invalid rather than treated as omitted."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "progress is flat",
            "current_approach": "rerun the same checks",
            "stagnation_pattern": "no_drift",
            "persona": persona,
        }
    )

    assert result.is_err
    assert "persona cannot be blank" in str(result.error)


@pytest.mark.asyncio
async def test_personas_list_takes_precedence_over_blank_persona() -> None:
    """Explicit personas list is honored even when persona is blank."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta.get("dispatch_mode") == "sequential"
    assert payload.meta.get("legacy_dispatch_mode") == "inline_fallback"
    assert payload.meta.get("persona_count") == 2


@pytest.mark.asyncio
async def test_stagnation_pattern_excludes_known_failed_personas() -> None:
    """failed_attempts persona names are excluded and unknown values are skipped."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "same failure repeats",
            "current_approach": "retry the same edit",
            "stagnation_pattern": "spinning",
            "failed_attempts": ["hacker", "not-a-persona"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta.get("persona") == "contrarian"


@pytest.mark.asyncio
async def test_stagnation_pattern_errors_when_all_personas_excluded() -> None:
    """When every persona is excluded, the handler does not repeat one."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "progress is flat",
            "current_approach": "tried every persona",
            "stagnation_pattern": "no_drift",
            "failed_attempts": [
                "hacker",
                "researcher",
                "simplifier",
                "architect",
                "contrarian",
            ],
        }
    )

    assert result.is_err
    assert "No available lateral thinking persona remains" in str(result.error)


# ---------------------------------------------------------------------------
# Regression tests for the inline-fallback content-side dispatch contract.
#
# FastMCP's adapter only forwards ``payload.content[0].text`` to the wire
# (``adapter.py:923`` — ``meta`` is dropped, see also
# ``subagent.py:141-144``). The non-plugin debate fan-out therefore depends
# on the canonical per-persona payloads being recoverable from the rendered
# ``content`` text alone. The block below verifies that contract directly.
# ---------------------------------------------------------------------------


_INLINE_DISPATCH_OPEN = "<!-- ouroboros-lateral-inline-dispatch-v1 base64\n"
_INLINE_DISPATCH_CLOSE = "\n-->"


def _extract_inline_dispatch(content_text: str) -> dict:
    """Recover the structured dispatch struct from the rendered content text.

    Mirrors what a SKILL implementer must do on the wire side: locate the
    versioned sentinel block at the end of ``content`` and decode the
    base64-wrapped JSON it carries. Tests use this helper so an escaping or
    formatting regression in the handler is caught before it ships.
    """
    import base64
    import json as _json

    open_idx = content_text.rfind(_INLINE_DISPATCH_OPEN)
    assert open_idx != -1, "inline dispatch sentinel block missing from content"
    close_idx = content_text.rfind(_INLINE_DISPATCH_CLOSE)
    assert close_idx > open_idx, "inline dispatch closing marker missing or misplaced"
    encoded = content_text[open_idx + len(_INLINE_DISPATCH_OPEN) : close_idx]
    decoded = base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    return _json.loads(decoded)


@pytest.mark.asyncio
async def test_sequential_dispatch_carries_dispatch_block_in_content() -> None:
    """Non-plugin debate response embeds canonical payloads in ``content``."""
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    text = payload.content[0].text

    # Visible markdown sections still survive transport.
    assert text.count("\n\n---\n\n") == 1
    assert "Lateral Thinking" in text

    # The dispatch block survives transport and decodes to canonical
    # structured payloads (one per requested persona).
    dispatch = _extract_inline_dispatch(text)
    assert dispatch["dispatch_mode"] == "sequential"
    assert dispatch["legacy_dispatch_mode"] == "inline_fallback"
    assert dispatch["host_action"] == "process_payloads_sequentially"
    assert dispatch["result_correlation_key"] == "context.persona"
    assert dispatch["persona_count"] == 2
    payloads = dispatch["payloads"]
    assert len(payloads) == 2

    for persona_name, persona_payload in zip(["hacker", "contrarian"], payloads, strict=True):
        assert persona_payload["agent"] == persona_name
        assert persona_payload["title"] == f"Lateral ({persona_name})"
        # The canonical prompt carries the "Task for you (subagent)" wrapper
        # that plugin mode also dispatches — same builder, same prompt.
        assert "Task for you (subagent)" in persona_payload["prompt"]
        assert persona_payload["context"]["persona"] == persona_name
        assert persona_payload["context"]["problem_context"] == "stuck on X"
        assert persona_payload["context"]["current_approach"] == "tried Y"


@pytest.mark.asyncio
async def test_sequential_dispatch_survives_html_close_in_user_context() -> None:
    """User context containing ``-->`` cannot prematurely close the comment.

    The dispatch JSON is base64-encoded inside the HTML comment exactly so
    that an HTML/JS debugging snippet supplied as ``problem_context`` or
    ``current_approach`` cannot leak the structured payload into the
    visible markdown by closing the comment early. Base64's alphabet is
    ``[A-Za-z0-9+/=]`` — ``-->`` cannot occur inside the encoded body.
    """
    handler = LateralThinkHandler(
        agent_runtime_backend="gemini",
        opencode_mode=None,
    )

    adversarial_context = (
        "I'm debugging an HTML template that has `<!-- foo -->` and "
        "JS like `<!--[if IE]>...<![endif]-->` everywhere; "
        "the closing `-->` keeps tripping me up."
    )

    result = await handler.handle(
        {
            "problem_context": adversarial_context,
            "current_approach": "looked for `-->` in the source",
            "personas": ["hacker", "architect"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    text = payload.content[0].text

    # The visible markdown faithfully echoes the user's content, so `-->`
    # may legitimately appear before the dispatch sentinel — that is a
    # display concern, not a transport one. What matters is that *inside
    # the comment block* the body is base64, so its `[A-Za-z0-9+/=]`
    # alphabet cannot produce a literal `-->` that would prematurely
    # terminate the wrapper. Verify by isolating the comment block and
    # checking it contains exactly one closing `-->` (the legitimate one).
    open_idx = text.rfind(_INLINE_DISPATCH_OPEN)
    assert open_idx != -1
    comment_region = text[open_idx:]
    assert comment_region.count("-->") == 1, (
        "base64 body must not contain a literal `-->` that could close "
        f"the wrapper early: {comment_region!r}"
    )

    # The dispatch block still decodes cleanly and round-trips the
    # adversarial `problem_context`/`current_approach` verbatim — no
    # corruption from the encoding round-trip.
    dispatch = _extract_inline_dispatch(text)
    assert dispatch["dispatch_mode"] == "sequential"
    assert dispatch["legacy_dispatch_mode"] == "inline_fallback"
    assert dispatch["persona_count"] == 2
    for persona_payload in dispatch["payloads"]:
        ctx = persona_payload["context"]
        assert ctx["problem_context"] == adversarial_context
        assert ctx["current_approach"] == "looked for `-->` in the source"
