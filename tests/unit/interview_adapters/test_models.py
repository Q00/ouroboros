from __future__ import annotations

from pydantic import ValidationError
import pytest

from ouroboros.interview_adapters.models import (
    InterviewTurnContext,
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceResolutionStatus,
)


def test_turn_context_rejects_oversized_confused_terms_and_references() -> None:
    with pytest.raises(ValidationError):
        InterviewTurnContext(confused_terms=tuple(f"term-{index}" for index in range(9)))

    with pytest.raises(ValidationError):
        InterviewTurnContext(
            references=tuple(
                ReferenceCue(reference_id=f"ref-{index}", label="Reference", origin="user_text")
                for index in range(9)
            )
        )


def test_turn_context_rejects_invalid_reference_keys_origins_duplicates_and_sizes() -> None:
    with pytest.raises(ValidationError):
        ReferenceCue.model_validate(
            {"reference_id": "ref-1", "label": "Reference", "origin": "user_text", "extra": "x"}
        )
    with pytest.raises(ValidationError):
        ReferenceCue(reference_id="ref-1", label="Reference", origin="web")
    with pytest.raises(ValidationError):
        ReferenceCue(reference_id="ref-1", label="x" * 161, origin="user_text")
    with pytest.raises(ValidationError):
        ReferenceCue(reference_id="ref-1", label="Reference", origin="url", url="x" * 2049)
    with pytest.raises(ValidationError):
        ReferenceCue(
            reference_id="ref-1", label="Reference", origin="user_text", excerpt="x" * 2001
        )
    with pytest.raises(ValidationError):
        InterviewTurnContext(
            references=(
                ReferenceCue(reference_id="ref-1", label="One", origin="user_text"),
                ReferenceCue(reference_id="ref-1", label="Two", origin="user_text"),
            )
        )


def test_models_are_frozen() -> None:
    cue = ReferenceCue(reference_id="ref-1", label="Reference", origin="user_text")

    with pytest.raises(ValidationError):
        cue.label = "Changed"  # type: ignore[misc]


def test_reference_resolution_state_requires_matching_payload() -> None:
    with pytest.raises(ValidationError):
        ReferenceContrastResolution(reference_id="ref-1", status=ReferenceResolutionStatus.ASKED)
    with pytest.raises(ValidationError):
        ReferenceContrastResolution(
            reference_id="ref-1",
            status=ReferenceResolutionStatus.RESOLVED,
            asked_question="Question?",
        )

    resolved = ReferenceContrastResolution(
        reference_id="ref-1",
        status=ReferenceResolutionStatus.RESOLVED,
        asked_question="Question?",
        answer="Use the workflow speed.",
    )

    assert resolved.answer == "Use the workflow speed."
