"""Tests for the ``force`` flag on the ``ouroboros_generate_seed`` MCP tool.

The flag mirrors the CLI ``init`` "Generate Seed anyway" opt-in: when set, the
ambiguity-score threshold is bypassed in both the plugin/subagent dispatch
path and the in-process generation path, the real score is still recorded in
seed metadata, and the bypass is emitted to the audit log.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler
from ouroboros.mcp.tools.subagent import build_generate_seed_subagent
from ouroboros.mcp.types import ToolInputType

# ---------------------------------------------------------------------------
# Tool definition exposes the force parameter
# ---------------------------------------------------------------------------


class TestDefinitionExposesForce:
    """The MCP tool schema advertises ``force`` as an optional boolean."""

    def test_force_parameter_present(self) -> None:
        params = {p.name: p for p in GenerateSeedHandler().definition.parameters}
        assert "force" in params

    def test_force_parameter_is_optional_boolean(self) -> None:
        param = next(p for p in GenerateSeedHandler().definition.parameters if p.name == "force")
        assert param.type == ToolInputType.BOOLEAN
        assert param.required is False

    def test_description_mentions_bypass(self) -> None:
        defn = GenerateSeedHandler().definition
        assert "force" in defn.description.lower()


# ---------------------------------------------------------------------------
# Subagent payload builder honors force
# ---------------------------------------------------------------------------


class TestBuildGenerateSeedSubagentForce:
    """``build_generate_seed_subagent`` plumbs ``force`` into context + prompt."""

    def test_force_defaults_to_false_in_context(self) -> None:
        payload = build_generate_seed_subagent(session_id="sess-1")
        assert payload.context["force"] is False

    def test_force_true_recorded_in_context(self) -> None:
        payload = build_generate_seed_subagent(session_id="sess-1", force=True)
        assert payload.context["force"] is True

    def test_force_true_adds_bypass_note_to_prompt(self) -> None:
        payload = build_generate_seed_subagent(session_id="sess-1", force=True)
        assert "Ambiguity Gate Bypassed" in payload.prompt
        assert "bypassed the ambiguity-score threshold" in payload.prompt

    def test_force_false_omits_bypass_note(self) -> None:
        payload = build_generate_seed_subagent(session_id="sess-1", force=False)
        assert "Ambiguity Gate Bypassed" not in payload.prompt


# ---------------------------------------------------------------------------
# Plugin/subagent dispatch path
# ---------------------------------------------------------------------------


def _state_with_persisted_score(
    *,
    session_id: str,
    score: float,
    completed: bool,
) -> InterviewState:
    state = InterviewState(
        interview_id=session_id,
        initial_context="ctx",
        status=(InterviewStatus.COMPLETED if completed else InterviewStatus.IN_PROGRESS),
        ambiguity_score=score,
    )
    return state


@pytest.fixture
def plugin_handler() -> GenerateSeedHandler:
    return GenerateSeedHandler(agent_runtime_backend="opencode", opencode_mode="plugin")


class TestPluginPathForce:
    """Plugin/subagent dispatch path respects ``force``."""

    @pytest.mark.asyncio
    async def test_high_ambiguity_without_force_returns_error_on_incomplete(
        self, plugin_handler: GenerateSeedHandler
    ) -> None:
        """Sanity check: without force, the threshold gate still rejects."""
        state = _state_with_persisted_score(session_id="sess-high", score=0.8, completed=False)
        with patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
            AsyncMock(return_value=Result.ok(state)),
        ):
            result = await plugin_handler.handle({"session_id": "sess-high"})

        assert result.is_err
        assert "exceeds" in result.error.message
        # Caller-facing hint should mention the new force escape hatch.
        assert "force=true" in result.error.message

    @pytest.mark.asyncio
    async def test_high_ambiguity_with_force_bypasses_gate(
        self, plugin_handler: GenerateSeedHandler
    ) -> None:
        state = _state_with_persisted_score(session_id="sess-high", score=0.8, completed=False)
        with patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
            AsyncMock(return_value=Result.ok(state)),
        ):
            result = await plugin_handler.handle({"session_id": "sess-high", "force": True})

        assert result.is_ok
        meta = result.value.meta
        assert meta["force"] is True
        ctx = meta["_subagent"]["context"]
        assert ctx["force"] is True
        # Bypass note must reach the subagent so it does not re-impose the gate.
        assert "Ambiguity Gate Bypassed" in meta["_subagent"]["prompt"]

    @pytest.mark.asyncio
    async def test_force_default_false_in_response_shape(
        self, plugin_handler: GenerateSeedHandler
    ) -> None:
        state = _state_with_persisted_score(session_id="sess-clear", score=0.1, completed=True)
        with patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
            AsyncMock(return_value=Result.ok(state)),
        ):
            result = await plugin_handler.handle({"session_id": "sess-clear"})

        assert result.is_ok
        assert result.value.meta["force"] is False
        assert result.value.meta["_subagent"]["context"]["force"] is False


# ---------------------------------------------------------------------------
# In-process generation path
# ---------------------------------------------------------------------------


class _GeneratorSpy:
    """Captures the kwargs passed to ``SeedGenerator.generate``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        state: InterviewState,
        ambiguity_score: AmbiguityScore,
        *,
        force: bool = False,
        **_: object,
    ) -> Result:
        self.calls.append(
            {
                "session_id": state.interview_id,
                "score": ambiguity_score.overall_score,
                "force": force,
            }
        )
        # Stop here; the surrounding handler logic for serialization is not
        # under test. Returning an error short-circuits the YAML rendering
        # while still exercising the kwarg propagation path.
        from ouroboros.core.errors import ValidationError

        return Result.err(
            ValidationError(
                "spy stop",
                field="test",
                details={"interview_id": state.interview_id},
            )
        )


class _InterviewEngineStub:
    """Returns a fully-populated ``InterviewState`` for the handler to use."""

    def __init__(self, state: InterviewState) -> None:
        self._state = state

    async def load_state(self, session_id: str) -> Result:
        return Result.ok(self._state)

    async def save_state(self, state: InterviewState) -> Result:
        return Result.ok(None)


def _stored_ambiguity_state(*, session_id: str, score: float) -> InterviewState:
    state = InterviewState(
        interview_id=session_id,
        initial_context="ctx",
        status=InterviewStatus.COMPLETED,
    )
    breakdown = ScoreBreakdown(
        goal_clarity=ComponentScore(
            name="goal_clarity",
            clarity_score=1.0 - score,
            weight=0.40,
            justification="seed",
        ),
        constraint_clarity=ComponentScore(
            name="constraint_clarity",
            clarity_score=1.0 - score,
            weight=0.30,
            justification="seed",
        ),
        success_criteria_clarity=ComponentScore(
            name="success_criteria_clarity",
            clarity_score=1.0 - score,
            weight=0.30,
            justification="seed",
        ),
    )
    state.store_ambiguity(score=score, breakdown=breakdown.model_dump(mode="json"))
    return state


class TestInProcessPathForce:
    """In-process generation propagates ``force`` to ``SeedGenerator.generate``."""

    @pytest.mark.asyncio
    async def test_force_false_by_default(self) -> None:
        spy = _GeneratorSpy()
        state = _stored_ambiguity_state(session_id="sess-A", score=0.1)
        handler = GenerateSeedHandler(
            interview_engine=_InterviewEngineStub(state),
            seed_generator=spy,  # type: ignore[arg-type]
            llm_adapter=object(),  # type: ignore[arg-type]
        )

        result = await handler.handle({"session_id": "sess-A"})

        assert result.is_err  # spy short-circuits
        assert spy.calls == [{"session_id": "sess-A", "score": 0.1, "force": False}]

    @pytest.mark.asyncio
    async def test_force_true_propagates_to_generator(self) -> None:
        spy = _GeneratorSpy()
        # High persisted ambiguity ensures the generator's own gate would
        # reject without force; the kwarg is what unlocks generation.
        state = _stored_ambiguity_state(session_id="sess-B", score=0.85)
        handler = GenerateSeedHandler(
            interview_engine=_InterviewEngineStub(state),
            seed_generator=spy,  # type: ignore[arg-type]
            llm_adapter=object(),  # type: ignore[arg-type]
        )

        result = await handler.handle({"session_id": "sess-B", "force": True})

        assert result.is_err  # spy short-circuits
        assert spy.calls == [{"session_id": "sess-B", "score": 0.85, "force": True}]

    @pytest.mark.asyncio
    async def test_force_truthy_coerced_to_bool(self) -> None:
        """Non-boolean truthy values must be coerced (defensive)."""
        spy = _GeneratorSpy()
        state = _stored_ambiguity_state(session_id="sess-C", score=0.5)
        handler = GenerateSeedHandler(
            interview_engine=_InterviewEngineStub(state),
            seed_generator=spy,  # type: ignore[arg-type]
            llm_adapter=object(),  # type: ignore[arg-type]
        )

        await handler.handle({"session_id": "sess-C", "force": 1})

        assert spy.calls[0]["force"] is True


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class TestForceAuditLogging:
    """Force opt-in must surface in the audit trail for incident review."""

    @pytest.mark.asyncio
    async def test_force_bypass_log_emitted(self) -> None:
        spy = _GeneratorSpy()
        state = _stored_ambiguity_state(session_id="sess-audit", score=0.9)
        handler = GenerateSeedHandler(
            interview_engine=_InterviewEngineStub(state),
            seed_generator=spy,  # type: ignore[arg-type]
            llm_adapter=object(),  # type: ignore[arg-type]
        )

        with patch("ouroboros.mcp.tools.authoring_handlers.log.warning") as mock_warning:
            await handler.handle({"session_id": "sess-audit", "force": True})

        bypass_calls = [
            call
            for call in mock_warning.call_args_list
            if call.args and call.args[0] == "mcp.tool.generate_seed.force_bypass"
        ]
        assert len(bypass_calls) == 1
        assert bypass_calls[0].kwargs["session_id"] == "sess-audit"

    @pytest.mark.asyncio
    async def test_force_bypass_log_absent_when_not_forced(self) -> None:
        spy = _GeneratorSpy()
        state = _stored_ambiguity_state(session_id="sess-no-audit", score=0.1)
        handler = GenerateSeedHandler(
            interview_engine=_InterviewEngineStub(state),
            seed_generator=spy,  # type: ignore[arg-type]
            llm_adapter=object(),  # type: ignore[arg-type]
        )

        with patch("ouroboros.mcp.tools.authoring_handlers.log.warning") as mock_warning:
            await handler.handle({"session_id": "sess-no-audit"})

        bypass_calls = [
            call
            for call in mock_warning.call_args_list
            if call.args and call.args[0] == "mcp.tool.generate_seed.force_bypass"
        ]
        assert bypass_calls == []
