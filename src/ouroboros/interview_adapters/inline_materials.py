"""Optional source material and its explicitly non-authoritative draft.

This is not interview intent. A quote proves only that the text occurs in the
provided material, not that the material is true or that the user wants it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.interview_adapters.first_question import _safe_single_line


class MaterialDraft(BaseModel):
    """Bounded literal excerpts; legacy states may also contain model assumptions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: Literal["legacy_model", "local_excerpts"] = "legacy_model"
    source_quotes: tuple[str, ...] = Field(max_length=5)
    inferred_assumptions: tuple[str, ...] = Field(max_length=5)

    @field_validator("source_quotes", "inferred_assumptions")
    @classmethod
    def safe_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not value.strip() or len(value) > 400 or not _safe_single_line(value):
                raise ValueError("Invalid material draft item")
        return values

    @model_validator(mode="after")
    def local_has_no_assumptions(self) -> "MaterialDraft":
        if self.origin == "local_excerpts" and self.inferred_assumptions:
            raise ValueError("Local excerpts cannot contain model assumptions")
        return self


class MaterialEvidenceDecision(BaseModel):
    """A host-reported decision to include or remove one local evidence reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    evidence_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal["source_quote", "model_assumption"]
    index: int = Field(ge=0, le=4)
    action: Literal["include", "remove"]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    presented_prompt: str = Field(min_length=1, max_length=2000)
    user_response: Literal["INCLUDE", "REMOVE"]

    @model_validator(mode="after")
    def decision_matches_response(self) -> "MaterialEvidenceDecision":
        if self.user_response != self.action.upper():
            raise ValueError("Material evidence decision does not match response")
        return self


class InlineMaterial(BaseModel):
    """One user-provided source, kept outside requirements and score inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4000)
    status: Literal["pending", "ready", "unavailable"] = "pending"
    draft: MaterialDraft | None = None
    evidence_decisions: tuple[MaterialEvidenceDecision, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def validate_draft(self) -> "InlineMaterial":
        if not self.text.strip():
            raise ValueError("Material must not be blank")
        if (self.status == "ready") != (self.draft is not None):
            raise ValueError("Material draft status is inconsistent")
        if self.draft and any(quote not in self.text for quote in self.draft.source_quotes):
            raise ValueError("A source quote is not present in the material")
        return self
