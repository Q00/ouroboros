"""MCP contract tests for reference-aware interview turn context."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    RequirementCandidate,
    RequirementDistillation,
    RequirementEvidence,
    RequirementEvidenceKind,
    RequirementSection,
    compute_requirement_input_fingerprint,
)
from ouroboros.core.types import Result
from ouroboros.interview_adapters import (
    InterviewTurnContext,
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceOrigin,
    ReferenceResolutionStatus,
    build_reference_contrast_question,
)
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.subagent import build_generate_seed_subagent, build_interview_subagent
from ouroboros.mcp.types import ToolInputType


def test_definition_exposes_optional_turn_context_parameters() -> None:
    params = {param.name: param for param in InterviewHandler().definition.parameters}

    assert params["confused_terms"].type is ToolInputType.ARRAY
    assert params["confused_terms"].required is False
    assert params["confused_terms"].items == {"type": "string"}
    assert params["references"].type is ToolInputType.ARRAY
    assert params["references"].required is False
    assert params["references"].items == {"type": "object"}


@pytest.mark.asyncio
async def test_invalid_reference_context_fails_before_dispatch() -> None:
    engine = MagicMock()
    handler = InterviewHandler(interview_engine=engine, llm_adapter=MagicMock())

    result = await handler.handle(
        {
            "initial_context": "Build a tool",
            "references": [
                {
                    "reference_id": "linear",
                    "label": "Linear",
                    "origin": "url",
                    "url": "https://linear.app",
                    "acceptance_criteria": ["keyboard-first"],
                }
            ],
        }
    )

    assert result.is_err
    assert "Invalid interview adapter context" in str(result.error)
    engine.start_interview.assert_not_called()


def test_plugin_payload_carries_context_without_first_turn_injection() -> None:
    context = InterviewTurnContext(
        confused_terms=("affordance",),
        references=(
            ReferenceCue(
                reference_id="linear",
                label="Linear",
                origin=ReferenceOrigin.USER_TEXT,
            ),
        ),
    )

    payload = build_interview_subagent(
        session_id="session-1",
        action="start",
        initial_context="Build a Linear-like tool",
        turn_context=context,
        adapter_question="must not appear on start",
    )

    assert payload.context["turn_context"] == context.model_dump(mode="json")
    assert payload.context["adapter_question"] == "must not appear on start"
    assert "Required Reference/Glossary Adapter Turn" not in payload.prompt


def test_plugin_payload_requires_adapter_question_on_followup() -> None:
    payload = build_interview_subagent(
        session_id="session-1",
        action="answer",
        answer="Fast triage",
        adapter_question="What should be copied or avoided from Linear?",
    )

    assert "Required Reference/Glossary Adapter Turn" in payload.prompt
    assert "What should be copied or avoided from Linear?" in payload.prompt
    assert "never as a requirement or acceptance criterion" in payload.prompt


def test_seed_subagent_payload_separates_promoted_and_omitted_candidates() -> None:
    evidence = RequirementEvidence(
        evidence_id="reference-1",
        kind=RequirementEvidenceKind.REFERENCE_CUE,
        text="Linear-like",
        reference_id="linear",
    )
    candidate = RequirementCandidate(
        candidate_id="candidate-1",
        section=RequirementSection.ACCEPTANCE_CRITERION,
        text="Keyboard-first navigation",
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        reference_ids=("linear",),
        evidence_ids=("reference-1",),
        required=False,
    )
    distillation = RequirementDistillation(
        candidates=(candidate,),
        evidence=(evidence,),
        input_fingerprint=compute_requirement_input_fingerprint({"transcript": "test"}),
    )

    payload = build_generate_seed_subagent(
        session_id="session-1",
        requirement_distillation=distillation,
    )

    assert "Deterministic Requirement Promotion Policy" in payload.prompt
    assert "MUST NOT be promoted" in payload.prompt
    assert (
        payload.context["requirement_distillation"]["candidates"][0]["content_source"]
        == "reference_derived"
    )


@pytest.mark.asyncio
async def test_plugin_reference_seed_is_built_server_side_without_child_leakage() -> None:
    cue = ReferenceCue(
        reference_id="linear",
        label="Linear-like",
        origin=ReferenceOrigin.USER_TEXT,
    )
    question = build_reference_contrast_question(cue)
    state = InterviewState(
        interview_id="session-reference",
        initial_context="Build a Linear-like issue tool",
        status=InterviewStatus.COMPLETED,
        ambiguity_score=0.1,
        rounds=[
            InterviewRound(
                round_number=1,
                question="What outcome matters most?",
                user_response="Fast triage.",
            ),
            InterviewRound(
                round_number=2,
                question=question,
                user_response="Copy speed, not keyboard menus.",
            ),
        ],
        reference_cues=(cue,),
        reference_resolutions=(
            ReferenceContrastResolution(
                reference_id="linear",
                status=ReferenceResolutionStatus.RESOLVED,
                asked_question=question,
                answer="Copy speed, not keyboard menus.",
            ),
        ),
    )
    handler = GenerateSeedHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
            AsyncMock(return_value=Result.ok(state)),
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_save_state",
            AsyncMock(return_value=Result.ok(MagicMock())),
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.dispatch_plugin_terminal",
            AsyncMock(),
        ) as dispatch,
    ):
        result = await handler.handle({"session_id": state.interview_id})

    assert result.is_ok
    assert "Seed Generated Successfully" in result.value.text_content
    assert "Keyboard-first command menu" not in result.value.text_content
    assert result.value.meta["requirement_distillation"]
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_seed_reopens_interview_for_queued_unresolved_reference() -> None:
    state = InterviewState(
        interview_id="session-unresolved-reference",
        initial_context="Build an issue tool",
        status=InterviewStatus.COMPLETED,
        ambiguity_score=0.1,
        rounds=[
            InterviewRound(
                round_number=1,
                question="What outcome matters most?",
                user_response="Fast triage.",
            )
        ],
        reference_cues=(
            ReferenceCue(
                reference_id="linear",
                label="Linear-like",
                origin=ReferenceOrigin.USER_TEXT,
            ),
        ),
    )
    handler = GenerateSeedHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
            AsyncMock(return_value=Result.ok(state)),
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.dispatch_plugin_terminal",
            AsyncMock(),
        ) as dispatch,
    ):
        result = await handler.handle({"session_id": state.interview_id})

    assert result.is_err
    assert "reference_confirmation_required" in str(result.error)
    dispatch.assert_not_awaited()
