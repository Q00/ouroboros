"""Persistent state for full-quality ``ooo auto`` sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from ouroboros.auto.recovery_plan import AutoRecoveryPlan

_LOGGER = logging.getLogger(__name__)


class AutoPhase(StrEnum):
    """Closed set of phases for auto-mode resume and stall handling."""

    CREATED = "created"
    INTERVIEW = "interview"
    SEED_GENERATION = "seed_generation"
    REVIEW = "review"
    REPAIR = "repair"
    RUN = "run"
    RALPH_HANDOFF = "ralph_handoff"
    EVALUATE = "evaluate"
    UNSTUCK_LATERAL = "unstuck_lateral"
    # RFC #809 Phase 2.2 originally specified a third recovery phase
    # ``SEED_REGENERATE`` that would re-enter SEED_GENERATION with a
    # ``[lateral_repair]`` ledger entry, automatically rewriting the Seed's
    # acceptance criteria when persona lateral also failed. P2.2b
    # deliberately does NOT wire this phase: an automatic AC rewrite when
    # the run cannot satisfy the user's stated bar is a reward-hacking
    # surface (the system silently downgrades the spec the user explicitly
    # agreed to in the interview, then declares success against the
    # downgraded spec). Ouroboros's spec-first invariant — "define what
    # should be built before telling AI what to build" — keeps the spec
    # owned by the human. P2.2b instead exhausts the deterministic
    # recovery budget (rounds + fingerprint + persona-once + wall-clock)
    # and surfaces a BLOCKED with the two operator choices that are
    # actually functional today (re-interview, abandon) rather than
    # mutating the Seed. An earlier draft of this comment advertised a
    # third "relax AC" path; that was dropped because late-phase resume
    # reconstructs the Seed from ``state.seed_artifact`` and ignores
    # edits to the on-disk seed file — the cue in ``pipeline.py``
    # (``_RECOVERY_BLOCKED_CHOICES``) is the source of truth and lists
    # only the two functional paths.
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"


class AutoPolicy(StrEnum):
    """Supported auto-mode resolution policies."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"


class SeedOrigin(StrEnum):
    """Provenance of the persisted Seed for an auto session.

    ``auto_pipeline`` marks a Seed produced by ``AutoPipeline.run()`` itself.
    ``none`` means no Seed has been persisted yet for this session — the
    schema default for legacy state files is also ``none`` and the pipeline
    backfills ``auto_pipeline`` once on first post-PR resume of a session
    that already had a ``seed_artifact`` or ``seed_path``.

    Additional provenance values (e.g. for Seeds attached via a side-channel
    ``ouroboros_generate_seed`` writer) are intentionally deferred until the
    matching producer path lands; introducing an enum value without a writer
    creates a public contract that the runtime cannot honor.
    """

    NONE = "none"
    AUTO_PIPELINE = "auto_pipeline"


DEFAULT_TIMEOUT_SECONDS_BY_PHASE: dict[str, int] = {
    AutoPhase.INTERVIEW.value: 600,
    AutoPhase.SEED_GENERATION.value: 300,
    AutoPhase.REVIEW.value: 90,
    AutoPhase.REPAIR.value: 90,
    AutoPhase.RUN.value: 60,
    AutoPhase.EVALUATE.value: 90,
    AutoPhase.UNSTUCK_LATERAL.value: 60,
}


def _apply_phase_timeout_env_overrides(
    defaults: dict[str, int],
) -> dict[str, int]:
    """Merge ``OUROBOROS_PHASE_TIMEOUT_<PHASE>_SECONDS`` overrides into ``defaults``.

    Each phase listed in ``defaults`` may be overridden via an environment
    variable named ``OUROBOROS_PHASE_TIMEOUT_<PHASE_UPPERCASE>_SECONDS`` (for
    example ``OUROBOROS_PHASE_TIMEOUT_INTERVIEW_SECONDS=900``). Invalid values
    (non-integer, non-positive) are ignored with a warning rather than raising
    so a typo in the shell cannot crash module import.
    """
    overridden = dict(defaults)
    for phase_value in defaults:
        env_name = f"OUROBOROS_PHASE_TIMEOUT_{phase_value.upper()}_SECONDS"
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring %s=%r: must be a positive integer", env_name, raw)
            continue
        if parsed <= 0:
            _LOGGER.warning("Ignoring %s=%r: must be a positive integer", env_name, raw)
            continue
        overridden[phase_value] = parsed
    return overridden


DEFAULT_TIMEOUT_SECONDS_BY_PHASE = _apply_phase_timeout_env_overrides(
    DEFAULT_TIMEOUT_SECONDS_BY_PHASE
)

# RFC #809 Phase 2.2b — closed-loop recovery cap. Three EVALUATE rounds is
# the SeedRepairer convention re-applied to the EVALUATE ⇄ UNSTUCK_LATERAL
# retry cycle. Past three rounds the failure mode is structural rather
# than a transient grading miss; the pipeline blocks for human review
# (with the two functional operator choices documented at
# ``_RECOVERY_BLOCKED_CHOICES`` in ``pipeline.py``: re-interview or
# abandon) instead of burning more LLM budget or — worse — silently
# rewriting the Seed (see the SEED_REGENERATE deferral comment on
# ``AutoPhase``).
MAX_EVALUATE_ROUNDS: int = 3

# RFC #809 Phase 2.2b — closed allowlists for the durable recovery-loop
# fields. ``_validate_loaded`` rejects any state file whose persisted
# values fall outside these sets so a hand-edited or corrupted
# checkpoint surfaces a clean ValueError at load time instead of a
# delayed runtime crash deep inside ``_run_evaluate`` /
# ``_run_lateral``. ``_VALID_RECOVERY_PERSONAS`` mirrors
# ``ouroboros.resilience.lateral.ThinkingPersona.value``; duplicated as
# string literals to keep ``state.py`` independent of the resilience
# module's import graph (state has no other dependency on resilience).
# ``_VALID_RECOVERY_GUARD_TAGS`` mirrors the tags written by the three
# guard sites in ``pipeline.py``.
_VALID_RECOVERY_PERSONAS: frozenset[str] = frozenset(
    {"hacker", "architect", "researcher", "simplifier", "contrarian"}
)
_VALID_RECOVERY_GUARD_TAGS: frozenset[str] = frozenset(
    {"round_budget", "duplicate_fingerprint", "personas_exhausted"}
)

# Top-level pipeline deadline (Q00/ouroboros#779). Default of 7200s (2h) covers
# a typical product-bootstrap chain — interview ≤ 600s (one LLM call per round
# across up to 12 rounds, capped at the phase budget) + seed gen ≤ 300s +
# review/repair ≤ 90s × 5 + run kick-off + ralph 10 generations × 5–15 min —
# with headroom, and stays well under "user has gone home" scenarios.
DEFAULT_PIPELINE_TIMEOUT_SECONDS: float = 7200.0
MIN_PIPELINE_TIMEOUT_SECONDS: float = 60.0
MAX_PIPELINE_TIMEOUT_SECONDS: float = 86400.0
# Allowed keys for the optional gateway-provenance metadata recorded on auto state.
# Strict allowlist: anything not listed here is dropped during redaction so that
# tokens, credentials, or raw user utterances cannot be persisted by accident.
PROVENANCE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "source",
        "rewrite",
        "original_utterance_hash",
        "channel_id_hash",
        "user_id_hash",
        "platform_message_id",
        "gateway_version",
    }
)

# Per-key validators. Each returns the cleaned value or raises ValueError.
_PROVENANCE_HEX_KEYS = {
    "original_utterance_hash",
    "channel_id_hash",
    "user_id_hash",
}
_PROVENANCE_MAX_LENGTHS = {
    "source": 32,
    "platform_message_id": 64,
    "gateway_version": 32,
    "original_utterance_hash": 128,
    "channel_id_hash": 128,
    "user_id_hash": 128,
}
# Surface a clear ImportError instead of a runtime KeyError when the allowlist
# grows but a length cap is not added alongside it.
assert (PROVENANCE_ALLOWED_KEYS - {"rewrite"}).issubset(  # noqa: S101
    _PROVENANCE_MAX_LENGTHS.keys()
), "every non-rewrite provenance key needs an entry in _PROVENANCE_MAX_LENGTHS"


def _clean_provenance_value(key: str, value: Any) -> Any:
    if key == "rewrite":
        if not isinstance(value, bool):
            msg = "provenance.rewrite must be a boolean"
            raise ValueError(msg)
        return value
    if not isinstance(value, str):
        msg = f"provenance.{key} must be a string"
        raise ValueError(msg)
    cleaned = value.strip()
    if not cleaned:
        msg = f"provenance.{key} must be a non-empty string"
        raise ValueError(msg)
    limit = _PROVENANCE_MAX_LENGTHS[key]
    if len(cleaned) > limit:
        msg = f"provenance.{key} exceeds {limit}-character limit"
        raise ValueError(msg)
    if key in _PROVENANCE_HEX_KEYS:
        lowered = cleaned.lower()
        if not all(c in "0123456789abcdef" for c in lowered):
            msg = f"provenance.{key} must be a lowercase hex digest"
            raise ValueError(msg)
        return lowered
    if any(c.isspace() or not c.isprintable() for c in cleaned):
        msg = f"provenance.{key} must be printable without whitespace"
        raise ValueError(msg)
    return cleaned


def redact_provenance(raw: Any) -> dict[str, Any] | None:
    """Return an allowlisted, type-checked provenance dict (or None).

    Unknown keys are silently dropped so that callers cannot smuggle private
    data via ad-hoc fields. Validation errors on allowed keys raise instead of
    being swallowed so that bad gateway integrations surface early.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = "provenance must be an object or null"
        raise ValueError(msg)
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in PROVENANCE_ALLOWED_KEYS:
            continue
        cleaned[key] = _clean_provenance_value(key, value)
    if not cleaned:
        return None
    return cleaned


class AutoResumeCapability(StrEnum):
    """Classification of what ``--resume`` will actually do for a session.

    The value is **never persisted** — it is a pure derivation from the
    persisted :class:`AutoPipelineState` fields. See
    :meth:`AutoPipelineState.resume_capability` for the decision matrix.
    """

    NONE = "none"
    """Cannot resume; the session is done or unrecoverable."""

    RETRY = "retry"
    """Re-runs the failed step from scratch — no prior progress is reused."""

    PARTIAL_RESUME = "partial_resume"
    """Resumes with some context preserved; pick-up point is approximate."""

    RESUME = "resume"
    """Continues exactly where it left off with full context."""


TERMINAL_PHASES = {AutoPhase.COMPLETE, AutoPhase.BLOCKED, AutoPhase.FAILED}
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
    AutoPhase.RUN: {
        AutoPhase.COMPLETE,
        AutoPhase.RALPH_HANDOFF,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    },
    AutoPhase.RALPH_HANDOFF: {
        AutoPhase.EVALUATE,
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    },
    AutoPhase.EVALUATE: {
        AutoPhase.UNSTUCK_LATERAL,
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    },
    # RFC #809 Phase 2.2b — UNSTUCK_LATERAL is no longer a terminal advisory
    # phase. It now dispatches one of three outcomes inside the bounded
    # retry budget: a fresh RALPH_HANDOFF that re-enters EVALUATE, a direct
    # EVALUATE re-entry for recovered artifacts, or BLOCKED when a recovery
    # guard trips (``MAX_EVALUATE_ROUNDS``, duplicate failure fingerprint,
    # persona exhaustion, or wall-clock budget). SEED_REGENERATE is
    # deliberately not part of this set — see the ``AutoPhase`` comment
    # block above for the spec-first rationale behind that omission.
    AutoPhase.UNSTUCK_LATERAL: {
        AutoPhase.RALPH_HANDOFF,
        AutoPhase.EVALUATE,
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    },
    AutoPhase.COMPLETE: set(),
    AutoPhase.BLOCKED: {
        AutoPhase.INTERVIEW,
        AutoPhase.SEED_GENERATION,
        AutoPhase.REVIEW,
        AutoPhase.RUN,
        AutoPhase.RALPH_HANDOFF,
        AutoPhase.EVALUATE,
        AutoPhase.UNSTUCK_LATERAL,
    },
    AutoPhase.FAILED: {
        AutoPhase.INTERVIEW,
        AutoPhase.SEED_GENERATION,
        AutoPhase.REVIEW,
        AutoPhase.RUN,
        AutoPhase.RALPH_HANDOFF,
        AutoPhase.EVALUATE,
        AutoPhase.UNSTUCK_LATERAL,
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
    seed_origin: SeedOrigin = SeedOrigin.NONE
    seed_artifact: dict[str, Any] = field(default_factory=dict)
    execution_id: str | None = None
    job_id: str | None = None
    run_session_id: str | None = None
    run_subagent: dict[str, Any] = field(default_factory=dict)
    run_start_attempted: bool = False
    run_handoff_status: str | None = None
    run_handoff_guidance: str | None = None
    attached_run_handle: str | None = None
    attached_run_source: str | None = None
    attached_at: str | None = None
    run_reconciliation_status: str | None = None
    run_reconciliation_source: str | None = None
    run_reconciled_at: str | None = None
    # Ralph handoff persistence (Q00/ouroboros#773). Populated only when
    # ``--complete-product`` chains RUN → RALPH_HANDOFF after a successful run
    # handoff. ``ralph_dispatch_mode`` is ``"job"`` for in-process job-manager
    # dispatches and ``"plugin"`` for OpenCode-plugin delegations. All three
    # default to None so legacy state files load unchanged.
    ralph_job_id: str | None = None
    ralph_lineage_id: str | None = None
    ralph_dispatch_mode: str | None = None
    # Q00/ouroboros#782 review-8: preserve the un-demoted OpenCode mode used
    # to construct the Ralph handler across resume. ``state.opencode_mode`` may
    # be demoted from plugin→subprocess for authoring/run-handoff handlers, but
    # Ralph still needs the original plugin mode to return a subagent envelope.
    ralph_opencode_mode: str | None = None
    # Q00/ouroboros#782: best-effort mirror of the linked Ralph job's most
    # recent observation, populated from mcp.job.* events. Defaults keep legacy
    # state files compatible and let status surfaces render the handoff gap.
    ralph_job_status: str | None = None
    ralph_last_event_at: float | None = None
    ralph_stop_reason: str | None = None
    ralph_current_generation: int | None = None
    # Q00/ouroboros#773: persisted intent for ``--complete-product`` /
    # ``complete_product=True``. The flag is durable session state — not a
    # per-invocation argument — so a session originally started with
    # ``--complete-product`` keeps chaining RUN → RALPH_HANDOFF on resume even
    # when the operator forgets to re-pass the flag. Defaults to False so
    # legacy state files load unchanged.
    complete_product: bool = False
    ledger: dict[str, Any] = field(default_factory=dict)
    last_grade: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    auto_answer_log: list[dict[str, Any]] = field(default_factory=list)
    repair_round: int = 0
    current_round: int = 0
    pending_question: str | None = None
    last_tool_name: str | None = None
    last_error: str | None = None
    last_authoring_backend: str | None = None
    last_progress_message: str = "created"
    phase_started_at: str = field(default_factory=utc_now_iso)
    last_progress_at: str = field(default_factory=utc_now_iso)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    timeout_seconds_by_phase: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_TIMEOUT_SECONDS_BY_PHASE)
    )
    # Top-level pipeline deadline (Q00/ouroboros#779). The deadline is a
    # *monotonic*-clock value (``time.monotonic() + pipeline_timeout_seconds``)
    # to avoid wall-clock skew during a single process run, with a companion
    # ``deadline_at_epoch`` field persisted in epoch seconds so cross-process
    # resume can re-derive a fresh monotonic value on load. Both fields are
    # ``None`` until the first CREATED → INTERVIEW transition arms the deadline.
    pipeline_timeout_seconds: float = DEFAULT_PIPELINE_TIMEOUT_SECONDS
    deadline_at: float | None = None
    deadline_at_epoch: float | None = None
    # Optional provenance metadata supplied by an external gateway when it
    # rewrote a natural-language request into ``ooo auto`` shell command. None
    # for direct CLI invocations so legacy state files load unchanged.
    provenance: dict[str, Any] | None = None
    # Caller-supplied user preferences keyed by ledger section name. Persisted
    # so resumed sessions converge to the same Seed as the original run when
    # the caller does not resupply preferences. Validated against
    # ``REQUIRED_SECTIONS`` at construction time by the MCP handler — only
    # known section keys with non-empty string values land here.
    user_preferences: dict[str, str] = field(default_factory=dict)
    # Active domain profile name resolved at session start (#809 P3).
    # The DomainProfile instance itself is not stored here because it contains
    # callables that are not JSON-serializable. Downstream phases recover the
    # profile via ``DEFAULT_REGISTRY.get(active_domain_profile_name)`` and
    # prefer the profile's ``safe_defaults`` over the hardcoded coding-domain
    # fallback. None means no profile was activated, so legacy state files load
    # unchanged.
    active_domain_profile_name: str | None = None
    # QA verdict captured during the EVALUATE phase (RFC #809 Phase 2.1).
    # Persisted so a resumed session reuses the verdict without re-invoking
    # the LLM-driven judge when the underlying artifact has not changed.
    # ``evaluate_artifact_hash`` is a sha256 of the run artifact that was
    # graded: if the hash on resume matches the cached one, the cached
    # verdict is honored; otherwise the evaluator re-runs.
    last_qa_score: float | None = None
    last_qa_verdict: str | None = None
    # The QA handler's canonical pass condition is ``score >= pass_threshold``
    # (see ``QAHandler``), which can be True even when ``verdict`` is
    # ``"revise"``. Persist the boolean separately so the EVALUATE cache
    # resume path reuses the authoritative passed flag instead of
    # re-deriving from verdict text (the LLM's free-form verdict string
    # is not the source of truth for the pass decision).
    last_qa_passed: bool | None = None
    last_qa_differences: list[str] = field(default_factory=list)
    last_qa_suggestions: list[str] = field(default_factory=list)
    evaluate_artifact_hash: str | None = None
    # The actual artifact text graded during EVALUATE. Persisted verbatim
    # on first entry into ``_run_evaluate`` so a timeout / exception /
    # transient QA error leaves a recoverable trail: on ``--resume`` the
    # pipeline re-enters EVALUATE with this artifact rather than dropping
    # into the "no cached verdict and no artifact" BLOCKED branch. Stored
    # without truncation so the recomputed hash on resume matches the
    # persisted ``evaluate_artifact_hash`` — truncation would silently
    # invalidate the cache.
    evaluate_artifact: str | None = None
    # RFC #809 Phase 2.2 — UNSTUCK_LATERAL persona output captured after an
    # EVALUATE fail. Persisted so a resumed session reuses the persona
    # suggestion without re-invoking the lateral_think tool when nothing
    # has changed. ``lateral_input_hash`` is sha256 of
    # ``f"{persona}:{differences}:{suggestions}"`` — if the hash on resume
    # matches the persisted one, the cached persona text is honored.
    last_lateral_persona: str | None = None
    last_lateral_approach_summary: str | None = None
    last_lateral_text: str | None = None
    lateral_input_hash: str | None = None
    # RFC #809 Phase 2.2b — closed-loop recovery counters. Persisted so a
    # resumed session does not re-roll the budget after a process restart.
    #
    # ``evaluate_round`` counts entries into the EVALUATE phase (post-Ralph
    # or post-SEED_REGENERATE). ``MAX_EVALUATE_ROUNDS`` (3) caps the loop.
    #
    # ``failure_fingerprints`` accumulates a deterministic fingerprint of
    # each EVALUATE failure (sha256 over differences + suggestions). Two
    # consecutive identical fingerprints means the recovery loop is not
    # making material progress on the failure surface, so the pipeline
    # transitions to BLOCKED rather than spending another LLM budget cycle.
    #
    # ``personas_invoked`` lists ``ThinkingPersona.value`` strings of every
    # persona already routed in this session. The lateral router skips
    # personas already in this list and finally returns ``None`` (no
    # persona left to try) when all five are exhausted, which the pipeline
    # treats as BLOCKED.
    evaluate_round: int = 0
    failure_fingerprints: list[str] = field(default_factory=list)
    personas_invoked: list[str] = field(default_factory=list)
    # RFC #809 Phase 2.2b — sticky "guard tripped" flag. Set to a short
    # tag identifying *which* recovery guard exhausted the budget the
    # first time one of them blocked the pipeline (one of
    # ``"round_budget"``, ``"duplicate_fingerprint"``, ``"personas_exhausted"``).
    # ``_run_evaluate`` and ``_run_lateral`` both inspect this on entry
    # so a ``--resume`` after a guard-driven BLOCKED does NOT re-enter
    # the cache-fast-path or spend another persona slot — the surface
    # had been declared exhausted, and silently un-exhausting it would
    # undermine the guard contract. Cleared only on a successful
    # forward transition out of the recovery loop (e.g. COMPLETE), so a
    # fresh artifact reaching EVALUATE through a deliberate operator
    # workflow can start over with a clean budget.
    recovery_guard_tripped: str | None = None
    # Typed artifact for the next automated recovery step. Current PRs persist
    # this plan after QA/lateral failure; a follow-up redispatch PR consumes it.
    last_recovery_plan: dict[str, Any] | None = None

    def phase_timeout_seconds(self, phase: AutoPhase) -> float:
        """Return the configured timeout for ``phase`` in seconds.

        Falls back to the canonical default policy when the persisted entry
        is missing or has an unusable type. The fallback matches the dataclass
        default so legacy/partial state never silently halves an operator's
        budget.
        """
        raw = self.timeout_seconds_by_phase.get(phase.value)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return float(DEFAULT_TIMEOUT_SECONDS_BY_PHASE[phase.value])
        return float(raw)

    def arm_deadline(self) -> None:
        """Set ``deadline_at`` (monotonic) and ``deadline_at_epoch`` if unset.

        Idempotent — once the deadline is armed it must not be silently
        re-armed, otherwise resume cannot enforce a stable absolute deadline
        across process restarts. Call this on the first ``CREATED → INTERVIEW``
        transition (and from ``from_dict`` when neither persisted field is
        present, to keep legacy state files honoring the new contract).
        """
        if self.deadline_at is not None and self.deadline_at_epoch is not None:
            return
        timeout = float(self.pipeline_timeout_seconds)
        now_mono = time.monotonic()
        now_epoch = time.time()
        self.deadline_at = now_mono + timeout
        self.deadline_at_epoch = now_epoch + timeout

    def is_deadline_expired(self) -> bool:
        """Return True when ``time.monotonic()`` has passed the armed deadline."""
        if self.deadline_at is None:
            return False
        return time.monotonic() > self.deadline_at

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
        # Authoring-backend attribution is scoped to the most recent
        # authoring failure; reset on every transition so a later
        # non-authoring blocker (grade_gate, seed_saver, run_starter)
        # cannot inherit stale metadata. Authoring-side call sites must
        # call ``record_authoring_backend(state)`` *after* mark_blocked
        # / mark_failed to repopulate the field.
        self.last_authoring_backend = None

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

    def invoked_by(self) -> str:
        """Return the high-level invocation source for blocker/summary output.

        ``direct`` covers all CLI-originated runs (no provenance, or
        ``source == "cli"``). Anything else with a recognized non-cli source
        is ``gateway``. Provenance present but missing a usable ``source``
        becomes ``unknown`` so misconfigured integrations are visible.
        """
        if not self.provenance:
            return "direct"
        source = self.provenance.get("source")
        if source == "cli":
            return "direct"
        if isinstance(source, str) and source.strip():
            return "gateway"
        return "unknown"

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

    def resume_capability(self) -> AutoResumeCapability:
        """Classify what ``--resume`` will actually do for the current state.

        This is a pure derivation — the result is never persisted. The
        classification mirrors the actual control flow in
        :meth:`AutoPipeline.run` and :meth:`AutoInterviewDriver.run`.

        Decision matrix (only the highlights — see the plan and tests for
        the full table):

        * :attr:`AutoPhase.COMPLETE` -> :attr:`AutoResumeCapability.NONE`
          (completed sessions render no resume hint).
        * :attr:`AutoPhase.REPAIR` is non-terminal — a fresh ``--resume``
          will transition the state back to ``REVIEW``, so the capability
          is :attr:`AutoResumeCapability.RESUME`.
        * Other non-terminal phases also classify as ``RESUME``.
        * :attr:`AutoPhase.BLOCKED` / :attr:`AutoPhase.FAILED` consult
          ``_recoverable_phase_for_tool`` first; an unmapped or missing
          ``last_tool_name`` yields ``NONE``.
        * The hot ``#688`` cell: ``BLOCKED`` + ``last_tool_name ==
          "interview.start"`` + ``interview_session_id is None``
          classifies as :attr:`AutoResumeCapability.RETRY` because
          resuming re-runs ``interview.start`` from scratch with no
          recovered state.

        Returns:
            The :class:`AutoResumeCapability` value for the current state.
        """
        # Lazy import to avoid the ``state.py`` <-> ``pipeline.py`` cycle.
        from ouroboros.auto.pipeline import _recoverable_phase_for_tool  # noqa: PLC0415

        if self.phase == AutoPhase.COMPLETE:
            return AutoResumeCapability.NONE

        # RFC #809 Phase 2.2b — a session whose recovery guard has tripped
        # cannot make forward progress on --resume. ``_run_evaluate`` and
        # ``_run_lateral`` both short-circuit to BLOCKED immediately when
        # ``recovery_guard_tripped`` is set; advertising the session as
        # resumable in this state would be a user-facing contract bug
        # (CLI/MCP status surfaces would tell the operator "you can
        # --resume" when --resume produces an identical blocker).
        if self.recovery_guard_tripped is not None:
            return AutoResumeCapability.NONE

        if self.phase not in {AutoPhase.BLOCKED, AutoPhase.FAILED}:
            # CREATED, INTERVIEW, SEED_GENERATION, REVIEW, REPAIR, RUN.
            # Pipeline.run() will simply continue from the current phase.
            # REPAIR explicitly transitions to REVIEW on resume, so this is
            # still a true RESUME (not a partial one).
            return AutoResumeCapability.RESUME

        # --- BLOCKED or FAILED ---
        recoverable = _recoverable_phase_for_tool(self.last_tool_name)
        if recoverable is None:
            return AutoResumeCapability.NONE

        tool = self.last_tool_name

        # Interview phase tools.
        if recoverable == AutoPhase.INTERVIEW:
            if tool == "interview.start" and not self.interview_session_id:
                # The #688 case: interview.start timed out before producing
                # a session id. Resuming re-runs interview.start from
                # scratch — that is a retry, not a continuation.
                return AutoResumeCapability.RETRY
            if self.interview_session_id:
                if self.pending_question:
                    return AutoResumeCapability.RESUME
                return AutoResumeCapability.PARTIAL_RESUME
            # Interview-tool but no session id (rare for tools other than
            # interview.start); treat as a retry rather than asserting.
            return AutoResumeCapability.RETRY

        # Seed generation. We reconcile seed_artifact, seed_path, and
        # interview_session_id: a persisted artifact is the strongest
        # signal; a seed_path means we can re-load the Seed; otherwise we
        # need the interview session to regenerate.
        if recoverable == AutoPhase.SEED_GENERATION:
            if self.seed_artifact:
                return AutoResumeCapability.RESUME
            if self.seed_path:
                return AutoResumeCapability.PARTIAL_RESUME
            if self.interview_session_id:
                # Interview context carries forward, but seed generation
                # itself re-runs from scratch — no prior generation work
                # is reused. That matches the RETRY semantics, not RESUME.
                return AutoResumeCapability.RETRY
            return AutoResumeCapability.NONE

        # Review phase tools (seed_saver / grade_gate / seed_loader).
        if recoverable == AutoPhase.REVIEW:
            if self.seed_artifact:
                return AutoResumeCapability.RESUME
            if self.seed_path:
                return AutoResumeCapability.PARTIAL_RESUME
            return AutoResumeCapability.NONE

        # Run phase. Persisted run handles let us short-circuit to
        # COMPLETE; otherwise we need a Seed (artifact > path). When the
        # pipeline already attempted to start a run but produced no durable
        # handle, ``AutoPipeline.run()`` immediately re-blocks at
        # ``run_starter`` to refuse a duplicate execution — so ``--resume``
        # cannot make progress and the capability must be ``NONE``.
        if recoverable == AutoPhase.RUN:
            if any((self.job_id, self.execution_id, self.run_session_id)):
                return AutoResumeCapability.RESUME
            if self.run_start_attempted:
                return AutoResumeCapability.NONE
            if self.seed_artifact:
                return AutoResumeCapability.RESUME
            if self.seed_path:
                return AutoResumeCapability.PARTIAL_RESUME
            return AutoResumeCapability.NONE

        # Ralph handoff phase. Persisted Ralph handles or plugin dispatch
        # markers let the pipeline resume without starting duplicate run/Ralph
        # work; otherwise a Seed is enough to return to the checkpoint and
        # surface manual recovery guidance.
        if recoverable == AutoPhase.RALPH_HANDOFF:
            if any((self.ralph_job_id, self.ralph_lineage_id, self.ralph_dispatch_mode)):
                return AutoResumeCapability.RESUME
            if self.seed_artifact:
                return AutoResumeCapability.RESUME
            if self.seed_path:
                return AutoResumeCapability.PARTIAL_RESUME
            return AutoResumeCapability.NONE

        # EVALUATE phase (RFC #809 Phase 2.1). Recovery requires either the
        # persisted artifact (so the evaluator can re-grade) OR a cached
        # verdict matching the persisted hash (so the cache hit drives the
        # decision without re-invoking the LLM). Neither present → NONE.
        #
        # ``is not None`` rather than truthiness on ``evaluate_artifact``:
        # an empty-string artifact is a valid graded input, so a session
        # blocked after persisting ``""`` is still resumable.
        if recoverable == AutoPhase.EVALUATE:
            if self.evaluate_artifact is not None or (
                self.evaluate_artifact_hash is not None and self.last_qa_passed is not None
            ):
                return AutoResumeCapability.RESUME
            return AutoResumeCapability.NONE

        # UNSTUCK_LATERAL phase (RFC #809 Phase 2.2). Recovery requires the
        # persisted lateral input hash so the cache short-circuit can return
        # the cached persona suggestion without re-invoking the lateral_think
        # tool. If the cache is empty but the QA fail context is intact,
        # resume can still classify a persona and re-run lateral.
        if recoverable == AutoPhase.UNSTUCK_LATERAL:
            if self.lateral_input_hash is not None or self.last_lateral_text is not None:
                return AutoResumeCapability.RESUME
            # ``_run_lateral`` can drive forward as long as we have ANY
            # QA-fail signal (differences OR suggestions); both feed the
            # persona's ``problem_context``. Earlier this branch required
            # ``differences`` specifically, which suppressed the resume
            # hint for suggestions-only failures even though resume would
            # actually work.
            if self.last_qa_passed is False and (
                self.last_qa_differences or self.last_qa_suggestions
            ):
                return AutoResumeCapability.RESUME
            return AutoResumeCapability.NONE

        return AutoResumeCapability.NONE  # defensive

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        data = asdict(self)
        data["phase"] = self.phase.value
        data["policy"] = self.policy.value
        data["seed_origin"] = self.seed_origin.value
        # ``deadline_at`` is a *monotonic*-clock value scoped to the writing
        # process; it is meaningless to a future loader. Persist only the
        # epoch companion and recompute ``deadline_at`` from it on load. The
        # null monotonic field is still serialized so the JSON shape stays
        # stable for callers that read state files directly.
        if self.deadline_at is not None:
            data["deadline_at"] = None
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
        payload.setdefault("attached_run_handle", None)
        payload.setdefault("attached_run_source", None)
        payload.setdefault("attached_at", None)
        payload.setdefault("run_reconciliation_status", None)
        payload.setdefault("run_reconciliation_source", None)
        payload.setdefault("run_reconciled_at", None)
        payload.setdefault("ralph_job_id", None)
        payload.setdefault("ralph_lineage_id", None)
        payload.setdefault("ralph_dispatch_mode", None)
        payload.setdefault("ralph_opencode_mode", None)
        payload.setdefault("ralph_job_status", None)
        payload.setdefault("ralph_last_event_at", None)
        payload.setdefault("ralph_stop_reason", None)
        payload.setdefault("ralph_current_generation", None)
        payload.setdefault("complete_product", False)
        payload.setdefault("provenance", None)
        payload.setdefault("auto_answer_log", [])
        payload.setdefault("seed_origin", SeedOrigin.NONE.value)
        payload.setdefault("last_authoring_backend", None)
        payload.setdefault("pipeline_timeout_seconds", DEFAULT_PIPELINE_TIMEOUT_SECONDS)
        payload.setdefault("deadline_at", None)
        payload.setdefault("deadline_at_epoch", None)
        payload.setdefault("user_preferences", {})
        payload.setdefault("last_qa_score", None)
        payload.setdefault("last_qa_verdict", None)
        payload.setdefault("last_qa_passed", None)
        payload.setdefault("last_qa_differences", [])
        payload.setdefault("last_qa_suggestions", [])
        payload.setdefault("evaluate_artifact_hash", None)
        payload.setdefault("evaluate_artifact", None)
        payload.setdefault("last_lateral_persona", None)
        payload.setdefault("last_lateral_approach_summary", None)
        payload.setdefault("last_lateral_text", None)
        payload.setdefault("lateral_input_hash", None)
        payload.setdefault("active_domain_profile_name", None)
        # RFC #809 Phase 2.2b — closed-loop recovery counters. Default the
        # three new fields so a pre-P2.2b state file loads as a fresh
        # recovery budget (round 0, no fingerprints, no personas tried).
        # Without these setdefaults a legacy resume would raise the
        # "state is missing required fields" guard below.
        payload.setdefault("evaluate_round", 0)
        payload.setdefault("failure_fingerprints", [])
        payload.setdefault("personas_invoked", [])
        payload.setdefault("recovery_guard_tripped", None)
        payload.setdefault("last_recovery_plan", None)
        # Convert the persisted ``deadline_at_epoch`` (epoch seconds) back into
        # a monotonic-clock value usable from this process. If the companion
        # epoch field is present, derive ``deadline_at`` from the offset
        # between ``time.monotonic()`` and ``time.time()`` so the absolute
        # deadline survives a process restart. If both fields are missing
        # (legacy state file or never-armed non-terminal session), arm a fresh
        # deadline after construction so resumed sessions still honor the
        # top-level timeout contract.
        epoch_value = payload.get("deadline_at_epoch")
        if isinstance(epoch_value, int | float) and not isinstance(epoch_value, bool):
            now_epoch = time.time()
            now_mono = time.monotonic()
            payload["deadline_at"] = now_mono + (float(epoch_value) - now_epoch)
        else:
            # An ``deadline_at`` written by a previous process is meaningless
            # in this monotonic clock domain; drop it unless we have an epoch
            # companion to derive it from.
            payload["deadline_at"] = None
        required_fields = {item.name for item in fields(cls)}
        missing_fields = sorted(required_fields - payload.keys())
        if missing_fields:
            msg = f"state is missing required fields: {', '.join(missing_fields)}"
            raise ValueError(msg)
        payload["phase"] = AutoPhase(payload["phase"])
        payload["policy"] = AutoPolicy(payload["policy"])
        try:
            payload["seed_origin"] = SeedOrigin(payload["seed_origin"])
        except ValueError as exc:
            msg = f"seed_origin must be one of {[item.value for item in SeedOrigin]}"
            raise ValueError(msg) from exc
        state = cls(**payload)
        if (
            state.phase not in TERMINAL_PHASES
            and state.phase is not AutoPhase.CREATED
            and state.deadline_at is None
            and state.deadline_at_epoch is None
        ):
            state.arm_deadline()
        state._validate_loaded()
        if (
            state.deadline_at is None
            and state.deadline_at_epoch is None
            and not state.is_terminal()
        ):
            state.arm_deadline()
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
        if (
            isinstance(self.pipeline_timeout_seconds, bool)
            or not isinstance(self.pipeline_timeout_seconds, int | float)
            or not (
                MIN_PIPELINE_TIMEOUT_SECONDS
                <= float(self.pipeline_timeout_seconds)
                <= MAX_PIPELINE_TIMEOUT_SECONDS
            )
        ):
            msg = (
                "pipeline_timeout_seconds must be a number between "
                f"{MIN_PIPELINE_TIMEOUT_SECONDS:g} and {MAX_PIPELINE_TIMEOUT_SECONDS:g}"
            )
            raise ValueError(msg)
        for field_name in ("deadline_at", "deadline_at_epoch"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                msg = f"{field_name} must be a number or null"
                raise ValueError(msg)

        # Q00/ouroboros#782 ralph mirror fields. ``ralph_last_event_at`` is a
        # ``time.monotonic()``-domain reading set by the listener; persisted
        # values from a prior process are still numerically valid for sorting
        # but must not be reused as live monotonic deltas across processes.
        if self.ralph_last_event_at is not None:
            if isinstance(self.ralph_last_event_at, bool) or not isinstance(
                self.ralph_last_event_at, int | float
            ):
                msg = "ralph_last_event_at must be a number or null"
                raise ValueError(msg)
        if self.ralph_current_generation is not None:
            if (
                isinstance(self.ralph_current_generation, bool)
                or not isinstance(self.ralph_current_generation, int)
                or self.ralph_current_generation < 0
            ):
                msg = "ralph_current_generation must be a non-negative integer or null"
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
        if not isinstance(self.user_preferences, dict):
            msg = "user_preferences must be an object"
            raise ValueError(msg)
        for pref_key, pref_value in self.user_preferences.items():
            if not isinstance(pref_key, str) or not pref_key.strip():
                msg = "user_preferences keys must be non-empty strings"
                raise ValueError(msg)
            if not isinstance(pref_value, str) or not pref_value.strip():
                msg = (
                    "user_preferences values must be non-empty strings; persist via the "
                    "MCP handler which validates against REQUIRED_SECTIONS"
                )
                raise ValueError(msg)
        if self.last_qa_score is not None and (
            isinstance(self.last_qa_score, bool) or not isinstance(self.last_qa_score, int | float)
        ):
            msg = "last_qa_score must be a number or null"
            raise ValueError(msg)
        if self.last_qa_verdict is not None and (
            not isinstance(self.last_qa_verdict, str) or not self.last_qa_verdict.strip()
        ):
            msg = "last_qa_verdict must be a non-empty string or null"
            raise ValueError(msg)
        if self.last_qa_passed is not None and not isinstance(self.last_qa_passed, bool):
            msg = "last_qa_passed must be a boolean or null"
            raise ValueError(msg)
        if not isinstance(self.last_qa_differences, list) or any(
            not isinstance(item, str) for item in self.last_qa_differences
        ):
            msg = "last_qa_differences must be a list of strings"
            raise ValueError(msg)
        if not isinstance(self.last_qa_suggestions, list) or any(
            not isinstance(item, str) for item in self.last_qa_suggestions
        ):
            msg = "last_qa_suggestions must be a list of strings"
            raise ValueError(msg)
        if self.evaluate_artifact_hash is not None and (
            not isinstance(self.evaluate_artifact_hash, str)
            or not self.evaluate_artifact_hash.strip()
        ):
            msg = "evaluate_artifact_hash must be a non-empty string or null"
            raise ValueError(msg)
        if self.evaluate_artifact is not None and not isinstance(self.evaluate_artifact, str):
            msg = "evaluate_artifact must be a string or null"
            raise ValueError(msg)
        if self.last_lateral_persona is not None and (
            not isinstance(self.last_lateral_persona, str) or not self.last_lateral_persona.strip()
        ):
            msg = "last_lateral_persona must be a non-empty string or null"
            raise ValueError(msg)
        if self.last_lateral_approach_summary is not None and not isinstance(
            self.last_lateral_approach_summary, str
        ):
            msg = "last_lateral_approach_summary must be a string or null"
            raise ValueError(msg)
        if self.last_lateral_text is not None and not isinstance(self.last_lateral_text, str):
            msg = "last_lateral_text must be a string or null"
            raise ValueError(msg)
        if self.lateral_input_hash is not None and (
            not isinstance(self.lateral_input_hash, str) or not self.lateral_input_hash.strip()
        ):
            msg = "lateral_input_hash must be a non-empty string or null"
            raise ValueError(msg)
        # RFC #809 Phase 2.2b — recovery-loop field validation. These four
        # fields are durable state; once malformed they would crash deep
        # inside ``_run_evaluate`` (``state.evaluate_round += 1`` on a
        # string) or ``_run_lateral`` (``ThinkingPersona(value)`` on a
        # bogus persona). Reject at load time instead.
        if (
            isinstance(self.evaluate_round, bool)
            or not isinstance(self.evaluate_round, int)
            or self.evaluate_round < 0
        ):
            msg = "evaluate_round must be a non-negative integer"
            raise ValueError(msg)
        if not isinstance(self.failure_fingerprints, list) or any(
            not isinstance(item, str) or not item.strip() for item in self.failure_fingerprints
        ):
            msg = "failure_fingerprints must be a list of non-empty strings"
            raise ValueError(msg)
        if not isinstance(self.personas_invoked, list) or any(
            not isinstance(item, str) or item not in _VALID_RECOVERY_PERSONAS
            for item in self.personas_invoked
        ):
            msg = (
                "personas_invoked entries must be ThinkingPersona values "
                f"({sorted(_VALID_RECOVERY_PERSONAS)})"
            )
            raise ValueError(msg)
        if self.last_recovery_plan is not None:
            try:
                AutoRecoveryPlan.from_dict(self.last_recovery_plan)
            except Exception as exc:
                msg = "last_recovery_plan must be a valid AutoRecoveryPlan object or null"
                raise ValueError(msg) from exc
        if self.recovery_guard_tripped is not None and (
            not isinstance(self.recovery_guard_tripped, str)
            or self.recovery_guard_tripped not in _VALID_RECOVERY_GUARD_TAGS
        ):
            msg = (
                "recovery_guard_tripped must be one of "
                f"{sorted(_VALID_RECOVERY_GUARD_TAGS)} or null"
            )
            raise ValueError(msg)
        if self.provenance is not None:
            if not isinstance(self.provenance, dict):
                msg = "provenance must be an object or null"
                raise ValueError(msg)
            cleaned = redact_provenance(self.provenance)
            if cleaned != self.provenance:
                msg = "provenance contains unallowed keys; pass through redact_provenance() before persisting"
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
            "attached_run_handle",
            "attached_run_source",
            "attached_at",
            "run_reconciliation_status",
            "run_reconciliation_source",
            "run_reconciled_at",
            "ralph_job_id",
            "ralph_lineage_id",
            "ralph_dispatch_mode",
            "ralph_opencode_mode",
            "ralph_job_status",
            "ralph_stop_reason",
            "last_grade",
            "pending_question",
            "last_tool_name",
            "last_error",
            "active_domain_profile_name",
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
        for field_name in (
            "interview_completed",
            "skip_run",
            "run_start_attempted",
            "complete_product",
        ):
            if type(getattr(self, field_name)) is not bool:
                msg = f"{field_name} must be a boolean"
                raise ValueError(msg)
        for field_name in ("findings", "auto_answer_log"):
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
