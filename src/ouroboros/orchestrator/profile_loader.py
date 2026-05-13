"""Execution profile YAML loader (RFC v2 H4).

Loads structured per-domain profiles consumed by harness invariants
(verifier focus, decomposition axis, evidence schema, suggested tools).

This module is wiring-only: it defines the schema and loads YAML files.
The live runner now passes loaded profiles into `parallel_executor`
for decomposition. Evidence/verifier-loop consumption remains staged for
follow-up PRs.

Usage:
    from ouroboros.orchestrator.profile_loader import load_profile

    profile = load_profile("code")
    profile.axis        # "testable_unit"
    profile.suggested_tools  # ["Read", "Edit", ...]
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import yaml

_PROFILES_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "profiles"


class VerifierCapability(StrEnum):
    """Structured verifier envelope required by an execution profile."""

    READ_ONLY_DISCOVERY = "read_only_discovery"
    SUBPROCESS_TEST_RUNNER = "subprocess_test_runner"


class SuggestedModelTier(StrEnum):
    """Profile-declared model cost/quality hint consumed by routing H5."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


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
    schema_version: Literal[1] = Field(
        default=1,
        description="Profile schema version. v1 is the RFC #830 YAML profile shape.",
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
    max_branching: int = Field(
        default=5,
        ge=2,
        description="Maximum number of sub-ACs the H4 decomposer should emit.",
    )
    must_produce: tuple[str, ...] = Field(
        default=(),
        description="Evidence fields the leaf executor should explicitly produce.",
    )
    evidence_schema: EvidenceSchema = Field(default_factory=EvidenceSchema)
    verifier_focus: str = Field(
        ...,
        min_length=1,
        description="Instruction passed to the external verifier subagent (H1).",
    )
    verifier_capability: VerifierCapability = Field(
        ...,
        description=(
            "Structured verifier execution envelope. READ_ONLY_DISCOVERY "
            "may inspect files only; SUBPROCESS_TEST_RUNNER requires a "
            "harness-owned read-only test runner and must not be modeled as "
            "unrestricted shell access."
        ),
    )
    suggested_tools: tuple[str, ...] = Field(
        default=(),
        description="Tool names the leaf executor may use; harness still gates.",
    )
    suggested_model_tier: SuggestedModelTier = Field(
        default=SuggestedModelTier.MEDIUM,
        description="Profile-declared cost/quality hint for H5 adaptive routing.",
    )

    @model_validator(mode="after")
    def _must_produce_subset_of_required_evidence(self) -> ExecutionProfile:
        unknown = sorted(set(self.must_produce) - set(self.evidence_schema.required))
        if unknown:
            msg = (
                "must_produce entries must also appear in "
                f"evidence_schema.required: {', '.join(unknown)}"
            )
            raise ValueError(msg)
        return self


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

    # Normalize filesystem + decoding errors into ProfileError so the
    # documented contract holds — callers should never see a raw OSError
    # or UnicodeDecodeError leak from the production loader.
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Profile {name!r} could not be read from {path}: {exc}"
        raise ProfileError(msg) from exc
    except UnicodeDecodeError as exc:
        msg = f"Profile {name!r} at {path} is not valid UTF-8: {exc.reason} (byte {exc.start})"
        raise ProfileError(msg) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"Profile {name!r} is not valid YAML: {exc}"
        raise ProfileError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"Profile {name!r} must be a YAML mapping at the top level, got {type(raw).__name__}"
        raise ProfileError(msg)

    # RFC v2 structured knobs (schema_version, max_branching, must_produce,
    # suggested_model_tier) intentionally fall through to schema defaults when
    # a YAML profile omits them. Hard-failing on absence would break existing
    # out-of-tree custom profiles for no runtime safety gain at this layer —
    # the Literal[1] schema_version still rejects unsupported versions, and
    # `max_branching` keeps its ge=2 floor via Pydantic. Built-in profiles
    # declare every knob explicitly so the RFC v2 example shape stays
    # representable in-tree.
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
    "SuggestedModelTier",
    "VerifierCapability",
    "available_profiles",
    "load_profile",
]
