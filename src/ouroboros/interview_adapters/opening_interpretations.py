"""Unconfirmed interpretations for an opt-in interview opening."""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.interview_adapters.first_question import _safe_single_line

OPENING_QUESTION = "What would you like to clarify or correct? You may reject both interpretations."

OPENING_INSTRUCTION = (
    "Return only JSON with key interpretations containing exactly two distinct, "
    "competing interpretations of the user's request. Each object must contain "
    "interpretation, evidence, and uncertainty, all concise single-line strings. "
    "Use the user's language. Evidence must refer only to the provided context; "
    "mark any missing information as uncertainty. Do not recommend either one, "
    "claim user confirmation, invent requirements, or include other keys."
)


class InterpretationProposal(BaseModel):
    """A model proposal, never a user decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretation: str = Field(min_length=1, max_length=600)
    evidence: str = Field(min_length=1, max_length=600)
    uncertainty: str = Field(min_length=1, max_length=600)

    @field_validator("interpretation", "evidence", "uncertainty")
    @classmethod
    def safe_line(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or not _safe_single_line(value)
            or any(
                marker in value.casefold()
                for marker in (
                    "[from-user]",
                    "[from-code]",
                    "[from-assumption]",
                    "[from-inference]",
                )
            )
        ):
            raise ValueError("interpretation fields must be nonblank single lines")
        return value


class ProviderOpening(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretations: tuple[InterpretationProposal, InterpretationProposal]

    @model_validator(mode="after")
    def distinct(self) -> "ProviderOpening":
        if (
            self.interpretations[0].interpretation.casefold()
            == self.interpretations[1].interpretation.casefold()
        ):
            raise ValueError("interpretations must be distinct")
        return self


class StoredInterpretation(InterpretationProposal):
    item_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    provenance: Literal["proposed"] = "proposed"


class ProposedOpening(BaseModel):
    """Server-owned provenance: no operation in this slice confirms a proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretations: tuple[StoredInterpretation, StoredInterpretation]
    provenance: Literal["proposed"] = "proposed"

    @classmethod
    def from_provider(cls, opening: ProviderOpening) -> "ProposedOpening":
        first, second = opening.interpretations
        return cls(
            interpretations=(
                StoredInterpretation(item_id=uuid4().hex, **first.model_dump()),
                StoredInterpretation(item_id=uuid4().hex, **second.model_dump()),
            )
        )

    def render(self, question: str = OPENING_QUESTION) -> str:
        lines = ["Possible interpretations (proposals, not confirmed decisions):"]
        for number, item in enumerate(self.interpretations, 1):
            lines.extend(
                [
                    f"{number}. {item.interpretation}",
                    f"   Model-cited evidence (unverified): {item.evidence}",
                    f"   Uncertainty (proposal): {item.uncertainty}",
                ]
            )
        lines.extend(["Neither interpretation may be correct.", "", question])
        return "\n".join(lines)
