"""Strict bounded models for interview adapter inputs and reference state."""

from __future__ import annotations

from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFUSED_TERM_LIMIT = 8
REFERENCE_LIMIT = 8
TERM_LENGTH_LIMIT = 80
REFERENCE_ID_LENGTH_LIMIT = 128
LABEL_LENGTH_LIMIT = 160
URL_LENGTH_LIMIT = 2048
EXCERPT_LENGTH_LIMIT = 2000

_REFERENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ReferenceOrigin(StrEnum):
    """Approved origin values for user-supplied reference cues."""

    USER_TEXT = "user_text"
    URL = "url"
    FILE_REFERENCE = "file_reference"


class ReferenceResolutionStatus(StrEnum):
    """Resolution state persisted by interview state integration."""

    UNRESOLVED = "unresolved"
    ASKED = "asked"
    RESOLVED = "resolved"


class _StrictFrozenModel(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")


class ReferenceCue(_StrictFrozenModel):
    """A bounded user-supplied reference cue.

    The cue stores exactly what the user supplied. It never fetches a URL or
    reads a file reference.
    """

    reference_id: str = Field(min_length=1, max_length=REFERENCE_ID_LENGTH_LIMIT)
    label: str = Field(min_length=1, max_length=LABEL_LENGTH_LIMIT)
    origin: ReferenceOrigin
    url: str | None = Field(default=None, min_length=1, max_length=URL_LENGTH_LIMIT)
    excerpt: str | None = Field(default=None, min_length=1, max_length=EXCERPT_LENGTH_LIMIT)

    @field_validator("reference_id")
    @classmethod
    def _validate_reference_id(cls, value: str) -> str:
        value = value.strip()
        if not _REFERENCE_ID_PATTERN.fullmatch(value):
            raise ValueError("reference_id contains unsupported characters")
        return value

    @field_validator("label", "url", "excerpt")
    @classmethod
    def _strip_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class InterviewTurnContext(_StrictFrozenModel):
    """Validated current-turn adapter inputs."""

    confused_terms: tuple[str, ...] = Field(default_factory=tuple, max_length=CONFUSED_TERM_LIMIT)
    references: tuple[ReferenceCue, ...] = Field(default_factory=tuple, max_length=REFERENCE_LIMIT)

    @field_validator("confused_terms")
    @classmethod
    def _validate_confused_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for term in value:
            stripped = term.strip()
            if not stripped:
                raise ValueError("confused_terms entries must not be blank")
            if len(stripped) > TERM_LENGTH_LIMIT:
                raise ValueError(
                    f"confused_terms entries must be at most {TERM_LENGTH_LIMIT} chars"
                )
            key = stripped.casefold()
            if key not in seen:
                normalized.append(stripped)
                seen.add(key)
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_reference_ids_unique(self) -> InterviewTurnContext:
        reference_ids = [cue.reference_id for cue in self.references]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("references must not contain duplicate reference_id values")
        return self


class ReferenceContrastResolution(_StrictFrozenModel):
    """Resolution state for one reference contrast prompt."""

    reference_id: str = Field(min_length=1, max_length=REFERENCE_ID_LENGTH_LIMIT)
    status: ReferenceResolutionStatus = ReferenceResolutionStatus.UNRESOLVED
    asked_question: str | None = Field(default=None, min_length=1, max_length=4000)
    answer: str | None = Field(default=None, min_length=1, max_length=8000)

    @field_validator("reference_id")
    @classmethod
    def _validate_reference_id(cls, value: str) -> str:
        value = value.strip()
        if not _REFERENCE_ID_PATTERN.fullmatch(value):
            raise ValueError("reference_id contains unsupported characters")
        return value

    @model_validator(mode="after")
    def _validate_status_payload(self) -> ReferenceContrastResolution:
        if self.status is ReferenceResolutionStatus.UNRESOLVED and (
            self.asked_question is not None or self.answer is not None
        ):
            raise ValueError("unresolved references cannot carry question or answer text")
        if self.status is ReferenceResolutionStatus.ASKED and self.asked_question is None:
            raise ValueError("asked references require asked_question")
        if self.status is ReferenceResolutionStatus.RESOLVED and (
            self.asked_question is None or self.answer is None
        ):
            raise ValueError("resolved references require asked_question and answer")
        return self
