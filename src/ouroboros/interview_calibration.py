"""Session-local language calibration for requirement interviews."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError


class InterviewCalibration(BaseModel):
    """Bounded evidence used to tune wording without changing interview rigor."""

    level: Literal["foundational", "working", "fluent"]
    confidence: Literal["low", "medium", "high"]
    evidence: str = Field(min_length=1, max_length=2000)
    unknown_terms: tuple[str, ...] = Field(default_factory=tuple, max_length=12)

    def prompt_guidance(self) -> str:
        """Return compact instructions suitable for the question-generation prompt."""
        if self.level == "foundational":
            wording = (
                "Use plain language, define necessary domain terms before using them, "
                "and include at most one neutral concrete example."
            )
        elif self.level == "working":
            wording = (
                "Use standard terminology, briefly define new or overloaded terms, "
                "and connect the question to practical trade-offs."
            )
        else:
            wording = "Use concise, precise domain terminology and skip basic definitions."
        unknown = ", ".join(self.unknown_terms) or "none explicitly extracted"
        return (
            "Session-local interview language calibration (do not reduce rigor):\n"
            f"- Level: {self.level}\n"
            f"- Explicitly unfamiliar terms: {unknown}\n"
            f"- Wording rule: {wording}"
        )


def infer_interview_calibration(evidence: str) -> InterviewCalibration:
    """Infer a conservative local calibration from explicit self-reported evidence."""
    normalized = " ".join(evidence.split())[:2000]
    lowered = normalized.casefold()
    unknown_markers = (
        "don't know",
        "do not know",
        "not familiar",
        "unfamiliar",
        "cannot explain",
        "can't explain",
        "잘 모르",
        "모르겠",
        "처음",
    )
    working_markers = (
        "built",
        "implemented",
        "used",
        "production",
        "만들어",
        "구현",
        "사용해",
        "운영",
    )
    fluent_markers = ("expert", "deeply", "teach", "전문", "깊이", "설명할 수")

    has_unknown = any(marker in lowered for marker in unknown_markers)
    has_working = any(marker in lowered for marker in working_markers)
    has_fluent = any(marker in lowered for marker in fluent_markers)
    if has_unknown:
        level: Literal["foundational", "working", "fluent"] = "foundational"
    elif has_fluent:
        level = "fluent"
    elif has_working:
        level = "working"
    else:
        level = "working"

    confidence: Literal["low", "medium", "high"] = (
        "high" if has_unknown and has_working else "medium" if has_unknown or has_working else "low"
    )
    unknown_terms: list[str] = []
    unknown_segments: list[str] = []
    english_match = re.search(
        r"(?:don't know|do not know|not familiar with|unfamiliar with)\s+([^.;]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if english_match:
        unknown_segments.append(english_match.group(1))
    # Match "cannot explain X" / "can't explain X" pattern
    explain_match = re.search(
        r"(?:cannot explain|can't explain|can not explain)\s+([^.;]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if explain_match:
        unknown_segments.append(explain_match.group(1))
    # Match "X are/is unfamiliar" pattern (subject before the adjective)
    subj_unfamiliar_match = re.search(
        r"([^.;,]{1,160}?)\s+(?:are|is)\s+unfamiliar",
        normalized,
        flags=re.IGNORECASE,
    )
    if subj_unfamiliar_match:
        unknown_segments.append(subj_unfamiliar_match.group(1))
    korean_match = re.search(
        r"(?:^|[,.;]\s*)([^,.;]{1,160}?)(?:은|는|이|가|을|를)?\s*(?:잘\s*)?(?:모르|낯설)",
        normalized,
    )
    if korean_match:
        unknown_segments.append(korean_match.group(1))
    for segment in unknown_segments:
        for term in re.split(r"\s+(?:and|or)\s+|[과와,/]", segment, flags=re.IGNORECASE):
            cleaned = term.strip(" -:()\"'")
            if cleaned and cleaned not in unknown_terms:
                unknown_terms.append(cleaned)

    return InterviewCalibration(
        level=level,
        confidence=confidence,
        evidence=normalized,
        unknown_terms=tuple(unknown_terms[:12]),
    )


def normalize_interview_calibration(value: Any) -> InterviewCalibration | None:
    """Validate a calibration transported through runtime-handle metadata."""
    if isinstance(value, InterviewCalibration):
        return value
    if isinstance(value, Mapping):
        try:
            return InterviewCalibration.model_validate(dict(value))
        except PydanticValidationError:
            return None
    return None
