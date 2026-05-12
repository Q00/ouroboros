"""Typed auto-mode recovery plan artifacts.

The complete-product recovery loop needs a machine-readable handoff between
QA/lateral analysis and a future Ralph redispatch. This module defines that
artifact without executing it: the pipeline can persist a plan today, while a
follow-up PR can consume the same shape to safely redispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RecoveryPlanAction(StrEnum):
    """Next action proposed by a recovery plan."""

    RALPH_REDISPATCH = "ralph_redispatch"
    MANUAL_INTERVENTION = "manual_intervention"


@dataclass(frozen=True)
class AutoRecoveryPlan:
    """Durable recovery artifact produced after a QA failure."""

    action: RecoveryPlanAction
    safe_to_redispatch: bool
    reason: str
    qa_score: float
    qa_verdict: str
    differences: tuple[str, ...] = field(default_factory=tuple)
    suggestions: tuple[str, ...] = field(default_factory=tuple)
    persona: str | None = None
    instruction: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.safe_to_redispatch, bool):
            msg = "safe_to_redispatch must be a boolean"
            raise ValueError(msg)
        if isinstance(self.qa_score, bool) or not isinstance(self.qa_score, int | float):
            msg = "qa_score must be a number"
            raise ValueError(msg)
        if not isinstance(self.qa_verdict, str) or not self.qa_verdict.strip():
            msg = "qa_verdict must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(self.reason, str) or not self.reason.strip():
            msg = "reason must be a non-empty string"
            raise ValueError(msg)
        if self.persona is not None and (
            not isinstance(self.persona, str) or not self.persona.strip()
        ):
            msg = "persona must be a non-empty string or null"
            raise ValueError(msg)
        if not isinstance(self.instruction, str):
            msg = "instruction must be a string"
            raise ValueError(msg)
        for field_name, values in (
            ("differences", self.differences),
            ("suggestions", self.suggestions),
        ):
            if not isinstance(values, tuple) or any(not isinstance(item, str) for item in values):
                msg = f"{field_name} must be a tuple of strings"
                raise ValueError(msg)
        if self.action is RecoveryPlanAction.RALPH_REDISPATCH and not self.safe_to_redispatch:
            msg = "ralph_redispatch plans must set safe_to_redispatch=true"
            raise ValueError(msg)
        if self.action is RecoveryPlanAction.MANUAL_INTERVENTION and self.safe_to_redispatch:
            msg = "manual_intervention plans must set safe_to_redispatch=false"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "action": self.action.value,
            "safe_to_redispatch": self.safe_to_redispatch,
            "reason": self.reason,
            "qa_score": float(self.qa_score),
            "qa_verdict": self.qa_verdict,
            "differences": list(self.differences),
            "suggestions": list(self.suggestions),
            "persona": self.persona,
            "instruction": self.instruction,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoRecoveryPlan:
        """Deserialize and validate a persisted recovery plan."""
        if not isinstance(data, dict):
            msg = "recovery plan must be an object"
            raise ValueError(msg)
        try:
            action = RecoveryPlanAction(data["action"])
        except KeyError as exc:
            msg = "recovery plan is missing action"
            raise ValueError(msg) from exc
        except ValueError as exc:
            msg = (
                f"recovery plan action must be one of {[item.value for item in RecoveryPlanAction]}"
            )
            raise ValueError(msg) from exc
        differences = _string_tuple_field(data, "differences")
        suggestions = _string_tuple_field(data, "suggestions")
        return cls(
            action=action,
            safe_to_redispatch=data.get("safe_to_redispatch"),
            reason=data.get("reason"),
            qa_score=data.get("qa_score"),
            qa_verdict=data.get("qa_verdict"),
            differences=differences,
            suggestions=suggestions,
            persona=data.get("persona"),
            instruction=data.get("instruction", ""),
        )


def _string_tuple_field(data: dict[str, Any], field_name: str) -> tuple[str, ...]:
    """Return a strict tuple-of-strings field from persisted JSON."""
    raw = data.get(field_name, ())
    if not isinstance(raw, list | tuple):
        msg = f"recovery plan {field_name} must be a list of strings"
        raise ValueError(msg)
    if any(not isinstance(item, str) for item in raw):
        msg = f"recovery plan {field_name} must be a list of strings"
        raise ValueError(msg)
    return tuple(raw)


def build_manual_recovery_plan(
    *,
    qa_score: float,
    qa_verdict: str,
    differences: tuple[str, ...],
    suggestions: tuple[str, ...],
) -> AutoRecoveryPlan:
    """Build a non-dispatchable plan when no automated recovery advisor ran."""
    instruction = "; ".join(suggestions[:3]) if suggestions else "Inspect QA differences manually."
    return AutoRecoveryPlan(
        action=RecoveryPlanAction.MANUAL_INTERVENTION,
        safe_to_redispatch=False,
        reason="QA failed without an automated recovery advisor.",
        qa_score=qa_score,
        qa_verdict=qa_verdict,
        differences=tuple(differences),
        suggestions=tuple(suggestions),
        instruction=instruction,
    )


def build_lateral_recovery_plan(
    *,
    qa_score: float,
    qa_verdict: str,
    differences: tuple[str, ...],
    suggestions: tuple[str, ...],
    persona: str,
    approach_summary: str,
    lateral_text: str,
) -> AutoRecoveryPlan:
    """Build the future Ralph-redispatch artifact from lateral advice."""
    instruction_parts = []
    if approach_summary:
        instruction_parts.append(approach_summary)
    if lateral_text:
        instruction_parts.append(lateral_text)
    instruction = "\n\n".join(instruction_parts).strip()
    if not instruction:
        return AutoRecoveryPlan(
            action=RecoveryPlanAction.MANUAL_INTERVENTION,
            safe_to_redispatch=False,
            reason="QA failed but lateral recovery advice was empty.",
            qa_score=qa_score,
            qa_verdict=qa_verdict,
            differences=tuple(differences),
            suggestions=tuple(suggestions),
            persona=persona,
            instruction="Inspect QA differences manually before redispatch.",
        )
    return AutoRecoveryPlan(
        action=RecoveryPlanAction.RALPH_REDISPATCH,
        safe_to_redispatch=True,
        reason="QA failed and lateral recovery advice is available for Ralph redispatch.",
        qa_score=qa_score,
        qa_verdict=qa_verdict,
        differences=tuple(differences),
        suggestions=tuple(suggestions),
        persona=persona,
        instruction=instruction,
    )


__all__ = [
    "AutoRecoveryPlan",
    "RecoveryPlanAction",
    "build_lateral_recovery_plan",
    "build_manual_recovery_plan",
]
