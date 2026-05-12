"""Execution profile YAML loader (RFC v2 H4).

Loads structured per-domain profiles consumed by harness invariants
(verifier focus, decomposition axis, evidence schema, suggested tools).

This module is wiring-only: it defines the schema and loads YAML files.
No existing caller is modified by this module — downstream PRs wire the
loaded profile into `execution_strategy`, `parallel_executor`, and the
forthcoming verifier loop.

Usage:
    from ouroboros.orchestrator.profile_loader import load_profile

    profile = load_profile("code")
    profile.axis        # "testable_unit"
    profile.suggested_tools  # ["Read", "Edit", ...]
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError
import yaml

_PROFILES_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "profiles"


class EvidenceSchema(BaseModel):
    """Schema describing required evidence fields and rejection rules.

    Consumed by H2 (typed evidence record) in a follow-up PR.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    required: tuple[str, ...] = Field(
        default=(),
        description="Evidence field names that every leaf result must include.",
    )
    rejected_if: tuple[str, ...] = Field(
        default=(),
        description=(
            "Reject expressions evaluated against the evidence record "
            "(e.g. 'tests_passed == []'). Free-form for now; the verifier "
            "PR will define an evaluator."
        ),
    )


class ExecutionProfile(BaseModel):
    """Per-domain execution profile (RFC v2 thin skill).

    See https://github.com/Q00/ouroboros/issues/830 for the v2 spec.
    Field shape matches shaun0927's H4 sketch (axis, min_unit, evidence_schema,
    verifier_focus, suggested_tools).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str = Field(
        ...,
        min_length=1,
        description="Profile identifier (e.g. 'code'). Must match filename stem.",
    )
    axis: str = Field(
        ...,
        min_length=1,
        description="Decomposition axis (e.g. 'testable_unit', 'subtopic').",
    )
    min_unit: str = Field(
        ...,
        min_length=1,
        description="Smallest dispatchable unit description for the decomposer.",
    )
    cut_signal: str = Field(
        default="",
        description="Heuristic signal that a sub-AC is small enough to stop splitting.",
    )
    evidence_schema: EvidenceSchema = Field(default_factory=EvidenceSchema)
    verifier_focus: str = Field(
        ...,
        min_length=1,
        description="Instruction passed to the external verifier subagent (H1).",
    )
    suggested_tools: tuple[str, ...] = Field(
        default=(),
        description="Tool names the leaf executor may use; harness still gates.",
    )


class ProfileError(ValueError):
    """Raised when a profile cannot be located or parsed."""


def _candidate_path(name: str, profiles_dir: Path) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        msg = f"Invalid profile name: {name!r}"
        raise ProfileError(msg)
    return profiles_dir / f"{name}.yaml"


def load_profile(name: str, *, profiles_dir: Path | None = None) -> ExecutionProfile:
    """Load and validate an execution profile by name.

    Args:
        name: Profile identifier (filename stem, e.g. 'code').
        profiles_dir: Override the default profiles directory (tests use this).

    Returns:
        Validated ExecutionProfile instance.

    Raises:
        ProfileError: If the file is missing, malformed, or violates the schema.
    """
    base = profiles_dir if profiles_dir is not None else _PROFILES_DIR
    path = _candidate_path(name, base)
    if not path.is_file():
        msg = f"Profile not found: {name!r} (looked in {base})"
        raise ProfileError(msg)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"Profile {name!r} is not valid YAML: {exc}"
        raise ProfileError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"Profile {name!r} must be a YAML mapping at the top level, got {type(raw).__name__}"
        raise ProfileError(msg)

    try:
        profile = ExecutionProfile.model_validate(raw)
    except ValidationError as exc:
        msg = f"Profile {name!r} failed schema validation: {exc}"
        raise ProfileError(msg) from exc

    if profile.profile != name:
        msg = f"Profile name mismatch: file {name!r} declares profile={profile.profile!r}"
        raise ProfileError(msg)

    return profile


def available_profiles(profiles_dir: Path | None = None) -> tuple[str, ...]:
    """Return sorted list of profile names discoverable in the profiles dir."""
    base = profiles_dir if profiles_dir is not None else _PROFILES_DIR
    if not base.is_dir():
        return ()
    return tuple(sorted(p.stem for p in base.glob("*.yaml")))


__all__ = [
    "EvidenceSchema",
    "ExecutionProfile",
    "ProfileError",
    "available_profiles",
    "load_profile",
]
