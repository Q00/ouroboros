"""Persistent state for full-quality ``ooo auto`` sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


class AutoPhase(StrEnum):
    """Closed set of phases for auto-mode resume and stall handling."""

    CREATED = "created"
    INTERVIEW = "interview"
    SEED_GENERATION = "seed_generation"
    REVIEW = "review"
    REPAIR = "repair"
    RUN = "run"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"


class AutoPolicy(StrEnum):
    """Supported auto-mode resolution policies."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"


TERMINAL_PHASES = {AutoPhase.COMPLETE, AutoPhase.BLOCKED, AutoPhase.FAILED}


class ResumeCapability(StrEnum):
    """How truthful is ``ooo auto --resume`` for the current state.

    The CLI used to suggest the same ``Resume:`` hint regardless of whether
    the persisted state actually carries enough handle to continue. For
    example, an ``interview.start`` timeout with ``interview_session_id=None``
    means ``--resume`` will *retry* the start call from scratch, not continue
    a half-completed interview. This enum lets the CLI distinguish the cases.
    """

    RESUME = "resume"  # has a continuation handle
    RETRY = "retry"  # blocked but no handle — re-attempt the same step
    UNAVAILABLE = "unavailable"  # complete or unrecoverable


_RETRY_TOOL_NAMES = {
    "interview.start",
    "interview.prepare",
    "auto_pipeline",
}


def resume_capability_for_state(state: AutoPipelineState) -> ResumeCapability:
    """Classify how ``ooo auto --resume`` will behave for ``state``.

    The capability is derived only from durable state so the CLI hint stays
    consistent between ``--status`` and a freshly-finished run. Rules:

    * ``COMPLETE`` → ``UNAVAILABLE`` — nothing to resume.
    * Run-handoff was attempted but no durable tracking handle was captured
      (``run_start_attempted`` is True and all of ``job_id`` /
      ``execution_id`` / ``run_session_id`` are None) → ``UNAVAILABLE``.
      ``AutoPipeline.run`` explicitly refuses to start another execution in
      this case, so advertising ``Resume:`` would lie. (Bot-flagged in #714
      review.)
    * Any other persisted continuation handle (``interview_session_id``,
      ``pending_question``, ``seed_artifact``/``seed_path``,
      ``execution_id``, ``job_id``, ``run_session_id``) → ``RESUME``.
    * Otherwise, blocked/failed (or in-progress without a handle) → ``RETRY``.
    """
    if state.phase == AutoPhase.COMPLETE:
        return ResumeCapability.UNAVAILABLE

    # Unknown run handoff: pipeline refuses to retry the run automatically to
    # avoid duplicate execution. Resume cannot continue, so we MUST NOT
    # advertise it as RESUME even though a Seed artifact is persisted.
    has_run_handle = bool(state.job_id or state.execution_id or state.run_session_id)
    if (
        state.phase in {AutoPhase.BLOCKED, AutoPhase.FAILED}
        and state.run_start_attempted
        and not has_run_handle
    ):
        return ResumeCapability.UNAVAILABLE

    if (
        state.interview_session_id
        or state.pending_question
        or state.seed_artifact
        or state.seed_path
        or state.execution_id
        or state.job_id
        or state.run_session_id
    ):
        return ResumeCapability.RESUME
    if state.phase in {AutoPhase.BLOCKED, AutoPhase.FAILED}:
        if state.last_tool_name in _RETRY_TOOL_NAMES or state.last_tool_name is None:
            return ResumeCapability.RETRY
    return ResumeCapability.RETRY


_ALLOWED_TRANSITIONS: dict[AutoPhase, set[AutoPhase]] = {
    AutoPhase.CREATED: {AutoPhase.INTERVIEW, AutoPhase.BLOCKED, AutoPhase.FAILED},
    AutoPhase.INTERVIEW: {
        AutoPhase.SEED_GENERATION,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    },
    AutoPhase.SEED_GENERATION: {AutoPhase.REVIEW, AutoPhase.BLOCKED, AutoPhase.FAILED},
    AutoPhase.REVIEW: {
        AutoPhase.REPAIR,
        AutoPhase.RUN,
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    },
    AutoPhase.REPAIR: {AutoPhase.REVIEW, AutoPhase.BLOCKED, AutoPhase.FAILED},
    AutoPhase.RUN: {AutoPhase.COMPLETE, AutoPhase.BLOCKED, AutoPhase.FAILED},
    AutoPhase.COMPLETE: set(),
    AutoPhase.BLOCKED: {
        AutoPhase.INTERVIEW,
        AutoPhase.SEED_GENERATION,
        AutoPhase.REVIEW,
        AutoPhase.RUN,
    },
    AutoPhase.FAILED: {
        AutoPhase.INTERVIEW,
        AutoPhase.SEED_GENERATION,
        AutoPhase.REVIEW,
        AutoPhase.RUN,
    },
}


def utc_now_iso() -> str:
    """Return the current UTC time in an ISO-8601 format."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class AutoPipelineState:
    """Durable state record for an ``ooo auto`` session.

    The state is intentionally JSON-serializable so a foreground command can
    safely persist progress before each potentially slow phase and resume later
    without silently duplicating execution.
    """

    goal: str
    cwd: str
    auto_session_id: str = field(default_factory=lambda: f"auto_{uuid4().hex[:12]}")
    phase: AutoPhase = AutoPhase.CREATED
    policy: AutoPolicy = AutoPolicy.CONSERVATIVE
    required_grade: str = "A"
    runtime_backend: str | None = None
    opencode_mode: str | None = None
    skip_run: bool = False
    max_interview_rounds: int = 12
    max_repair_rounds: int = 5
    interview_session_id: str | None = None
    interview_completed: bool = False
    seed_id: str | None = None
    seed_path: str | None = None
    seed_artifact: dict[str, Any] = field(default_factory=dict)
    execution_id: str | None = None
    job_id: str | None = None
    run_session_id: str | None = None
    run_subagent: dict[str, Any] = field(default_factory=dict)
    run_start_attempted: bool = False
    run_handoff_status: str | None = None
    run_handoff_guidance: str | None = None
    ledger: dict[str, Any] = field(default_factory=dict)
    last_grade: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    repair_round: int = 0
    current_round: int = 0
    pending_question: str | None = None
    last_tool_name: str | None = None
    last_error: str | None = None
    last_progress_message: str = "created"
    phase_started_at: str = field(default_factory=utc_now_iso)
    last_progress_at: str = field(default_factory=utc_now_iso)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    timeout_seconds_by_phase: dict[str, int] = field(
        default_factory=lambda: {
            AutoPhase.INTERVIEW.value: 120,
            AutoPhase.SEED_GENERATION.value: 120,
            AutoPhase.REVIEW.value: 90,
            AutoPhase.REPAIR.value: 90,
            AutoPhase.RUN.value: 60,
        }
    )

    def transition(self, next_phase: AutoPhase, message: str, *, error: str | None = None) -> None:
        """Move to ``next_phase`` after validating the phase state machine."""
        if next_phase not in _ALLOWED_TRANSITIONS[self.phase]:
            msg = f"Invalid auto phase transition: {self.phase.value} -> {next_phase.value}"
            raise ValueError(msg)
        now = utc_now_iso()
        self.phase = next_phase
        self.phase_started_at = now
        self.last_progress_at = now
        self.updated_at = now
        self.last_progress_message = message
        self.last_error = error

    def mark_progress(self, message: str, *, tool_name: str | None = None) -> None:
        """Record non-terminal progress within the current phase."""
        now = utc_now_iso()
        self.last_progress_at = now
        self.updated_at = now
        self.last_progress_message = message
        self.last_tool_name = tool_name

    def recover(self, next_phase: AutoPhase, message: str) -> None:
        """Move a session back to a valid recoverable phase."""
        self.transition(next_phase, message)

    def mark_blocked(self, message: str, *, tool_name: str | None = None) -> None:
        """Transition to blocked with actionable diagnostics."""
        self.last_tool_name = tool_name
        self.transition(AutoPhase.BLOCKED, message, error=message)

    def mark_failed(self, message: str, *, tool_name: str | None = None) -> None:
        """Transition to failed with actionable diagnostics."""
        self.last_tool_name = tool_name
        self.transition(AutoPhase.FAILED, message, error=message)

    def is_terminal(self) -> bool:
        """Return True when the state cannot continue automatically."""
        return self.phase in TERMINAL_PHASES

    def is_stale(self, now: datetime | None = None) -> bool:
        """Return True when current phase has exceeded its configured timeout."""
        if self.is_terminal():
            return False
        timeout = self.timeout_seconds_by_phase.get(self.phase.value)
        if timeout is None:
            return False
        current = now or datetime.now(UTC)
        last = datetime.fromisoformat(self.last_progress_at)
        return (current - last).total_seconds() > timeout

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        data = asdict(self)
        data["phase"] = self.phase.value
        data["policy"] = self.policy.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoPipelineState:
        """Deserialize from a dictionary and reject malformed persisted state."""
        payload = dict(data)
        # Older auto sessions predate durable loop-bound policy. Preserve
        # resume compatibility by assigning the historical defaults once, then
        # persisting them with subsequent saves.
        payload.setdefault("max_interview_rounds", 12)
        payload.setdefault("max_repair_rounds", 5)
        payload.setdefault("run_handoff_status", None)
        payload.setdefault("run_handoff_guidance", None)
        required_fields = {item.name for item in fields(cls)}
        missing_fields = sorted(required_fields - payload.keys())
        if missing_fields:
            msg = f"state is missing required fields: {', '.join(missing_fields)}"
            raise ValueError(msg)
        payload["phase"] = AutoPhase(payload["phase"])
        payload["policy"] = AutoPolicy(payload["policy"])
        state = cls(**payload)
        state._validate_loaded()
        return state

    def _validate_loaded(self) -> None:
        """Validate fields whose bad values would otherwise fail later during resume."""
        for field_name in (
            "goal",
            "cwd",
            "auto_session_id",
            "required_grade",
            "last_progress_message",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                msg = f"{field_name} must be a non-empty string"
                raise ValueError(msg)
        if self.required_grade not in {"A", "B", "C"}:
            msg = "required_grade must be one of A, B, or C"
            raise ValueError(msg)
        for field_name in ("max_interview_rounds", "max_repair_rounds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                msg = f"{field_name} must be a positive integer"
                raise ValueError(msg)

        for field_name in (
            "phase_started_at",
            "last_progress_at",
            "created_at",
            "updated_at",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                msg = f"{field_name} must be an ISO timestamp string"
                raise ValueError(msg)
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                msg = f"{field_name} must be an ISO timestamp string"
                raise ValueError(msg) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                msg = f"{field_name} must include timezone information"
                raise ValueError(msg)

        if not isinstance(self.timeout_seconds_by_phase, dict):
            msg = "timeout_seconds_by_phase must be an object"
            raise ValueError(msg)
        valid_phases = {phase.value for phase in AutoPhase}
        required_timeout_phases = {
            AutoPhase.INTERVIEW.value,
            AutoPhase.SEED_GENERATION.value,
            AutoPhase.REVIEW.value,
            AutoPhase.REPAIR.value,
            AutoPhase.RUN.value,
        }
        missing_timeout_phases = sorted(
            required_timeout_phases - self.timeout_seconds_by_phase.keys()
        )
        if missing_timeout_phases:
            msg = f"timeout_seconds_by_phase is missing required phases: {', '.join(missing_timeout_phases)}"
            raise ValueError(msg)
        for phase, timeout in self.timeout_seconds_by_phase.items():
            if not isinstance(phase, str) or phase not in valid_phases:
                msg = "timeout_seconds_by_phase keys must be known phase strings"
                raise ValueError(msg)
            if type(timeout) is not int or timeout <= 0:
                msg = "timeout_seconds_by_phase values must be positive integers"
                raise ValueError(msg)

        if not isinstance(self.ledger, dict):
            msg = "ledger must be an object"
            raise ValueError(msg)
        if not isinstance(self.run_subagent, dict):
            msg = "run_subagent must be an object"
            raise ValueError(msg)
        if self.ledger:
            try:
                from ouroboros.auto.ledger import SeedDraftLedger

                SeedDraftLedger.from_dict(self.ledger)
            except Exception as exc:
                msg = "ledger must be a valid Seed Draft Ledger"
                raise ValueError(msg) from exc
        optional_string_fields = (
            "runtime_backend",
            "opencode_mode",
            "interview_session_id",
            "seed_id",
            "seed_path",
            "execution_id",
            "job_id",
            "run_session_id",
            "run_handoff_status",
            "run_handoff_guidance",
            "last_grade",
            "pending_question",
            "last_tool_name",
            "last_error",
        )
        for field_name in optional_string_fields:
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                msg = f"{field_name} must be a string or null"
                raise ValueError(msg)
            if not value.strip():
                msg = f"{field_name} must be a non-empty string or null"
                raise ValueError(msg)
        for field_name in ("interview_completed", "skip_run", "run_start_attempted"):
            if type(getattr(self, field_name)) is not bool:
                msg = f"{field_name} must be a boolean"
                raise ValueError(msg)
        for field_name in ("findings",):
            value = getattr(self, field_name)
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                msg = f"{field_name} must be a list of objects"
                raise ValueError(msg)
        for field_name in ("repair_round", "current_round"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                msg = f"{field_name} must be a non-negative integer"
                raise ValueError(msg)

        if self.seed_artifact != {}:
            if not isinstance(self.seed_artifact, dict):
                msg = "seed_artifact must be an object"
                raise ValueError(msg)
            try:
                from ouroboros.core.seed import Seed

                Seed.from_dict(self.seed_artifact)
            except Exception as exc:
                msg = "seed_artifact must be a valid Seed artifact"
                raise ValueError(msg) from exc


class AutoStore:
    """JSON file store for ``AutoPipelineState`` records."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (Path.home() / ".ouroboros" / "data")

    def path_for(self, auto_session_id: str) -> Path:
        """Return the JSON path for ``auto_session_id``."""
        safe = auto_session_id.strip()
        if not safe.startswith("auto_") or "/" in safe or ".." in safe:
            msg = f"Invalid auto session id: {auto_session_id}"
            raise ValueError(msg)
        return self.root / f"{safe}.json"

    def save(self, state: AutoPipelineState) -> Path:
        """Persist ``state`` atomically and return the written path."""
        state._validate_loaded()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(state.auto_session_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(path)
        return path

    def load(self, auto_session_id: str) -> AutoPipelineState:
        """Load a state record or raise an actionable error."""
        path = self.path_for(auto_session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            msg = f"Auto session not found: {auto_session_id}"
            raise ValueError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"Auto session state is corrupt: {path}"
            raise ValueError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"Auto session state must be an object: {path}"
            raise ValueError(msg)
        try:
            state = AutoPipelineState.from_dict(raw)
            if state.auto_session_id != auto_session_id:
                msg = f"Auto session id mismatch: requested {auto_session_id}, found {state.auto_session_id}"
                raise ValueError(msg)
            return state
        except (TypeError, ValueError) as exc:
            msg = f"Auto session state is invalid: {path}: {exc}"
            raise ValueError(msg) from exc
