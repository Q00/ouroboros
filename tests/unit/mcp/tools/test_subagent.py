"""Tests for subagent dispatch helper module.

TDD: write tests FIRST, then implement src/ouroboros/mcp/tools/subagent.py.

Tests verify:
1. build_subagent_payload() returns correct structure
2. build_subagent_result() wraps payload in MCPToolResult with meta._subagent
3. All tool-specific builders produce valid payloads
4. Required fields enforced, optional fields handled
5. Prompt includes system prompt + user context
6. Context round-trips tool arguments for bridge callback
"""

from __future__ import annotations

import json

import pytest

from ouroboros.mcp.tools.subagent import (
    SubagentPayload,
    build_evaluate_subagent,
    build_execute_subagent,
    build_generate_seed_subagent,
    build_interview_subagent,
    build_pm_interview_subagent,
    build_qa_subagent,
    build_subagent_payload,
    build_subagent_result,
)
from ouroboros.mcp.types import ContentType, MCPToolResult

# ---------------------------------------------------------------------------
# build_subagent_payload: core structure tests
# ---------------------------------------------------------------------------


class TestBuildSubagentPayload:
    """Test the low-level payload builder."""

    def test_returns_subagent_payload_dataclass(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA eval",
            prompt="Evaluate this artifact",
        )
        assert isinstance(p, SubagentPayload)

    def test_required_fields_present(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA eval",
            prompt="Evaluate this",
        )
        assert p.tool_name == "ouroboros_qa"
        assert p.title == "QA eval"
        assert p.prompt == "Evaluate this"
        assert p.agent == "general"  # default

    def test_custom_agent_type(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_execute_seed",
            title="Execute seed",
            prompt="Run this seed",
            agent="general",
        )
        assert p.agent == "general"

    def test_optional_model_hint(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
            model="claude-sonnet-4-20250514",
        )
        assert p.model == "claude-sonnet-4-20250514"

    def test_model_defaults_none(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
        )
        assert p.model is None

    def test_context_round_trip(self) -> None:
        ctx = {"artifact": "code here", "quality_bar": "no bugs"}
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
            context=ctx,
        )
        assert p.context == ctx

    def test_context_defaults_empty_dict(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
        )
        assert p.context == {}

    def test_to_dict_produces_correct_keys(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA eval",
            prompt="Evaluate this",
            agent="general",
            model="gpt-4o",
            context={"key": "val"},
        )
        d = p.to_dict()
        assert set(d.keys()) == {"tool_name", "title", "agent", "prompt", "model", "context"}
        assert d["tool_name"] == "ouroboros_qa"

    def test_to_dict_omits_none_model(self) -> None:
        """When model is None, to_dict should still include it as None."""
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
        )
        d = p.to_dict()
        assert "model" in d
        assert d["model"] is None

    def test_prompt_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError, match="prompt"):
            build_subagent_payload(
                tool_name="ouroboros_qa",
                title="QA",
                prompt="",
            )

    def test_tool_name_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError, match="tool_name"):
            build_subagent_payload(
                tool_name="",
                title="QA",
                prompt="Eval",
            )

    def test_title_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError, match="title"):
            build_subagent_payload(
                tool_name="ouroboros_qa",
                title="",
                prompt="Eval",
            )

    def test_is_json_serializable(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
            context={"nested": {"deep": [1, 2, 3]}},
        )
        serialized = json.dumps(p.to_dict())
        assert isinstance(serialized, str)
        roundtrip = json.loads(serialized)
        assert roundtrip["tool_name"] == "ouroboros_qa"


# ---------------------------------------------------------------------------
# build_subagent_result: MCPToolResult wrapper tests
# ---------------------------------------------------------------------------


class TestBuildSubagentResult:
    """Test wrapping payload into MCPToolResult."""

    def test_returns_result_ok_with_mcp_tool_result(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
        )
        result = build_subagent_result(p)
        assert result.is_ok
        assert isinstance(result.value, MCPToolResult)

    def test_meta_contains_subagent_key(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
        )
        result = build_subagent_result(p)
        mcp_result = result.value
        assert "_subagent" in mcp_result.meta
        assert mcp_result.meta["_subagent"]["tool_name"] == "ouroboros_qa"

    def test_content_has_dispatch_json(self) -> None:
        """Content should have JSON with _subagent key (parsed by bridge plugin)."""
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA evaluation",
            prompt="Eval",
        )
        result = build_subagent_result(p)
        mcp_result = result.value
        assert len(mcp_result.content) == 1
        assert mcp_result.content[0].type == ContentType.TEXT
        text = mcp_result.content[0].text
        import json

        parsed = json.loads(text)
        assert "_subagent" in parsed
        assert parsed["_subagent"]["tool_name"] == "ouroboros_qa"
        assert parsed["_subagent"]["title"] == "QA evaluation"

    def test_is_error_is_false(self) -> None:
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
        )
        result = build_subagent_result(p)
        assert result.value.is_error is False

    def test_meta_subagent_matches_payload_dict(self) -> None:
        ctx = {"artifact": "hello", "quality_bar": "good"}
        p = build_subagent_payload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="Eval",
            context=ctx,
        )
        result = build_subagent_result(p)
        assert result.value.meta["_subagent"] == p.to_dict()


class TestBuildSubagentResultResponseShape:
    """response_shape kwarg merges natural tool response keys alongside _subagent.

    Round-3 reviewer fix: plugin dispatch must preserve each tool's public
    response shape. Passing response_shape={'job_id': ..., 'status': ...}
    yields JSON body = {job_id, status, _subagent: {...}} — plugin still
    finds its key, consumers still find documented fields.
    """

    def test_legacy_path_unchanged_when_shape_none(self) -> None:
        """No response_shape kwarg = legacy sole-key envelope."""
        import json

        p = build_subagent_payload(tool_name="ouroboros_qa", title="QA", prompt="x")
        result = build_subagent_result(p)
        parsed = json.loads(result.value.content[0].text)
        assert set(parsed.keys()) == {"_subagent"}

    def test_shape_keys_merged_into_content_json(self) -> None:
        import json

        p = build_subagent_payload(
            tool_name="ouroboros_start_execute_seed", title="exec", prompt="x"
        )
        shape = {
            "job_id": None,
            "session_id": "s-1",
            "status": "delegated_to_subagent",
            "dispatch_mode": "plugin",
        }
        result = build_subagent_result(p, response_shape=shape)
        parsed = json.loads(result.value.content[0].text)
        # Both natural keys AND _subagent present
        assert parsed["job_id"] is None
        assert parsed["session_id"] == "s-1"
        assert parsed["status"] == "delegated_to_subagent"
        assert parsed["dispatch_mode"] == "plugin"
        assert "_subagent" in parsed
        assert parsed["_subagent"]["tool_name"] == "ouroboros_start_execute_seed"

    def test_shape_keys_merged_into_meta(self) -> None:
        p = build_subagent_payload(tool_name="ouroboros_qa", title="QA", prompt="x")
        shape = {"qa_session_id": "q-1", "status": "delegated_to_subagent"}
        result = build_subagent_result(p, response_shape=shape)
        meta = result.value.meta
        assert meta["qa_session_id"] == "q-1"
        assert meta["status"] == "delegated_to_subagent"
        assert "_subagent" in meta

    def test_subagent_key_not_overwritten_by_shape(self) -> None:
        """If caller passes _subagent in shape, real payload wins."""
        import json

        p = build_subagent_payload(tool_name="ouroboros_qa", title="QA", prompt="x")
        shape = {"_subagent": "bogus", "status": "delegated_to_subagent"}
        result = build_subagent_result(p, response_shape=shape)
        parsed = json.loads(result.value.content[0].text)
        assert parsed["_subagent"] != "bogus"
        assert parsed["_subagent"]["tool_name"] == "ouroboros_qa"


# ---------------------------------------------------------------------------
# Tool-specific builders: QA
# ---------------------------------------------------------------------------


class TestBuildQaSubagent:
    """Test QA-specific subagent builder."""

    def test_returns_subagent_payload(self) -> None:
        p = build_qa_subagent(
            artifact="def foo(): pass",
            quality_bar="All functions have docstrings",
            artifact_type="code",
        )
        assert isinstance(p, SubagentPayload)
        assert p.tool_name == "ouroboros_qa"

    def test_prompt_includes_artifact_and_quality_bar(self) -> None:
        p = build_qa_subagent(
            artifact="def foo(): pass",
            quality_bar="All functions have docstrings",
            artifact_type="code",
        )
        assert "def foo(): pass" in p.prompt
        assert "All functions have docstrings" in p.prompt

    def test_prompt_includes_system_prompt(self) -> None:
        """Prompt should include the qa-judge system prompt."""
        p = build_qa_subagent(
            artifact="code",
            quality_bar="bar",
            artifact_type="code",
        )
        # At minimum the prompt should reference QA evaluation
        assert "qa" in p.prompt.lower() or "quality" in p.prompt.lower()

    def test_context_preserves_all_arguments(self) -> None:
        p = build_qa_subagent(
            artifact="code",
            quality_bar="bar",
            artifact_type="document",
            reference="ref",
            pass_threshold=0.9,
            qa_session_id="qa-123",
            iteration_history=[{"score": 0.5}],
            seed_content="goal: test",
        )
        assert p.context["artifact"] == "code"
        assert p.context["quality_bar"] == "bar"
        assert p.context["artifact_type"] == "document"
        assert p.context["reference"] == "ref"
        assert p.context["pass_threshold"] == 0.9
        assert p.context["qa_session_id"] == "qa-123"
        assert p.context["iteration_history"] == [{"score": 0.5}]
        assert p.context["seed_content"] == "goal: test"

    def test_title_contains_qa(self) -> None:
        p = build_qa_subagent(
            artifact="code",
            quality_bar="bar",
            artifact_type="code",
        )
        assert "qa" in p.title.lower()

    def test_prompt_instructs_json_output(self) -> None:
        """Subagent prompt must instruct LLM to return JSON verdict."""
        p = build_qa_subagent(
            artifact="code",
            quality_bar="bar",
            artifact_type="code",
        )
        assert "json" in p.prompt.lower()


# ---------------------------------------------------------------------------
# Tool-specific builders: Interview
# ---------------------------------------------------------------------------


class TestBuildInterviewSubagent:
    """Test interview subagent builder."""

    def test_returns_correct_tool_name(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="start",
            initial_context="Build a web app",
        )
        assert p.tool_name == "ouroboros_interview"

    def test_start_prompt_includes_context(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="start",
            initial_context="Build a REST API",
        )
        assert "Build a REST API" in p.prompt

    def test_start_prompt_bounds_initial_context_from_head(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="start",
            initial_context="PRIMARY GOAL: Build a REST API. " + ("details " * 1_000),
        )
        assert "PRIMARY GOAL: Build a REST API." in p.prompt
        assert "[truncated]" in p.prompt
        assert "details " * 200 not in p.prompt
        assert len(p.prompt) < 5_000

    def test_answer_prompt_includes_answer(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="Python with FastAPI",
        )
        assert "Python with FastAPI" in p.prompt

    def test_answer_prompt_requires_seed_ready_guard(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="Python with FastAPI",
        )
        assert "Do not treat ambiguity <= 0.2 as sufficient for closure" in p.prompt
        assert "ownership/SSoT" in p.prompt
        assert "declare ready if ambiguity <= 0.2" not in p.prompt

    def test_answer_prompt_uses_seed_closer_as_guard_ssot(self, monkeypatch) -> None:
        from ouroboros.agents import loader

        def fake_load_agent_prompt(agent_name: str) -> str:
            if agent_name == "socratic-interviewer":
                return "SOCRATIC INTERVIEWER PROMPT"
            raise FileNotFoundError(agent_name)

        def fake_load_agent_section(agent_name: str, section: str) -> str:
            if agent_name == "seed-closer" and section == "CLOSURE GATE SUMMARY":
                return "CANONICAL SEED CLOSER SUMMARY"
            raise KeyError(section)

        monkeypatch.setattr(loader, "load_agent_prompt", fake_load_agent_prompt)
        monkeypatch.setattr(loader, "load_agent_section", fake_load_agent_section)

        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="Python with FastAPI",
        )

        assert "SOCRATIC INTERVIEWER PROMPT" in p.prompt
        assert "CANONICAL SEED CLOSER SUMMARY" in p.prompt

    def test_answer_prompt_bounds_large_transcript_and_answer(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="A" * 5_000,
            transcript="T" * 5_000,
        )

        assert "[truncated]" in p.prompt
        assert "A" * 1_000 not in p.prompt
        assert "T" * 1_000 not in p.prompt
        assert len(p.prompt) < 5_000

    def test_answer_prompt_preserves_latest_transcript_round(self) -> None:
        latest_question = "**Q7:** Should subscription control be server-side or client-side?"
        latest_answer = "**A7:** Server-side should own the final decision."
        transcript = (
            f"**Q6:** {'older context ' * 80}\n"
            f"**A6:** {'older answer ' * 80}\n\n"
            f"{latest_question}\n{latest_answer}"
        )

        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="Server-side should own the final decision.",
            transcript=transcript,
        )

        assert latest_question in p.prompt
        assert latest_answer in p.prompt
        assert "older context " * 20 not in p.prompt

    def test_answer_prompt_compacts_multiline_latest_round_by_markers(self) -> None:
        latest_question = (
            "**Q7:** Should subscription control be server-side or client-side?\n"
            "Please decide this before Seed generation."
        )
        transcript = (
            "**Q6:** Previous question\n"
            "**A6:** Previous answer\n\n"
            f"{latest_question}\n"
            f"**A7:** Server-side should own it.\n\n{'code line\\n' * 500}"
        )

        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="Server-side should own it.",
            transcript=transcript,
        )

        assert "**Q7:** Should subscription control be server-side or client-side?" in p.prompt
        assert "Please decide this before Seed generation." in p.prompt
        assert "**A7:** Server-side should own it." in p.prompt
        assert "code line\n" * 100 not in p.prompt
        assert len(p.prompt) < 5_000

    def test_answer_prompt_falls_back_when_seed_closer_summary_missing(self, monkeypatch) -> None:
        from ouroboros.agents import loader

        def fake_load_agent_prompt(agent_name: str) -> str:
            if agent_name == "socratic-interviewer":
                return "SOCRATIC INTERVIEWER PROMPT"
            raise FileNotFoundError(agent_name)

        def fake_load_agent_section(agent_name: str, section: str) -> str:
            if agent_name == "seed-closer" and section == "YOUR APPROACH":
                return "FALLBACK SEED CLOSER APPROACH"
            raise KeyError(section)

        monkeypatch.setattr(loader, "load_agent_prompt", fake_load_agent_prompt)
        monkeypatch.setattr(loader, "load_agent_section", fake_load_agent_section)

        p = build_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="Python with FastAPI",
        )

        assert "FALLBACK SEED CLOSER APPROACH" in p.prompt

    def test_context_preserves_session_id(self) -> None:
        p = build_interview_subagent(
            session_id="sess-123",
            action="start",
        )
        assert p.context["session_id"] == "sess-123"


# ---------------------------------------------------------------------------
# Tool-specific builders: Generate Seed
# ---------------------------------------------------------------------------


class TestBuildGenerateSeedSubagent:
    """Test seed generation subagent builder."""

    def test_returns_correct_tool_name(self) -> None:
        p = build_generate_seed_subagent(session_id="sess-123")
        assert p.tool_name == "ouroboros_generate_seed"

    def test_prompt_references_seed_generation(self) -> None:
        p = build_generate_seed_subagent(session_id="sess-123")
        assert "seed" in p.prompt.lower()

    def test_context_has_session_id(self) -> None:
        p = build_generate_seed_subagent(
            session_id="sess-123",
            ambiguity_score=0.15,
        )
        assert p.context["session_id"] == "sess-123"
        assert p.context["ambiguity_score"] == 0.15


# ---------------------------------------------------------------------------
# Tool-specific builders: Evaluate
# ---------------------------------------------------------------------------


class TestBuildEvaluateSubagent:
    """Test evaluate subagent builder."""

    def test_returns_correct_tool_name(self) -> None:
        p = build_evaluate_subagent(
            session_id="sess-123",
            artifact="code here",
        )
        assert p.tool_name == "ouroboros_evaluate"

    def test_prompt_includes_artifact(self) -> None:
        p = build_evaluate_subagent(
            session_id="sess-123",
            artifact="def main(): pass",
        )
        assert "def main(): pass" in p.prompt

    def test_context_preserves_all_args(self) -> None:
        p = build_evaluate_subagent(
            session_id="sess-123",
            artifact="code",
            artifact_type="code",
            seed_content="goal: test",
            acceptance_criterion="tests pass",
            working_dir="/tmp",
            trigger_consensus=True,
        )
        assert p.context["session_id"] == "sess-123"
        assert p.context["trigger_consensus"] is True


# ---------------------------------------------------------------------------
# Tool-specific builders: Execute
# ---------------------------------------------------------------------------


class TestBuildExecuteSubagent:
    """Test execute subagent builder."""

    def test_returns_correct_tool_name(self) -> None:
        p = build_execute_subagent(
            seed_content="goal: build it",
            session_id="sess-123",
        )
        assert p.tool_name == "ouroboros_execute_seed"

    def test_prompt_includes_seed(self) -> None:
        p = build_execute_subagent(
            seed_content="goal: build a CLI tool",
            session_id="sess-123",
        )
        assert "build a CLI tool" in p.prompt

    def test_context_preserves_execution_args(self) -> None:
        p = build_execute_subagent(
            seed_content="goal: test",
            session_id="sess-123",
            seed_path="/tmp/seed.yaml",
            cwd="/project",
            max_iterations=5,
            skip_qa=True,
        )
        assert p.context["session_id"] == "sess-123"
        assert p.context["seed_path"] == "/tmp/seed.yaml"
        assert p.context["cwd"] == "/project"
        assert p.context["max_iterations"] == 5
        assert p.context["skip_qa"] is True


# ---------------------------------------------------------------------------
# Tool-specific builders: PM Interview
# ---------------------------------------------------------------------------


class TestBuildPmInterviewSubagent:
    """Test PM interview subagent builder."""

    def test_returns_correct_tool_name(self) -> None:
        p = build_pm_interview_subagent(
            session_id="sess-123",
            action="start",
            initial_context="E-commerce site",
        )
        assert p.tool_name == "ouroboros_pm_interview"

    def test_start_prompt_includes_context(self) -> None:
        p = build_pm_interview_subagent(
            session_id="sess-123",
            action="start",
            initial_context="E-commerce site",
        )
        assert "E-commerce site" in p.prompt

    def test_answer_prompt_includes_answer(self) -> None:
        p = build_pm_interview_subagent(
            session_id="sess-123",
            action="answer",
            answer="React + Node.js",
        )
        assert "React + Node.js" in p.prompt

    def test_generate_action(self) -> None:
        p = build_pm_interview_subagent(
            session_id="sess-123",
            action="generate",
        )
        assert "generate" in p.prompt.lower() or "seed" in p.prompt.lower()

    def test_context_preserves_all_fields(self) -> None:
        p = build_pm_interview_subagent(
            session_id="sess-123",
            action="start",
            initial_context="site",
            cwd="/project",
            selected_repos=["/repo1", "/repo2"],
        )
        assert p.context["session_id"] == "sess-123"
        assert p.context["action"] == "start"
        assert p.context["selected_repos"] == ["/repo1", "/repo2"]


# ---------------------------------------------------------------------------
# Runtime dispatch gate
# ---------------------------------------------------------------------------


class TestShouldDispatchViaPlugin:
    """should_dispatch_via_plugin() gate truth table."""

    def test_opencode_plugin_true(self) -> None:
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        assert should_dispatch_via_plugin("opencode", "plugin") is True

    def test_opencode_subprocess_false(self) -> None:
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        assert should_dispatch_via_plugin("opencode", "subprocess") is False

    def test_opencode_mode_none_does_not_dispatch(self) -> None:
        """Upgraded users without explicit plugin setup must NOT get envelopes."""
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        assert should_dispatch_via_plugin("opencode", None) is False

    def test_non_opencode_runtime_never_dispatches(self) -> None:
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        assert should_dispatch_via_plugin("claude", "plugin") is False
        assert should_dispatch_via_plugin("claude", "subprocess") is False
        assert should_dispatch_via_plugin("claude", None) is False
        assert should_dispatch_via_plugin("codex", "plugin") is False
        assert should_dispatch_via_plugin(None, None) is False
        assert should_dispatch_via_plugin("", "plugin") is False

    def test_opencode_cli_alias_accepted(self) -> None:
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        assert should_dispatch_via_plugin("opencode_cli", "plugin") is True

    def test_case_insensitive(self) -> None:
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        assert should_dispatch_via_plugin("OpenCode", "PLUGIN") is True
        assert should_dispatch_via_plugin("OPENCODE", "Subprocess") is False


class TestEmitSubagentDispatchedEvent:
    """emit_subagent_dispatched_event() audit emission."""

    async def test_skips_when_event_store_none(self) -> None:
        from ouroboros.mcp.tools.subagent import (
            SubagentPayload,
            emit_subagent_dispatched_event,
        )

        payload = SubagentPayload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="p",
            context={"k": "v"},
        )
        # Must not raise.
        await emit_subagent_dispatched_event(None, session_id="s", payload=payload)

    async def test_appends_base_event_on_real_store(self) -> None:
        from unittest.mock import AsyncMock

        from ouroboros.events.base import BaseEvent
        from ouroboros.mcp.tools.subagent import (
            SubagentPayload,
            emit_subagent_dispatched_event,
        )

        store = AsyncMock()
        payload = SubagentPayload(
            tool_name="ouroboros_qa",
            title="QA",
            prompt="hello world",
            context={"artifact": "x"},
            agent="custom-agent",
            model="gpt-5",
        )
        await emit_subagent_dispatched_event(store, session_id="sess-1", payload=payload)
        store.append.assert_awaited_once()
        (event,) = store.append.await_args.args
        assert isinstance(event, BaseEvent)
        assert event.type == "subagent.dispatched"
        assert event.aggregate_type == "subagent"
        assert event.aggregate_id == "sess-1"
        assert event.data["tool_name"] == "ouroboros_qa"
        assert event.data["agent"] == "custom-agent"
        assert event.data["model"] == "gpt-5"
        assert event.data["prompt_len"] == len("hello world")
        assert event.data["context_keys"] == ["artifact"]
        assert event.data["session_id"] == "sess-1"

    async def test_fallback_aggregate_id_when_session_missing(self) -> None:
        from unittest.mock import AsyncMock

        from ouroboros.mcp.tools.subagent import (
            SubagentPayload,
            emit_subagent_dispatched_event,
        )

        store = AsyncMock()
        payload = SubagentPayload(
            tool_name="ouroboros_interview",
            title="Interview",
            prompt="p",
            context={},
        )
        await emit_subagent_dispatched_event(store, session_id=None, payload=payload)
        (event,) = store.append.await_args.args
        assert event.aggregate_id == "subagent-ouroboros_interview"

    async def test_swallows_exceptions(self) -> None:
        """Audit emission must never break dispatch."""
        from unittest.mock import AsyncMock

        from ouroboros.mcp.tools.subagent import (
            SubagentPayload,
            emit_subagent_dispatched_event,
        )

        store = AsyncMock()
        store.append.side_effect = RuntimeError("db down")
        payload = SubagentPayload(tool_name="ouroboros_qa", title="QA", prompt="p", context={})
        # Must not raise.
        await emit_subagent_dispatched_event(store, session_id="s", payload=payload)


# ---------------------------------------------------------------------------
# Gate integration: subprocess mode falls through
# ---------------------------------------------------------------------------


class TestSubprocessModeFallsThrough:
    """When opencode_mode=subprocess, handlers must NOT return _subagent."""

    async def test_qa_handler_subprocess_no_envelope(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.types import Result
        from ouroboros.mcp.tools.qa import QAHandler

        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.err("stubbed"))
        handler = QAHandler(
            agent_runtime_backend="opencode",
            opencode_mode="subprocess",
            llm_adapter=adapter,
        )
        result = await handler.handle({"artifact": "x", "quality_bar": "y"})
        adapter.complete.assert_awaited()
        if result.is_ok:
            assert "_subagent" not in (result.value.meta or {})

    async def test_non_opencode_runtime_no_envelope(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.types import Result
        from ouroboros.mcp.tools.qa import QAHandler

        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.err("stubbed"))
        handler = QAHandler(
            agent_runtime_backend="claude",
            opencode_mode="plugin",
            llm_adapter=adapter,
        )
        result = await handler.handle({"artifact": "x", "quality_bar": "y"})
        adapter.complete.assert_awaited()
        if result.is_ok:
            assert "_subagent" not in (result.value.meta or {})
