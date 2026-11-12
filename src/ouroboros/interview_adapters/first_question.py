"""An opt-in, single-choice presentation for an interview's first question."""

import json
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _safe_single_line(value: str) -> bool:
    return all(
        unicodedata.category(char) not in {"Cc", "Cs", "Zl", "Zp"}
        and char not in "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for char in value
    )


class FirstQuestionPresentation(BaseModel):
    """Unconfirmed suggestions; the answer, not this object, carries intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=2000)
    choices: tuple[str, ...] = Field(min_length=2, max_length=4)
    direct_input_label: str = Field(min_length=1, max_length=300)
    multi_select: bool = Field(default=False, strict=True)

    @field_validator("question", "direct_input_label")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value or not _safe_single_line(value):
            raise ValueError("must not be blank")
        return value

    @field_validator("choices")
    @classmethod
    def choices_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(
            not value or len(value) > 300 or not _safe_single_line(value) or value.isdecimal()
            for value in cleaned
        ):
            raise ValueError("each choice must be one nonblank line")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("choices must be distinct")
        return cleaned

    @model_validator(mode="after")
    def avoid_choice_collision(self) -> "FirstQuestionPresentation":
        if self.direct_input_label.casefold() in {value.casefold() for value in self.choices}:
            raise ValueError("direct input label must not duplicate a choice")
        if "\n" in self.question or any(
            choice.casefold() in self.question.casefold() for choice in self.choices
        ):
            raise ValueError("question must not embed choice text")
        return self

    def render(self) -> str:
        lines = [
            self.question,
            *(
                f"{chr(97 + i) if self.multi_select else i + 1}. {choice}"
                for i, choice in enumerate(self.choices)
            ),
        ]
        lines.append(self.direct_input_label)
        return "\n".join(lines)


def parse_selection_text(value: str) -> list[int]:
    """Parse explicit a,b text transport; '-' means no selected choices."""
    if value.strip() == "-":
        return []
    tokens = [part.strip().lower() for part in value.split(",")]
    if not tokens or any(part not in {"a", "b", "c", "d"} for part in tokens):
        raise ValueError("Invalid selection text")
    indices = [ord(part) - ord("a") + 1 for part in tokens]
    if len(indices) != len(set(indices)):
        raise ValueError("Duplicate selection")
    return indices


def selected_answers(presentation: FirstQuestionPresentation | None, indices: list[int]) -> str:
    """Convert only an explicit multi-selection to user-owned answer text."""
    if presentation is None or not presentation.multi_select:
        raise ValueError("No multi-select first question is pending")
    if (
        len(indices) > len(presentation.choices)
        or any(
            type(index) is not int or index < 1 or index > len(presentation.choices)
            for index in indices
        )
        or len(set(indices)) != len(indices)
    ):
        raise ValueError("Selected options are invalid")
    selected = [presentation.choices[index - 1] for index in indices]
    return "[from-user] Selected options (JSON): " + json.dumps(selected, ensure_ascii=False)


def selected_answer(presentation: FirstQuestionPresentation | None, index: int) -> str:
    """Convert a confirmed selection, never an ordinary textual number."""
    if presentation is None:
        raise ValueError("No structured first question is pending")
    if presentation.multi_select:
        raise ValueError("Use multi-selection for this question")
    if not 1 <= index <= len(presentation.choices):
        raise ValueError("Selected option is out of range")
    return "[from-user] " + presentation.choices[index - 1]


FIRST_QUESTION_INSTRUCTION = (
    "Respond with a JSON object only, with exactly these keys: question (one clear "
    "requirements question), choices (2 to 4 concise, distinct possible answers), "
    "direct_input_label (an invitation to type a different answer). Use the user's "
    "language. Choices are tentative suggestions, not the user's decisions. "
    "Do not include recommendations, markdown, or extra keys."
)


MULTI_FIRST_QUESTION_INSTRUCTION = (
    "Respond with a JSON object only, with exactly these keys: question (one clear "
    "requirements question instructing the user to select all that apply, including "
    "none), choices (2 to 4 concise, distinct possible answers), direct_input_label "
    "(an invitation to type a different answer), multi_select (true). Use the user's "
    "language. Choices are tentative suggestions, not the user's decisions. "
    "Do not include recommendations, markdown, or extra keys."
)


def validate_stored_selection(
    presentation: FirstQuestionPresentation | None,
    indices: tuple[int, ...] | None,
    response: str | None,
) -> None:
    if indices is not None and response != selected_answers(presentation, list(indices)):
        raise ValueError("stored selection does not match the confirmed answer")


def first_question_instruction(multi_select: bool) -> str:
    return MULTI_FIRST_QUESTION_INSTRUCTION if multi_select else FIRST_QUESTION_INSTRUCTION
