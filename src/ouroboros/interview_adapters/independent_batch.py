"""Validated representations for a proposed interview question batch.

A model's proposed dependency labels are not proof of independence.  The
caller must present the entire proposal and obtain confirmation before
answers can be recorded.
"""

from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState

from ouroboros.interview_adapters.first_question import (
    FirstQuestionPresentation,
    selected_answer,
    selected_answers,
)


class ProposedBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    presentation: FirstQuestionPresentation
    dimension: Literal["goal", "constraint", "success", "context", "scope"]
    recommended_index: int = Field(strict=True, ge=1, le=4)
    depends_on: tuple[str, ...]

    @model_validator(mode="after")
    def valid_item(self) -> "ProposedBatchItem":
        if self.depends_on:
            raise ValueError("dependent questions are not eligible for a batch")
        if self.recommended_index > len(self.presentation.choices):
            raise ValueError("recommendation is not a listed choice")
        return self


class ProposedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    questions: tuple[ProposedBatchItem, ...] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def distinct_questions_and_dimensions(self) -> "ProposedBatch":
        if len({item.presentation.question.casefold() for item in self.questions}) != len(
            self.questions
        ):
            raise ValueError("questions must be distinct")
        return self


def new_question_id() -> str:
    return uuid4().hex


def validate_batch_selection(
    batch_id: str | None,
    presentation: FirstQuestionPresentation | None,
    indices: tuple[int, ...] | None,
    response: str | None,
) -> None:
    if indices is None:
        return
    if batch_id is None or presentation is None:
        raise ValueError("Batch selection has no presentation")
    if presentation.multi_select:
        expected = selected_answers(presentation, list(indices))
    elif len(indices) == 1:
        expected = selected_answer(presentation, indices[0])
    else:
        raise ValueError("Invalid single selection")
    if response != expected:
        raise ValueError("Batch selection does not match recorded answer")


def has_pending_batch(state: "InterviewState") -> bool:
    return any(
        getattr(r, "batch_id", None) is not None and r.user_response is None for r in state.rounds
    )
