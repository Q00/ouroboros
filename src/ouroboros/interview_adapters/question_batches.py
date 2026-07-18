"""Host-neutral question-batch rendering and answer parsing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QUESTION_BATCH_CONTRACT_VERSION = "interview_question_batch.v1"
MAX_BATCH_QUESTIONS = 100
MAX_QUESTION_OPTIONS = 26

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ANSWER_START = re.compile(r"(?:^|\n|\s*/\s*)(?P<number>\d+)\s*:\s*")


class _StrictFrozenModel(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")


class QuestionBatchOption(_StrictFrozenModel):
    """One stable option in a question payload."""

    option_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=500)

    @field_validator("option_id")
    @classmethod
    def _option_id(cls, value: str) -> str:
        value = value.strip()
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("option_id must be a short machine-readable identifier")
        return value

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _nonblank(value)


class QuestionBatchQuestion(_StrictFrozenModel):
    """A renderer-independent question and its full display contract."""

    question_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    options: tuple[QuestionBatchOption, ...] = Field(
        default_factory=tuple, max_length=MAX_QUESTION_OPTIONS
    )
    multiple: bool = False
    recommended_answer: str = Field(min_length=1, max_length=1000)
    escape_hatch: str = Field(min_length=1, max_length=500)
    source_label: str = Field(min_length=1, max_length=160)

    @field_validator("question_id")
    @classmethod
    def _question_id(cls, value: str) -> str:
        value = value.strip()
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("question_id must be a short machine-readable identifier")
        return value

    @field_validator("question", "recommended_answer", "escape_hatch", "source_label")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _option_contract(self) -> QuestionBatchQuestion:
        if self.multiple and not self.options:
            raise ValueError("multiple-selection questions require options")
        ids = [option.option_id for option in self.options]
        labels = [option.label.casefold() for option in self.options]
        if len(ids) != len(set(ids)) or len(labels) != len(set(labels)):
            raise ValueError("question options must have unique IDs and labels")
        return self


class QuestionBatch(_StrictFrozenModel):
    """The stable batch payload shared by every rendering path."""

    contract_version: Literal["interview_question_batch.v1"] = QUESTION_BATCH_CONTRACT_VERSION
    batch_id: str = Field(min_length=1, max_length=128)
    questions: tuple[QuestionBatchQuestion, ...] = Field(
        min_length=1, max_length=MAX_BATCH_QUESTIONS
    )

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, value: str) -> str:
        value = value.strip()
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("batch_id must be a short machine-readable identifier")
        return value

    @model_validator(mode="after")
    def _unique_questions(self) -> QuestionBatch:
        ids = [question.question_id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("batch questions must use unique question_id values")
        return self


class QuestionHostCapabilities(_StrictFrozenModel):
    """Negotiated rendering features; host identity is deliberately absent."""

    native_question_batches: bool = False
    max_questions_per_batch: int = Field(default=5, ge=1, le=MAX_BATCH_QUESTIONS)


class QuestionRenderMode(StrEnum):
    NATIVE = "native"
    PLAIN_TEXT = "plain_text"


class QuestionBatchPage(_StrictFrozenModel):
    """One capacity-sized page whose questions remain byte-for-byte equivalent."""

    contract_version: Literal["interview_question_batch.v1"] = QUESTION_BATCH_CONTRACT_VERSION
    batch_id: str
    page_number: int = Field(ge=1)
    page_count: int = Field(ge=1)
    question_offset: int = Field(ge=0)
    total_questions: int = Field(ge=1)
    questions: tuple[QuestionBatchQuestion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bounds(self) -> QuestionBatchPage:
        if self.page_number > self.page_count:
            raise ValueError("page_number must not exceed page_count")
        if self.question_offset + len(self.questions) > self.total_questions:
            raise ValueError("page questions exceed total_questions")
        return self


class RenderedQuestionBatchPage(_StrictFrozenModel):
    mode: QuestionRenderMode
    payload: QuestionBatchPage
    text: str | None = None

    @model_validator(mode="after")
    def _content(self) -> RenderedQuestionBatchPage:
        if self.mode is QuestionRenderMode.NATIVE and self.text is not None:
            raise ValueError("native pages must preserve the payload without fallback text")
        if self.mode is QuestionRenderMode.PLAIN_TEXT and not self.text:
            raise ValueError("plain-text pages require rendered text")
        return self


class QuestionAnswer(_StrictFrozenModel):
    """A canonical answer independent of the host rendering path."""

    question_id: str
    selected_option_ids: tuple[str, ...] = ()
    free_text: str | None = None

    @model_validator(mode="after")
    def _content(self) -> QuestionAnswer:
        if bool(self.selected_option_ids) == (self.free_text is not None):
            raise ValueError("answer requires exactly one of selected options or free text")
        return self


class QuestionBatchAnswers(_StrictFrozenModel):
    batch_id: str
    answers: tuple[QuestionAnswer, ...] = Field(min_length=1)


class QuestionAnswerParseError(ValueError):
    """A host answer could not be mapped to every question on a page."""


def render_question_batch(
    batch: QuestionBatch, capabilities: QuestionHostCapabilities
) -> tuple[RenderedQuestionBatchPage, ...]:
    """Choose native or text rendering and paginate to host capacity."""

    capacity = capabilities.max_questions_per_batch
    count = (len(batch.questions) + capacity - 1) // capacity
    mode = (
        QuestionRenderMode.NATIVE
        if capabilities.native_question_batches
        else QuestionRenderMode.PLAIN_TEXT
    )
    pages: list[RenderedQuestionBatchPage] = []
    for number, offset in enumerate(range(0, len(batch.questions), capacity), start=1):
        payload = QuestionBatchPage(
            batch_id=batch.batch_id,
            page_number=number,
            page_count=count,
            question_offset=offset,
            total_questions=len(batch.questions),
            questions=batch.questions[offset : offset + capacity],
        )
        pages.append(
            RenderedQuestionBatchPage(
                mode=mode,
                payload=payload,
                text=render_plain_text_question_page(payload)
                if mode is QuestionRenderMode.PLAIN_TEXT
                else None,
            )
        )
    return tuple(pages)


def render_plain_text_question_page(page: QuestionBatchPage) -> str:
    """Render the deterministic numbered fallback."""

    lines: list[str] = []
    examples: list[str] = []
    for index, question in enumerate(page.questions):
        number = page.question_offset + index + 1
        lines.append(f"{number}. {question.question} [{question.source_label}]")
        for option_index, option in enumerate(question.options):
            lines.append(f"   {_letter(option_index)}) {option.label}")
        lines.append(f"   Recommended: {_recommendation(question)}")
        lines.append(f"   Other: {question.escape_hatch}")
        examples.append(_example(number, question))
        if index < len(page.questions) - 1:
            lines.append("")
    lines.extend(("", f"Reply in this format: {' / '.join(examples)}"))
    return "\n".join(lines)


def parse_native_question_answers(
    page: QuestionBatchPage, response: Mapping[str, object]
) -> QuestionBatchAnswers:
    """Normalize a native host's question-ID keyed response."""

    expected = {question.question_id for question in page.questions}
    received = set(response)
    if missing := expected - received:
        raise QuestionAnswerParseError(f"missing answers for: {', '.join(sorted(missing))}")
    if unknown := received - expected:
        raise QuestionAnswerParseError(f"unknown question IDs: {', '.join(sorted(unknown))}")
    return QuestionBatchAnswers(
        batch_id=page.batch_id,
        answers=tuple(
            _normalize_answer(question, response[question.question_id])
            for question in page.questions
        ),
    )


def parse_plain_text_question_answers(
    page: QuestionBatchPage, response: str
) -> QuestionBatchAnswers:
    """Parse ``1: a / 2: a,c / 3: free text`` or newline equivalents."""

    if not isinstance(response, str) or not response.strip():
        raise QuestionAnswerParseError("plain-text answer must not be blank")
    text = response.strip()
    matches = list(_ANSWER_START.finditer(text))
    if not matches or matches[0].start() != 0:
        raise QuestionAnswerParseError("use numbered answers such as '1: a / 2: a,c'")
    values: dict[int, str] = {}
    for index, match in enumerate(matches):
        number = int(match.group("number"))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip()
        if number in values:
            raise QuestionAnswerParseError(f"question {number} was answered more than once")
        if not value:
            raise QuestionAnswerParseError(f"question {number} has an empty answer")
        values[number] = value

    expected = {page.question_offset + index + 1 for index in range(len(page.questions))}
    if missing := expected - set(values):
        raise QuestionAnswerParseError(f"missing answers for question numbers: {_numbers(missing)}")
    if unknown := set(values) - expected:
        raise QuestionAnswerParseError(f"unexpected question numbers: {_numbers(unknown)}")
    return QuestionBatchAnswers(
        batch_id=page.batch_id,
        answers=tuple(
            _normalize_answer(question, values[page.question_offset + index + 1])
            for index, question in enumerate(page.questions)
        ),
    )


@dataclass(frozen=True, slots=True)
class PlainTextAnswerAttempt:
    answers: QuestionBatchAnswers | None = None
    error: str | None = None
    reprompt: str | None = None
    remaining_reprompts: int = 0
    failed_attempts: int = 0
    exhausted: bool = False

    @property
    def accepted(self) -> bool:
        return self.answers is not None


class PlainTextAnswerCollector:
    """Parse fallback answers with a strict reprompt budget."""

    def __init__(self, page: QuestionBatchPage, *, max_reprompts: int = 2) -> None:
        if type(max_reprompts) is not int or max_reprompts < 0:
            raise ValueError("max_reprompts must be a non-negative integer")
        self._page = page
        self._max_reprompts = max_reprompts
        self._failed_attempts = 0
        self._completed = False
        self._exhausted = False

    @property
    def failed_attempts(self) -> int:
        return self._failed_attempts

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def submit(self, response: str) -> PlainTextAnswerAttempt:
        if self._completed:
            raise RuntimeError("plain-text question page has already been answered")
        if self._exhausted:
            return PlainTextAnswerAttempt(
                error="plain-text answer retries are exhausted",
                exhausted=True,
                failed_attempts=self._failed_attempts,
            )
        try:
            answers = parse_plain_text_question_answers(self._page, response)
        except QuestionAnswerParseError as exc:
            self._failed_attempts += 1
            if self._failed_attempts > self._max_reprompts:
                self._exhausted = True
                return PlainTextAnswerAttempt(
                    error=str(exc), exhausted=True, failed_attempts=self._failed_attempts
                )
            remaining = self._max_reprompts - self._failed_attempts
            return PlainTextAnswerAttempt(
                error=str(exc),
                reprompt=_reprompt(self._page, str(exc)),
                remaining_reprompts=remaining,
                failed_attempts=self._failed_attempts,
            )
        self._completed = True
        return PlainTextAnswerAttempt(
            answers=answers,
            remaining_reprompts=self._max_reprompts - self._failed_attempts,
            failed_attempts=self._failed_attempts,
        )


def _normalize_answer(question: QuestionBatchQuestion, raw: object) -> QuestionAnswer:
    if isinstance(raw, bool) or not isinstance(raw, str | int | Sequence):
        raise QuestionAnswerParseError(f"unsupported answer for {question.question_id}")
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        if not question.options:
            raise QuestionAnswerParseError(
                f"free-text question {question.question_id} requires text"
            )
        tokens = [str(item).strip() for item in raw]
        resolved = tuple(_resolve(question, token) for token in tokens)
        if not tokens or any(value is None for value in resolved):
            raise QuestionAnswerParseError(f"unknown option for {question.question_id}")
        return _selection(question, tuple(value for value in resolved if value))

    value = str(raw).strip()
    if not value:
        raise QuestionAnswerParseError(f"answer for {question.question_id} is empty")
    if not question.options:
        return QuestionAnswer(question_id=question.question_id, free_text=value)
    resolved = tuple(_resolve(question, token.strip()) for token in value.split(","))
    if all(option_id is not None for option_id in resolved):
        return _selection(question, tuple(option_id for option_id in resolved if option_id))
    return QuestionAnswer(question_id=question.question_id, free_text=value)


def _resolve(question: QuestionBatchQuestion, token: str) -> str | None:
    for option in question.options:
        if token == option.option_id or token.casefold() == option.label.casefold():
            return option.option_id
    if token.isdigit() and 0 < int(token) <= len(question.options):
        return question.options[int(token) - 1].option_id
    if len(token) == 1 and token.casefold().isalpha():
        index = ord(token.casefold()) - ord("a")
        if 0 <= index < len(question.options):
            return question.options[index].option_id
    return None


def _selection(question: QuestionBatchQuestion, selected: tuple[str, ...]) -> QuestionAnswer:
    if not question.multiple and len(selected) != 1:
        raise QuestionAnswerParseError(f"question {question.question_id} accepts one option")
    if len(selected) != len(set(selected)):
        raise QuestionAnswerParseError(f"question {question.question_id} repeats an option")
    return QuestionAnswer(question_id=question.question_id, selected_option_ids=selected)


def _recommendation(question: QuestionBatchQuestion) -> str:
    for index, option in enumerate(question.options):
        if question.recommended_answer == option.option_id:
            return f"{_letter(index)}) {option.label}"
    return question.recommended_answer


def _example(number: int, question: QuestionBatchQuestion) -> str:
    if not question.options:
        return f"{number}: <free text>"
    return f"{number}: a,b" if question.multiple else f"{number}: a"


def _reprompt(page: QuestionBatchPage, error: str) -> str:
    examples = [
        _example(page.question_offset + index + 1, question)
        for index, question in enumerate(page.questions)
    ]
    return f"I couldn't read that answer ({error}). Reply as: {' / '.join(examples)}"


def _letter(index: int) -> str:
    return chr(ord("a") + index)


def _numbers(values: set[int]) -> str:
    return ", ".join(str(value) for value in sorted(values))


def _nonblank(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("question payload text must be a string")
    value = value.strip()
    if not value:
        raise ValueError("question payload text must not be blank")
    return value
