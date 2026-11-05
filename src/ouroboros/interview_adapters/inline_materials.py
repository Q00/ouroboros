"""Optional source material and its explicitly non-authoritative draft.

This is not interview intent. A quote proves only that the text occurs in the
provided material, not that the material is true or that the user wants it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.interview_adapters.first_question import _safe_single_line


class MaterialDraft(BaseModel):
    """Bounded model output; the caller separately validates source quotes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_quotes: tuple[str, ...] = Field(max_length=5)
    inferred_assumptions: tuple[str, ...] = Field(max_length=5)

    @field_validator("source_quotes", "inferred_assumptions")
    @classmethod
    def safe_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not value.strip() or len(value) > 400 or not _safe_single_line(value):
                raise ValueError("Invalid material draft item")
        return values


class InlineMaterial(BaseModel):
    """One user-provided source, kept outside requirements and score inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4000)
    status: Literal["pending", "ready", "unavailable"] = "pending"
    draft: MaterialDraft | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> "InlineMaterial":
        if not self.text.strip():
            raise ValueError("Material must not be blank")
        if (self.status == "ready") != (self.draft is not None):
            raise ValueError("Material draft status is inconsistent")
        if self.draft and any(quote not in self.text for quote in self.draft.source_quotes):
            raise ValueError("A source quote is not present in the material")
        return self
