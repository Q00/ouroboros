"""Fail-closed frugality receipts for focused evolution.

Focused node counts prove that the loop did less *logical work*; they do not
prove that it spent fewer resources.  This module records measured generation
observations and compares them only when a full-graph control and a focused
treatment started from the same clean Git commit in distinct worktrees.

The control is never launched automatically.  Shadowing every production
generation would consume the savings being measured.  A normal evolve run
therefore emits an ``insufficient_data`` receipt until a deliberately isolated
comparison arm exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import logging
import math
from pathlib import Path
import subprocess
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.core.lineage import EvaluationSummary, OntologyLineage
from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.evolution.evaluation_coverage import validate_seed_ac_coverage
from ouroboros.evolution.focus import EvolutionFocus
from ouroboros.evolution.provider_usage import ProviderUsageSummary, current_summary

OBSERVATION_EVENT = "lineage.generation.frugality_observed"
PROOF_EVENT = "lineage.generation.frugality_evaluated"
ATTEMPT_DISPATCHED_EVENT = "execution.ac.attempt.dispatched"
TOKEN_ATTRIBUTION_EVENT = "execution.ac.token_attribution.reported"
DELIVER_VERDICT_EVENT = "execution.ac.deliver_verdict"
MIN_TOKEN_REDUCTION_PCT = 10.0
_MAX_COMPARISON_OBSERVATIONS = 500
_EVIDENCE_EVENT_PAGE_SIZE = 200
_MAX_EVIDENCE_EVENTS_PER_TYPE = 2_000
_MAX_EVIDENCE_ISSUES = 8
_MAX_EVIDENCE_ISSUE_CHARS = 512
_VOLATILE_SEED_METADATA_FIELDS = frozenset(
    {"seed_id", "created_at", "interview_id", "parent_seed_id"}
)

logger = logging.getLogger(__name__)


class _LoopForFrugality(Protocol):
    event_store: Any
    config: Any

    def get_project_dir(self) -> str | None: ...


class EvolutionFrugalityArm(StrEnum):
    """Whether a receipt came from a comparable control or treatment."""

    FULL_GRAPH = "full_graph"
    FOCUSED = "focused"
    UNCONTROLLED = "uncontrolled"


class EvolutionFrugalityStatus(StrEnum):
    """Deterministic outcome of one paired generation comparison."""

    PASS = "pass"
    FAIL_NO_SAVINGS = "fail_no_savings"
    FAIL_QUALITY_REGRESSION = "fail_quality_regression"
    INSUFFICIENT_DATA = "insufficient_data"


class ProjectBaseline(BaseModel, frozen=True):
    """Starting workspace identity required for a trustworthy comparison."""

    model_config = ConfigDict(extra="forbid")

    workspace_path: str | None = None
    git_commit: str | None = None
    clean: bool = False


class EvolutionFrugalityObservation(BaseModel, frozen=True):
    """Measured resource and quality axes for one evolution generation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    receipt_id: str
    comparison_key: str
    lineage_id: str
    generation_number: int = Field(ge=2)
    execution_id: str
    arm: EvolutionFrugalityArm
    workspace_path: str | None = None
    baseline_commit: str | None = None
    baseline_clean: bool = False
    active_ac_indices: tuple[int, ...] = ()
    frozen_ac_indices: tuple[int, ...] = ()
    executed_ac_indices: tuple[int, ...] = ()
    total_node_count: int = Field(ge=0)
    evaluated_seed_fingerprint: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    token_spend: float | None = Field(default=None, ge=0)
    execution_token_spend: float | None = Field(default=None, ge=0)
    token_event_count: int = Field(default=0, ge=0)
    expected_attempt_count: int = Field(default=0, ge=0)
    attempt_identity_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    execution_configuration_fingerprint: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    execution_configuration_assignments: tuple[tuple[int, tuple[str, ...]], ...] = ()
    token_evidence_complete: bool = False
    provider_token_spend: float | None = Field(default=None, ge=0)
    provider_call_count: int = Field(default=0, ge=0)
    provider_identity_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    provider_configuration_fingerprint: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    provider_configuration_assignments: tuple[tuple[str, tuple[str, ...]], ...] = ()
    provider_evidence_complete: bool = False
    generation_token_evidence_complete: bool = False
    wall_time_seconds: float | None = Field(default=None, ge=0)
    runtime_calls: int | None = Field(default=None, ge=0)
    runtime_messages: int | None = Field(default=None, ge=0)
    retry_calls: int = Field(default=0, ge=0)
    final_approved: bool | None = None
    evaluation_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evaluation_drift_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evaluation_reward_hacking_risk: float | None = Field(default=None, ge=0.0, le=1.0)
    highest_stage_passed: int | None = Field(default=None, ge=1, le=3)
    passed_ac_indices: tuple[int, ...] = ()
    failed_ac_indices: tuple[int, ...] = ()
    ac_score_assignments: tuple[tuple[int, float | None], ...] = ()
    regressed_ac_indices: tuple[int, ...] = ()
    grounding_observations: int = Field(default=0, ge=0)
    grounding_regressions: int | None = Field(default=None, ge=0)
    grounding_evidence_complete: bool = False
    ac_evidence_complete: bool = False
    evidence_issues: tuple[str, ...] = Field(default=(), max_length=_MAX_EVIDENCE_ISSUES)

    @field_validator(
        "evaluation_score",
        "evaluation_drift_score",
        "evaluation_reward_hacking_risk",
    )
    @classmethod
    def _finite_score(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("evaluation_score must be finite")
        return value

    @field_validator("evidence_issues")
    @classmethod
    def _bounded_evidence_issues(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not issue or len(issue) > _MAX_EVIDENCE_ISSUE_CHARS for issue in value):
            raise ValueError("evidence issues must be non-empty and bounded")
        return value

    @model_validator(mode="after")
    def _complete_evidence_is_internally_consistent(self) -> EvolutionFrugalityObservation:
        ac_scores = dict(self.ac_score_assignments)
        if any(index < 0 for index, _ in self.ac_score_assignments) or any(
            score is not None and _quality_score(score) is None
            for _, score in self.ac_score_assignments
        ):
            raise ValueError("AC score evidence must be in range and finite")
        if self.ac_evidence_complete:
            reported = self.passed_ac_indices + self.failed_ac_indices
            if (
                len(reported) != self.total_node_count
                or len(set(reported)) != len(reported)
                or set(reported) != set(range(self.total_node_count))
                or len(ac_scores) != len(self.ac_score_assignments)
                or set(ac_scores) != set(range(self.total_node_count))
            ):
                raise ValueError("complete AC evidence must cover every node exactly once")
            if self.evaluated_seed_fingerprint is None:
                raise ValueError("complete AC evidence must bind the evaluated Seed")
        if self.token_evidence_complete:
            execution_assignments = dict(self.execution_configuration_assignments)
            if (
                self.expected_attempt_count <= 0
                or self.token_event_count != self.expected_attempt_count
                or _positive_finite(self.execution_token_spend) is None
                or self.attempt_identity_digest is None
                or self.execution_configuration_fingerprint is None
                or len(execution_assignments) != len(self.execution_configuration_assignments)
                or set(execution_assignments) != set(self.executed_ac_indices)
                or sum(len(values) for values in execution_assignments.values())
                != self.expected_attempt_count
                or not all(
                    values and all(_is_digest(value) for value in values)
                    for values in execution_assignments.values()
                )
            ):
                raise ValueError("complete token evidence must cover every expected attempt")
        if self.provider_evidence_complete:
            provider_assignments = dict(self.provider_configuration_assignments)
            if (
                not _provider_spend_is_complete(
                    self.provider_call_count,
                    self.provider_token_spend,
                )
                or self.provider_identity_digest is None
                or self.provider_configuration_fingerprint is None
                or len(provider_assignments) != len(self.provider_configuration_assignments)
                or sum(len(values) for values in provider_assignments.values())
                != self.provider_call_count
                or not all(
                    role.strip()
                    and role.strip().lower() != "unknown"
                    and values
                    and all(_is_digest(value) for value in values)
                    for role, values in provider_assignments.items()
                )
            ):
                raise ValueError("complete provider evidence must bind every provider call")
        if self.generation_token_evidence_complete:
            expected_total = (self.execution_token_spend or 0.0) + (
                self.provider_token_spend or 0.0
            )
            if (
                not self.token_evidence_complete
                or not self.provider_evidence_complete
                or self.token_spend is None
                or _positive_finite(self.token_spend) is None
                or not math.isclose(self.token_spend, expected_total, rel_tol=0.0, abs_tol=1e-9)
                or self.runtime_calls != self.expected_attempt_count + self.provider_call_count
            ):
                raise ValueError("complete generation token evidence must reconcile every call")
        if self.grounding_evidence_complete:
            if (
                self.expected_attempt_count <= 0
                or self.grounding_observations != self.expected_attempt_count
                or self.grounding_regressions is None
                or self.attempt_identity_digest is None
            ):
                raise ValueError("complete grounding evidence must cover every expected attempt")
        return self


class EvolutionFrugalityProof(BaseModel, frozen=True):
    """Paired verdict; PASS is impossible without resource and quality evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    status: EvolutionFrugalityStatus
    comparison_key: str
    control_receipt_id: str | None = None
    treatment_receipt_id: str | None = None
    token_reduction_pct: float | None = None
    wall_time_reduction_pct: float | None = None
    call_reduction_pct: float | None = None
    quality_vetoes: tuple[str, ...] = ()
    reason: str
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {"minimum_token_reduction_pct": MIN_TOKEN_REDUCTION_PCT}
    )


@dataclass(frozen=True, slots=True)
class EvolutionFrugalityEvidence:
    """Observation plus the best proof available after recording it."""

    observation: EvolutionFrugalityObservation
    proof: EvolutionFrugalityProof


@dataclass(frozen=True, slots=True)
class _ExecutionAxes:
    token_spend: float | None
    token_event_count: int
    expected_attempt_count: int
    attempt_identity_digest: str | None
    configuration_fingerprint: str | None
    configuration_assignments: tuple[tuple[int, tuple[str, ...]], ...]
    token_evidence_complete: bool
    retry_calls: int
    runtime_calls: int | None
    grounding_observations: int
    grounding_regressions: int | None
    grounding_evidence_complete: bool
    executed_ac_indices: tuple[int, ...]
    evidence_issues: tuple[str, ...]


def capture_project_baseline(project_dir: str | None) -> ProjectBaseline:
    """Capture a clean Git starting point without mutating the workspace."""
    if not project_dir:
        return ProjectBaseline()
    path = Path(project_dir).expanduser().resolve()
    if not path.is_dir():
        return ProjectBaseline(workspace_path=str(path))
    try:
        root = _git(path, "rev-parse", "--show-toplevel")
        commit = _git(path, "rev-parse", "HEAD")
        status = _git(path, "status", "--porcelain=v1", "--untracked-files=normal")
    except (OSError, subprocess.SubprocessError):
        return ProjectBaseline(workspace_path=str(path))
    return ProjectBaseline(workspace_path=root, git_commit=commit, clean=not status)


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def comparison_key(
    seed: Seed,
    previous_evaluation: EvaluationSummary,
    baseline: ProjectBaseline,
) -> str:
    """Hash the complete semantic generation input, excluding identity only."""
    payload = {
        "protocol": "focused-evolution-frugality/v2",
        "seed": _semantic_seed_payload(seed),
        "previous_evaluation": previous_evaluation.model_dump(mode="json"),
        "baseline_commit": baseline.git_commit,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _semantic_seed_payload(seed: Seed) -> dict[str, Any]:
    """Return every Seed field except explicitly volatile lineage identity."""
    payload = seed.to_dict()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        payload["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key not in _VOLATILE_SEED_METADATA_FIELDS
        }
    return payload


def semantic_seed_fingerprint(seed: Seed) -> str:
    """Hash the complete final semantic Seed contract for paired quality equality."""
    encoded = json.dumps(
        _semantic_seed_payload(seed),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _arm(*, focused_evolution: bool, scoped_reexecution: bool) -> EvolutionFrugalityArm:
    if focused_evolution:
        return EvolutionFrugalityArm.FOCUSED
    if not scoped_reexecution:
        return EvolutionFrugalityArm.FULL_GRAPH
    return EvolutionFrugalityArm.UNCONTROLLED


def _finite_non_negative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _provider_spend_is_complete(call_count: int, token_spend: object) -> bool:
    """Require positive usage per measured call while allowing an empty provider surface."""
    if call_count == 0:
        return _finite_non_negative(token_spend) == 0.0
    return _positive_finite(token_spend) is not None


def _positive_finite(value: object) -> float | None:
    number = _finite_non_negative(value)
    return number if number is not None and number > 0 else None


def _quality_score(value: object) -> float | None:
    number = _finite_non_negative(value)
    return number if number is not None and number <= 1.0 else None


def _nonempty_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _execution_configuration(data: dict[str, Any]) -> str | None:
    settings = {
        name: _nonempty_text(data.get(name))
        for name in (
            "model",
            "effective_model",
            "model_tier",
            "model_mode",
            "effort_level",
            "effort_mode",
            "runtime_backend",
            "llm_backend",
            "permission_mode",
            "request_authority_digest",
        )
    }
    if any(
        value is None or value.lower() == "unknown" for value in settings.values()
    ) or not _is_digest(settings["request_authority_digest"]):
        return None
    return json.dumps(
        {
            "surface": "primary_runtime",
            **settings,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _configuration_fingerprint(configurations: list[str]) -> str | None:
    if not configurations:
        return None
    encoded = json.dumps(
        sorted(set(configurations)),
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _root_configuration_assignments(
    token_configurations: dict[_AttemptKey, list[str]],
    attempt_order: tuple[_AttemptKey, ...],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    by_root: dict[int, list[str]] = {}
    for key in attempt_order:
        configurations = token_configurations.get(key, ())
        for configuration in configurations:
            fingerprint = _configuration_fingerprint([configuration])
            if fingerprint is not None:
                by_root.setdefault(key[4], []).append(fingerprint)
    return tuple(
        (root_ac_index, tuple(configurations))
        for root_ac_index, configurations in sorted(by_root.items())
    )


type _AttemptKey = tuple[str, int, str, str, int]


def _attempt_key(data: dict[str, Any]) -> _AttemptKey | None:
    ac_id = data.get("ac_id")
    retry_attempt = data.get("retry_attempt")
    session_attempt_id = data.get("session_attempt_id")
    ac_dispatch_id = data.get("ac_dispatch_id")
    root_ac_index = data.get("root_ac_index")
    if (
        not isinstance(ac_id, str)
        or not ac_id.strip()
        or isinstance(retry_attempt, bool)
        or not isinstance(retry_attempt, int)
        or retry_attempt < 0
        or not isinstance(session_attempt_id, str)
        or not session_attempt_id.strip()
        or not isinstance(ac_dispatch_id, str)
        or len(ac_dispatch_id) != 32
        or any(char not in "0123456789abcdef" for char in ac_dispatch_id)
        or isinstance(root_ac_index, bool)
        or not isinstance(root_ac_index, int)
        or root_ac_index < 0
    ):
        return None
    return (
        ac_id.strip(),
        retry_attempt,
        session_attempt_id.strip(),
        ac_dispatch_id,
        root_ac_index,
    )


def _execution_axes(events: list[BaseEvent], *, execution_id: str) -> _ExecutionAxes:
    expected_counts: dict[_AttemptKey, int] = {}
    dispatch_authorities: dict[_AttemptKey, str] = {}
    token_values: dict[_AttemptKey, list[float]] = {}
    token_configurations: dict[_AttemptKey, list[str]] = {}
    token_authorities: dict[_AttemptKey, str] = {}
    grounding_values: dict[_AttemptKey, list[bool]] = {}
    invalid_dispatch = False
    invalid_token = False
    invalid_grounding = False

    for event in events:
        if event.type not in {
            ATTEMPT_DISPATCHED_EVENT,
            TOKEN_ATTRIBUTION_EVENT,
            DELIVER_VERDICT_EVENT,
        }:
            continue
        data = event.data
        if data.get("execution_id") != execution_id:
            if event.type != ATTEMPT_DISPATCHED_EVENT or data.get("dispatch_kind") == "primary":
                if event.type == TOKEN_ATTRIBUTION_EVENT:
                    invalid_token = True
                elif event.type == DELIVER_VERDICT_EVENT:
                    invalid_grounding = True
                else:
                    invalid_dispatch = True
            continue
        key = _attempt_key(data)

        if event.type == ATTEMPT_DISPATCHED_EVENT:
            dispatch_kind = data.get("dispatch_kind")
            if dispatch_kind == "session_signal_followup":
                invalid_dispatch = True
                continue
            if dispatch_kind != "primary" or key is None:
                invalid_dispatch = True
                continue
            request_authority_digest = data.get("request_authority_digest")
            if not _is_digest(request_authority_digest):
                invalid_dispatch = True
                continue
            expected_counts[key] = expected_counts.get(key, 0) + 1
            dispatch_authorities[key] = request_authority_digest
            continue

        if key is None:
            if event.type == TOKEN_ATTRIBUTION_EVENT:
                invalid_token = True
            else:
                invalid_grounding = True
            continue

        if event.type == TOKEN_ATTRIBUTION_EVENT:
            spend = _positive_finite(data.get("token_spend"))
            configuration = _execution_configuration(data)
            if (
                spend is None
                or data.get("token_source") != "runtime_usage"
                or configuration is None
            ):
                invalid_token = True
                continue
            token_values.setdefault(key, []).append(spend)
            token_configurations.setdefault(key, []).append(configuration)
            token_authorities[key] = data["request_authority_digest"]
            continue

        verdict = data.get("traceguard_verdict")
        claim_rate = _finite_non_negative(data.get("unsupported_claim_rate"))
        regression = data.get("grounding_regression")
        consistent = (verdict == "accepted" and regression is False and claim_rate == 0.0) or (
            verdict == "rejected"
            and regression is True
            and claim_rate is not None
            and 0.0 <= claim_rate <= 1.0
        )
        if not consistent:
            invalid_grounding = True
            continue
        grounding_values.setdefault(key, []).append(regression)

    duplicate_dispatch = any(count != 1 for count in expected_counts.values())
    attempt_order = tuple(expected_counts)
    expected = set(expected_counts)
    token_keys = set(token_values)
    grounding_keys = set(grounding_values)
    duplicate_tokens = any(len(values) != 1 for values in token_values.values())
    duplicate_configurations = any(len(values) != 1 for values in token_configurations.values())
    duplicate_grounding = any(len(values) != 1 for values in grounding_values.values())
    token_complete = bool(expected) and not any(
        (
            invalid_dispatch,
            invalid_token,
            duplicate_dispatch,
            duplicate_tokens,
            duplicate_configurations,
            token_keys != expected,
            set(token_configurations) != expected,
            token_authorities != dispatch_authorities,
        )
    )
    grounding_complete = bool(expected) and not any(
        (
            invalid_dispatch,
            invalid_grounding,
            duplicate_dispatch,
            duplicate_grounding,
            grounding_keys != expected,
        )
    )
    token_spend: float | None = None
    aggregate_token_invalid = False
    if token_complete:
        token_spend = _positive_finite(sum(values[0] for values in token_values.values()))
        aggregate_token_invalid = token_spend is None
        if aggregate_token_invalid:
            token_complete = False

    issues: list[str] = []
    if not expected:
        issues.append("no authoritative primary attempt dispatches")
    if invalid_dispatch or duplicate_dispatch:
        issues.append("attempt dispatch identity is malformed or duplicated")
    if not token_complete:
        issues.append(
            "runtime token/configuration attribution does not exactly cover expected attempts"
        )
    if aggregate_token_invalid:
        issues.append("aggregate primary runtime-token usage is non-finite")
    if not grounding_complete:
        issues.append("TraceGuard evidence does not exactly cover expected attempts")

    identities = [
        {
            "ac_id": ac_id,
            "retry_attempt": attempt,
            "session_attempt_id": session_attempt_id,
            "ac_dispatch_id": ac_dispatch_id,
            "root_ac_index": root_ac_index,
        }
        for ac_id, attempt, session_attempt_id, ac_dispatch_id, root_ac_index in sorted(expected)
    ]
    identity_digest = None
    if identities and not invalid_dispatch and not duplicate_dispatch:
        encoded = json.dumps(identities, separators=(",", ":"), ensure_ascii=True)
        identity_digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

    grounding_regressions = (
        sum(int(values[0]) for values in grounding_values.values()) if grounding_complete else None
    )
    return _ExecutionAxes(
        token_spend=token_spend,
        token_event_count=sum(len(values) for values in token_values.values()),
        expected_attempt_count=len(expected),
        attempt_identity_digest=identity_digest,
        configuration_fingerprint=(
            _configuration_fingerprint([values[0] for values in token_configurations.values()])
            if token_complete
            else None
        ),
        configuration_assignments=(
            _root_configuration_assignments(token_configurations, attempt_order)
            if token_complete
            else ()
        ),
        token_evidence_complete=token_complete,
        retry_calls=sum(int(key[1] > 0) for key in expected),
        runtime_calls=len(expected) if expected else None,
        grounding_observations=sum(len(values) for values in grounding_values.values()),
        grounding_regressions=grounding_regressions,
        grounding_evidence_complete=grounding_complete,
        executed_ac_indices=tuple(sorted({key[4] for key in expected})),
        evidence_issues=tuple(issues),
    )


def build_observation(
    *,
    lineage_id: str,
    generation_number: int,
    execution_id: str,
    parent_seed: Seed,
    current_seed: Seed,
    previous_evaluation: EvaluationSummary,
    current_evaluation: EvaluationSummary | None,
    focus: EvolutionFocus,
    baseline: ProjectBaseline,
    focused_evolution: bool,
    scoped_reexecution: bool,
    execution_events: list[BaseEvent],
    executor_result: Any | None,
    provider_usage: ProviderUsageSummary | None = None,
    evidence_collection_complete: bool = True,
) -> EvolutionFrugalityObservation:
    """Build one deterministic observation from runtime telemetry."""
    axes = _execution_axes(execution_events, execution_id=execution_id)
    wall_time = _finite_non_negative(getattr(executor_result, "duration_seconds", None))
    runtime_messages = getattr(executor_result, "messages_processed", None)
    if (
        isinstance(runtime_messages, bool)
        or not isinstance(runtime_messages, int)
        or runtime_messages < 0
    ):
        runtime_messages = None
    ac_results = current_evaluation.ac_results if current_evaluation is not None else ()
    evaluation_score = _quality_score(current_evaluation.score) if current_evaluation else None
    evaluation_drift_score = (
        _quality_score(current_evaluation.drift_score) if current_evaluation else None
    )
    evaluation_reward_hacking_risk = (
        _quality_score(current_evaluation.reward_hacking_risk) if current_evaluation else None
    )
    previous_passes = {
        result.ac_index for result in previous_evaluation.ac_results if result.passed
    }
    current_passes = {result.ac_index for result in ac_results if result.passed}
    coverage = (
        validate_seed_ac_coverage(current_seed, current_evaluation)
        if current_evaluation is not None
        else None
    )
    evidence_issues = list(axes.evidence_issues)
    if not evidence_collection_complete:
        evidence_issues.append("execution evidence exceeded the bounded collection limit")
    if axes.executed_ac_indices != focus.active_ac_indices:
        evidence_issues.append(
            "primary dispatch roots do not exactly cover the active AC working set"
        )
    if provider_usage is None:
        evidence_issues.append("generation provider-call evidence is missing")
        provider_usage = ProviderUsageSummary(
            call_count=0,
            token_spend=None,
            identity_digest=None,
            configuration_fingerprint=None,
            configuration_assignments=(),
            complete=False,
        )
    evidence_issues.extend(provider_usage.issues)
    if coverage is None:
        evidence_issues.append("final Seed-wide AC evaluation is missing")
    elif not coverage.complete:
        evidence_issues.append(coverage.reason[:_MAX_EVIDENCE_ISSUE_CHARS])
    if current_evaluation is not None:
        raw_quality_values = (
            current_evaluation.score,
            current_evaluation.drift_score,
            current_evaluation.reward_hacking_risk,
            *(result.score for result in ac_results),
        )
        if any(value is not None and _quality_score(value) is None for value in raw_quality_values):
            evidence_issues.append("evaluation quality metrics are malformed or out of range")
    generation_token_spend: float | None = None
    generation_token_evidence_complete = False
    if (
        axes.token_evidence_complete
        and provider_usage.complete
        and axes.token_spend is not None
        and provider_usage.token_spend is not None
    ):
        generation_token_spend = _positive_finite(axes.token_spend + provider_usage.token_spend)
        generation_token_evidence_complete = generation_token_spend is not None
        if not generation_token_evidence_complete:
            evidence_issues.append("aggregate total-generation runtime-token usage is non-finite")
    key = comparison_key(parent_seed, previous_evaluation, baseline)
    return EvolutionFrugalityObservation(
        receipt_id=f"{lineage_id}:generation:{generation_number}",
        comparison_key=key,
        lineage_id=lineage_id,
        generation_number=generation_number,
        execution_id=execution_id,
        arm=_arm(
            focused_evolution=focused_evolution,
            scoped_reexecution=scoped_reexecution,
        ),
        workspace_path=baseline.workspace_path,
        baseline_commit=baseline.git_commit,
        baseline_clean=baseline.clean,
        active_ac_indices=focus.active_ac_indices,
        frozen_ac_indices=focus.frozen_ac_indices,
        executed_ac_indices=axes.executed_ac_indices,
        total_node_count=len(current_seed.acceptance_criteria),
        evaluated_seed_fingerprint=semantic_seed_fingerprint(current_seed),
        token_spend=generation_token_spend,
        execution_token_spend=axes.token_spend,
        token_event_count=axes.token_event_count,
        expected_attempt_count=axes.expected_attempt_count,
        attempt_identity_digest=axes.attempt_identity_digest,
        execution_configuration_fingerprint=axes.configuration_fingerprint,
        execution_configuration_assignments=axes.configuration_assignments,
        token_evidence_complete=axes.token_evidence_complete,
        provider_token_spend=provider_usage.token_spend,
        provider_call_count=provider_usage.call_count,
        provider_identity_digest=provider_usage.identity_digest,
        provider_configuration_fingerprint=provider_usage.configuration_fingerprint,
        provider_configuration_assignments=provider_usage.configuration_assignments,
        provider_evidence_complete=provider_usage.complete,
        generation_token_evidence_complete=generation_token_evidence_complete,
        wall_time_seconds=wall_time,
        runtime_calls=(
            axes.expected_attempt_count + provider_usage.call_count
            if axes.expected_attempt_count and provider_usage.complete
            else None
        ),
        runtime_messages=runtime_messages,
        retry_calls=axes.retry_calls,
        final_approved=(current_evaluation.final_approved if current_evaluation else None),
        evaluation_score=evaluation_score,
        evaluation_drift_score=evaluation_drift_score,
        evaluation_reward_hacking_risk=evaluation_reward_hacking_risk,
        highest_stage_passed=(
            current_evaluation.highest_stage_passed if current_evaluation else None
        ),
        passed_ac_indices=tuple(sorted(result.ac_index for result in ac_results if result.passed)),
        failed_ac_indices=tuple(
            sorted(result.ac_index for result in ac_results if not result.passed)
        ),
        ac_score_assignments=tuple(
            sorted((result.ac_index, _quality_score(result.score)) for result in ac_results)
        ),
        regressed_ac_indices=tuple(sorted(previous_passes - current_passes)),
        grounding_observations=axes.grounding_observations,
        grounding_regressions=axes.grounding_regressions,
        grounding_evidence_complete=axes.grounding_evidence_complete,
        ac_evidence_complete=bool(coverage and coverage.complete),
        evidence_issues=tuple(evidence_issues[:_MAX_EVIDENCE_ISSUES]),
    )


def _reduction(control: float | int | None, treatment: float | int | None) -> float | None:
    if control is None or treatment is None or control <= 0:
        return None
    # Threshold authority uses the unrounded measurement. Rounding before the
    # comparison could promote 9.99996% to a false 10% PASS.
    return (float(control) - float(treatment)) / float(control) * 100.0


def _ac_evidence_issue(observation: EvolutionFrugalityObservation) -> str | None:
    reported = observation.passed_ac_indices + observation.failed_ac_indices
    ac_scores = dict(observation.ac_score_assignments)
    if not observation.ac_evidence_complete:
        return "Seed-wide final AC verdict coverage is incomplete"
    if (
        len(reported) != observation.total_node_count
        or len(set(reported)) != len(reported)
        or set(reported) != set(range(observation.total_node_count))
        or len(ac_scores) != len(observation.ac_score_assignments)
        or set(ac_scores) != set(range(observation.total_node_count))
    ):
        return "Seed-wide final AC verdict coverage is inconsistent"
    return None


def _focus_evidence_issue(observation: EvolutionFrugalityObservation) -> str | None:
    active = observation.active_ac_indices
    frozen = observation.frozen_ac_indices
    expected = set(range(observation.total_node_count))
    if observation.arm is EvolutionFrugalityArm.FULL_GRAPH and (
        frozen or active != tuple(range(observation.total_node_count))
    ):
        return "full_graph control did not execute the complete Seed graph"
    if (
        len(active) != len(set(active))
        or len(frozen) != len(set(frozen))
        or set(active) & set(frozen)
        or set(active) | set(frozen) != expected
    ):
        return "active/frozen working set is not an exact Seed partition"
    return None


def _runtime_evidence_issue(observation: EvolutionFrugalityObservation) -> str | None:
    execution_assignments = dict(observation.execution_configuration_assignments)
    provider_assignments = dict(observation.provider_configuration_assignments)
    if observation.evidence_issues:
        return "; ".join(observation.evidence_issues)
    if (
        observation.expected_attempt_count <= 0
        or observation.attempt_identity_digest is None
        or observation.execution_configuration_fingerprint is None
        or set(execution_assignments) != set(observation.executed_ac_indices)
        or sum(len(values) for values in execution_assignments.values())
        != observation.expected_attempt_count
        or observation.executed_ac_indices != observation.active_ac_indices
    ):
        return "authoritative runtime attempt identity is incomplete"
    if (
        not observation.token_evidence_complete
        or observation.token_event_count != observation.expected_attempt_count
        or _positive_finite(observation.execution_token_spend) is None
    ):
        return "runtime token attribution is incomplete"
    if (
        not observation.provider_evidence_complete
        or observation.provider_identity_digest is None
        or observation.provider_configuration_fingerprint is None
        or sum(len(values) for values in provider_assignments.values())
        != observation.provider_call_count
        or any(
            not role.strip() or role.strip().lower() == "unknown" for role in provider_assignments
        )
        or not _provider_spend_is_complete(
            observation.provider_call_count,
            observation.provider_token_spend,
        )
    ):
        return "generation provider-call token attribution is incomplete"
    if (
        not observation.generation_token_evidence_complete
        or _positive_finite(observation.token_spend) is None
        or observation.runtime_calls
        != observation.expected_attempt_count + observation.provider_call_count
    ):
        return "total generation token attribution does not reconcile"
    if (
        not observation.grounding_evidence_complete
        or observation.grounding_observations != observation.expected_attempt_count
        or observation.grounding_regressions is None
    ):
        return "TraceGuard grounding evidence is incomplete"
    return None


def evaluate_pair(
    control: EvolutionFrugalityObservation,
    treatment: EvolutionFrugalityObservation,
) -> EvolutionFrugalityProof:
    """Evaluate an isolated full-graph/focused pair with quality veto first."""
    key = treatment.comparison_key
    if control.comparison_key != key:
        return _insufficient(key, treatment=treatment, reason="comparison inputs differ")
    if control.arm is not EvolutionFrugalityArm.FULL_GRAPH:
        return _insufficient(key, treatment=treatment, reason="control arm is not full_graph")
    if treatment.arm is not EvolutionFrugalityArm.FOCUSED:
        return _insufficient(key, control=control, reason="treatment arm is not focused")
    if (
        not control.baseline_clean
        or not treatment.baseline_clean
        or not control.baseline_commit
        or control.baseline_commit != treatment.baseline_commit
        or not control.workspace_path
        or not treatment.workspace_path
        or control.workspace_path == treatment.workspace_path
    ):
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired proof requires distinct clean worktrees at the same Git commit",
        )
    if not treatment.frozen_ac_indices:
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="focused arm excluded no nodes",
        )

    for label, observation in (("control", control), ("treatment", treatment)):
        issue = _focus_evidence_issue(observation)
        if issue is not None:
            return _insufficient(
                key,
                control=control,
                treatment=treatment,
                reason=f"{label} {issue}",
            )
        issue = _ac_evidence_issue(observation)
        if issue is not None:
            return _insufficient(
                key,
                control=control,
                treatment=treatment,
                reason=f"{label} {issue}",
            )
        issue = _runtime_evidence_issue(observation)
        if issue is not None:
            return _insufficient(
                key,
                control=control,
                treatment=treatment,
                reason=f"{label} {issue}",
            )
    control_root_configurations = dict(control.execution_configuration_assignments)
    treatment_root_configurations = dict(treatment.execution_configuration_assignments)
    if any(
        control_root_configurations.get(root_ac_index) != configurations
        for root_ac_index, configurations in treatment_root_configurations.items()
    ):
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired primary runtime configurations differ for an active root AC",
        )
    control_role_configurations = dict(control.provider_configuration_assignments)
    treatment_role_configurations = dict(treatment.provider_configuration_assignments)
    if any(
        control_role_configurations.get(role) != configurations
        for role, configurations in treatment_role_configurations.items()
    ):
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired auxiliary provider/runtime configurations differ by role",
        )
    if (
        not control.evaluated_seed_fingerprint
        or control.evaluated_seed_fingerprint != treatment.evaluated_seed_fingerprint
    ):
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired final semantic Seed contracts differ",
        )
    if control.total_node_count != treatment.total_node_count:
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired final AC contracts differ in cardinality",
        )

    vetoes: list[str] = []
    if control.final_approved is None or treatment.final_approved is None:
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired final-gate evaluations are required",
        )
    if control.final_approved and not treatment.final_approved:
        vetoes.append("final approval regressed")
    quality_metrics = (
        ("evaluation score", control.evaluation_score, treatment.evaluation_score, False),
        (
            "evaluation drift",
            control.evaluation_drift_score,
            treatment.evaluation_drift_score,
            True,
        ),
        (
            "reward-hacking risk",
            control.evaluation_reward_hacking_risk,
            treatment.evaluation_reward_hacking_risk,
            True,
        ),
    )
    for label, control_value, treatment_value, higher_is_worse in quality_metrics:
        if (control_value is None) != (treatment_value is None):
            return _insufficient(
                key,
                control=control,
                treatment=treatment,
                reason=f"paired {label} evidence is not comparable",
            )
        if control_value is None or treatment_value is None:
            continue
        regressed = (
            treatment_value > control_value if higher_is_worse else treatment_value < control_value
        )
        if regressed:
            vetoes.append(f"{label} regressed from {control_value:.6f} to {treatment_value:.6f}")
    if control.highest_stage_passed is None or treatment.highest_stage_passed is None:
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired evaluation-stage evidence is required",
        )
    if treatment.highest_stage_passed < control.highest_stage_passed:
        vetoes.append(
            "highest evaluation stage regressed from "
            f"{control.highest_stage_passed} to {treatment.highest_stage_passed}"
        )
    control_ac_scores = dict(control.ac_score_assignments)
    treatment_ac_scores = dict(treatment.ac_score_assignments)
    for ac_index in range(control.total_node_count):
        control_score = control_ac_scores[ac_index]
        treatment_score = treatment_ac_scores[ac_index]
        if (control_score is None) != (treatment_score is None):
            return _insufficient(
                key,
                control=control,
                treatment=treatment,
                reason=f"paired AC {ac_index} score evidence is not comparable",
            )
        if (
            control_score is not None
            and treatment_score is not None
            and treatment_score < control_score
        ):
            vetoes.append(
                f"AC {ac_index} score regressed from {control_score:.6f} to {treatment_score:.6f}"
            )
    missing_passes = set(control.passed_ac_indices) - set(treatment.passed_ac_indices)
    if missing_passes:
        vetoes.append(f"previously passing ACs regressed: {sorted(missing_passes)}")
    if treatment.regressed_ac_indices:
        vetoes.append(
            f"treatment lineage reports regressions: {list(treatment.regressed_ac_indices)}"
        )
    if treatment.grounding_regressions:
        vetoes.append(f"grounding regressed on {treatment.grounding_regressions} runtime call(s)")
    if vetoes:
        return EvolutionFrugalityProof(
            status=EvolutionFrugalityStatus.FAIL_QUALITY_REGRESSION,
            comparison_key=key,
            control_receipt_id=control.receipt_id,
            treatment_receipt_id=treatment.receipt_id,
            quality_vetoes=tuple(vetoes),
            reason="Focused evolution reduced quality; resource savings are vetoed.",
        )
    token_reduction = _reduction(control.token_spend, treatment.token_spend)
    if token_reduction is None:
        return _insufficient(
            key,
            control=control,
            treatment=treatment,
            reason="paired runtime token attribution is required",
        )
    wall_reduction = _reduction(control.wall_time_seconds, treatment.wall_time_seconds)
    call_reduction = _reduction(control.runtime_calls, treatment.runtime_calls)
    status = (
        EvolutionFrugalityStatus.PASS
        if token_reduction >= MIN_TOKEN_REDUCTION_PCT
        else EvolutionFrugalityStatus.FAIL_NO_SAVINGS
    )
    return EvolutionFrugalityProof(
        status=status,
        comparison_key=key,
        control_receipt_id=control.receipt_id,
        treatment_receipt_id=treatment.receipt_id,
        token_reduction_pct=token_reduction,
        wall_time_reduction_pct=wall_reduction,
        call_reduction_pct=call_reduction,
        reason=(
            f"Paired focused evolution saved {token_reduction:.6f}% runtime tokens "
            "with no measured quality regression."
            if status is EvolutionFrugalityStatus.PASS
            else (
                f"Paired focused evolution saved only {token_reduction:.6f}% runtime tokens; "
                f"the proof threshold is {MIN_TOKEN_REDUCTION_PCT:.2f}%."
            )
        ),
    )


def _insufficient(
    key: str,
    *,
    control: EvolutionFrugalityObservation | None = None,
    treatment: EvolutionFrugalityObservation | None = None,
    reason: str,
) -> EvolutionFrugalityProof:
    return EvolutionFrugalityProof(
        status=EvolutionFrugalityStatus.INSUFFICIENT_DATA,
        comparison_key=key,
        control_receipt_id=control.receipt_id if control else None,
        treatment_receipt_id=treatment.receipt_id if treatment else None,
        reason=reason,
    )


def evaluate_observations(
    current: EvolutionFrugalityObservation,
    observations: list[EvolutionFrugalityObservation],
) -> EvolutionFrugalityProof:
    """Pair the current receipt with the newest matching opposite arm."""
    if current.arm is EvolutionFrugalityArm.UNCONTROLLED:
        return _insufficient(
            current.comparison_key,
            reason="legacy scoped reexecution is not a full-graph control",
        )
    opposite = (
        EvolutionFrugalityArm.FULL_GRAPH
        if current.arm is EvolutionFrugalityArm.FOCUSED
        else EvolutionFrugalityArm.FOCUSED
    )
    candidates = [
        item
        for item in observations
        if item.comparison_key == current.comparison_key
        and item.arm is opposite
        and item.receipt_id != current.receipt_id
    ]
    if not candidates:
        return _insufficient(
            current.comparison_key,
            control=(current if current.arm is EvolutionFrugalityArm.FULL_GRAPH else None),
            treatment=(current if current.arm is EvolutionFrugalityArm.FOCUSED else None),
            reason="matching isolated full-graph/focused receipt not found",
        )
    match = candidates[0]
    control = current if current.arm is EvolutionFrugalityArm.FULL_GRAPH else match
    treatment = current if current.arm is EvolutionFrugalityArm.FOCUSED else match
    return evaluate_pair(control, treatment)


async def _record_generation(
    *,
    event_store: Any,
    lineage_id: str,
    generation_number: int,
    execution_id: str,
    parent_seed: Seed,
    current_seed: Seed,
    previous_evaluation: EvaluationSummary,
    current_evaluation: EvaluationSummary | None,
    focus: EvolutionFocus,
    baseline: ProjectBaseline,
    focused_evolution: bool,
    scoped_reexecution: bool,
    executor_result: Any | None,
    provider_usage: ProviderUsageSummary | None,
) -> EvolutionFrugalityEvidence:
    """Persist an observation and its fail-closed paired verdict."""
    provider_usage = provider_usage or current_summary()
    execution_events, evidence_collection_complete = await query_execution_evidence_events(
        event_store,
        execution_id,
    )
    observation = build_observation(
        lineage_id=lineage_id,
        generation_number=generation_number,
        execution_id=execution_id,
        parent_seed=parent_seed,
        current_seed=current_seed,
        previous_evaluation=previous_evaluation,
        current_evaluation=current_evaluation,
        focus=focus,
        baseline=baseline,
        focused_evolution=focused_evolution,
        scoped_reexecution=scoped_reexecution,
        execution_events=execution_events,
        executor_result=executor_result,
        provider_usage=provider_usage,
        evidence_collection_complete=evidence_collection_complete,
    )
    await event_store.append(
        BaseEvent(
            type=OBSERVATION_EVENT,
            aggregate_type="lineage",
            aggregate_id=lineage_id,
            data=observation.model_dump(mode="json"),
        )
    )
    raw_observations = await event_store.query_events(
        event_type=OBSERVATION_EVENT,
        aggregate_type="lineage",
        limit=_MAX_COMPARISON_OBSERVATIONS,
    )
    observations: list[EvolutionFrugalityObservation] = []
    for event in raw_observations:
        try:
            observations.append(EvolutionFrugalityObservation.model_validate(event.data))
        except (TypeError, ValueError):
            continue
    proof = evaluate_observations(observation, observations)
    await event_store.append(
        BaseEvent(
            type=PROOF_EVENT,
            aggregate_type="lineage",
            aggregate_id=lineage_id,
            data={
                "generation_number": generation_number,
                "receipt_id": observation.receipt_id,
                "proof": proof.model_dump(mode="json"),
            },
        )
    )
    return EvolutionFrugalityEvidence(observation=observation, proof=proof)


async def query_execution_evidence_events(
    event_store: Any,
    execution_id: str,
) -> tuple[list[BaseEvent], bool]:
    """Read only proof-relevant events through bounded, stable keyset pages."""
    events: list[BaseEvent] = []
    complete = True
    for event_type in (
        ATTEMPT_DISPATCHED_EVENT,
        TOKEN_ATTRIBUTION_EVENT,
        DELIVER_VERDICT_EVENT,
    ):
        latest = await event_store.query_execution_related_events(
            execution_id,
            event_type=event_type,
            limit=1,
        )
        if not latest:
            continue
        through = (latest[0].timestamp, latest[0].id)
        cursor = None
        event_type_count = 0
        while True:
            remaining = _MAX_EVIDENCE_EVENTS_PER_TYPE - event_type_count
            if remaining <= 0:
                overflow = await event_store.query_execution_related_events(
                    execution_id,
                    event_type=event_type,
                    limit=1,
                    chronological=True,
                    cursor_after=cursor,
                    cursor_through=through,
                )
                complete = complete and not overflow
                break
            page_limit = min(_EVIDENCE_EVENT_PAGE_SIZE, remaining)
            page = await event_store.query_execution_related_events(
                execution_id,
                event_type=event_type,
                limit=page_limit,
                chronological=True,
                cursor_after=cursor,
                cursor_through=through,
            )
            events.extend(page)
            event_type_count += len(page)
            if len(page) < page_limit:
                break
            cursor = (page[-1].timestamp, page[-1].id)
    return events, complete


async def record_generation(
    *,
    event_store: Any,
    lineage_id: str,
    generation_number: int,
    execution_id: str,
    parent_seed: Seed,
    current_seed: Seed,
    previous_evaluation: EvaluationSummary | None,
    current_evaluation: EvaluationSummary | None,
    focus: EvolutionFocus,
    baseline: ProjectBaseline,
    focused_evolution: bool,
    scoped_reexecution: bool,
    executor_result: Any | None,
    provider_usage: ProviderUsageSummary | None = None,
) -> EvolutionFrugalityEvidence | None:
    """Record evidence without letting optional telemetry fail evolution."""
    if generation_number < 2 or previous_evaluation is None:
        return None
    try:
        return await _record_generation(
            event_store=event_store,
            lineage_id=lineage_id,
            generation_number=generation_number,
            execution_id=execution_id,
            parent_seed=parent_seed,
            current_seed=current_seed,
            previous_evaluation=previous_evaluation,
            current_evaluation=current_evaluation,
            focus=focus,
            baseline=baseline,
            focused_evolution=focused_evolution,
            scoped_reexecution=scoped_reexecution,
            executor_result=executor_result,
            provider_usage=provider_usage,
        )
    except Exception:  # noqa: BLE001 - proof is observe-only and fails closed
        logger.warning(
            "evolution.frugality.receipt_failed",
            extra={"lineage_id": lineage_id, "generation": generation_number},
            exc_info=True,
        )
        return None


async def record_wonder_stop(
    loop: _LoopForFrugality,
    lineage: OntologyLineage,
    context: tuple[int, Seed, EvolutionFocus, Any],
) -> EvolutionFrugalityEvidence | None:
    """Persist a non-PASS receipt when Wonder ends before execution."""
    (
        generation_number,
        current_seed,
        generation_focus,
        execution_policy,
    ) = context
    previous = next(
        (
            generation
            for generation in reversed(lineage.generations)
            if getattr(generation, "evaluation_summary", None) is not None
        ),
        None,
    )
    logger.info("evolution.wonder.nothing_to_learn")
    return await record_generation(
        event_store=loop.event_store,
        lineage_id=lineage.lineage_id,
        generation_number=generation_number,
        execution_id=f"evolve:{lineage.lineage_id}:generation:{generation_number}",
        parent_seed=current_seed,
        current_seed=current_seed,
        previous_evaluation=(previous.evaluation_summary if previous is not None else None),
        current_evaluation=None,
        focus=generation_focus,
        baseline=capture_project_baseline(loop.get_project_dir()),
        focused_evolution=execution_policy.focused_evolution,
        scoped_reexecution=execution_policy.scoped_reexecution,
        executor_result=None,
    )


__all__ = [
    "MIN_TOKEN_REDUCTION_PCT",
    "OBSERVATION_EVENT",
    "PROOF_EVENT",
    "EvolutionFrugalityArm",
    "EvolutionFrugalityEvidence",
    "EvolutionFrugalityObservation",
    "EvolutionFrugalityProof",
    "EvolutionFrugalityStatus",
    "ProjectBaseline",
    "build_observation",
    "capture_project_baseline",
    "comparison_key",
    "evaluate_observations",
    "evaluate_pair",
    "record_generation",
    "record_wonder_stop",
    "query_execution_evidence_events",
    "semantic_seed_fingerprint",
]
