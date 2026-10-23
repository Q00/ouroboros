"""Pipeline-facing Seed preflight enforcement."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ouroboros.auto.execution_acceptance import normalize_execution_acceptance
from ouroboros.auto.grade_gate_terminals import (
    degraded_seed_runs_as_stated,
    seed_review_gate_blocker,
)
from ouroboros.auto.grading import GradeGate
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.seed_preflight import run_seed_preflight
from ouroboros.auto.seed_preflight_repair import repair_seed_preflight
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPipelineState
from ouroboros.core.seed import Seed

SEED_PREFLIGHT_REPAIRED_EVENT = "auto.seed_preflight.repaired"


def preflight_workspace_root(state: AutoPipelineState) -> Path | None:
    """Return the run workspace preflight anchors relative paths to."""
    if state.cwd:
        candidate = Path(state.cwd).expanduser()
        if candidate.is_dir():
            return candidate
    return None


async def enforce_seed_preflight(
    seed: Seed,
    state: AutoPipelineState,
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
) -> str | None:
    """Return and persist a blocker description for unverifiable Seed claims."""
    report = run_seed_preflight(seed, workspace_root=preflight_workspace_root(state))
    if report.passed:
        return None
    questions = list(report.open_questions)[:8]
    await emit(
        "auto.seed_preflight.blocked",
        state.auto_session_id,
        {
            "schema_version": 1,
            "auto_session_id": state.auto_session_id,
            "seed_id": seed.metadata.seed_id,
            "codes": [finding.code for finding in report.blocking_findings][:8],
            "open_questions": questions,
        },
    )
    # The event carries every open question; the blocker names only the
    # questions of the blocking findings it counts, so an advisory question
    # is never presented as the reason the session stopped.
    blocking_questions = list(
        dict.fromkeys(finding.question for finding in report.blocking_findings)
    )[:8]
    blocker = (
        f"Seed preflight found {len(report.blocking_findings)} unexecutable contract claim(s); "
        "answer these open questions, revise the Seed, and start a new session: "
        + " | ".join(blocking_questions)
    )
    state.mark_blocked(
        blocker, tool_name="seed_preflight", error_code="seed_preflight_unexecutable"
    )
    return blocker


@dataclass(frozen=True, slots=True)
class AutoPreflightRepair:
    """Seed and review to continue with, plus a blocker when the session stops."""

    seed: Seed
    review: SeedReview
    changed: bool = False
    blocker: str | None = None


async def repair_seed_preflight_in_auto(
    seed: Seed,
    review: SeedReview,
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    *,
    grade_gate: GradeGate | None,
    seed_saver: Callable[[Seed], str] | None,
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    skip_run: bool,
) -> AutoPreflightRepair:
    """Repair preflight findings the workspace answers before blocking on them.

    Auto mode has no human to answer a preflight question, so a finding the
    repository itself decides (a verifier script that exists at another path
    in the checkout) is rewritten from the workspace listing and re-checked;
    see :mod:`ouroboros.auto.seed_preflight_repair`. The rounds share the
    review repair budget (``max_repair_rounds``). A repaired Seed is
    re-reviewed, persisted, and must still clear the grade gate; whatever
    preflight still finds afterwards is blocked by :func:`enforce_seed_preflight`.
    """
    budget = max(0, int(state.max_repair_rounds or 0) - int(state.repair_round or 0))
    outcome = await asyncio.to_thread(
        repair_seed_preflight, seed, preflight_workspace_root(state), max_rounds=budget
    )
    if not outcome.changed:
        return AutoPreflightRepair(seed, review)
    repaired_seed = normalize_execution_acceptance(outcome.seed)
    repaired_review = SeedReviewer(grade_gate).review(
        repaired_seed,
        ledger=ledger,
        closure_mode=state.interview_closure_mode,
        degraded=bool(getattr(repaired_seed.metadata, "degraded", False)),
    )
    state.seed_artifact = repaired_seed.to_dict()
    state.seed_id = repaired_seed.metadata.seed_id
    state.repair_round += len(outcome.repairs)
    state.last_grade = repaired_review.grade_result.grade.value
    state.findings = [asdict(finding) for finding in repaired_review.findings]
    if seed_saver is not None:
        try:
            state.seed_path = seed_saver(repaired_seed)
        except Exception as exc:
            state.mark_failed(f"seed save failed: {exc}", tool_name="seed_saver")
            return AutoPreflightRepair(repaired_seed, repaired_review, True, state.last_error)
    await emit(
        SEED_PREFLIGHT_REPAIRED_EVENT,
        state.auto_session_id,
        {
            "schema_version": 1,
            "auto_session_id": state.auto_session_id,
            "seed_id": repaired_seed.metadata.seed_id,
            "parent_seed_id": seed.metadata.seed_id,
            "rounds": outcome.rounds,
            "repairs": [
                {
                    "code": repair.code,
                    "subject": repair.subject,
                    "evidence_path": repair.evidence_path,
                    "commands_before": list(repair.commands_before[:8]),
                    "commands_after": list(repair.commands_after[:8]),
                }
                for repair in outcome.repairs[:8]
            ],
        },
    )
    state.mark_progress(
        f"Seed preflight repaired {len(outcome.repairs)} verify command path(s) "
        "from the workspace listing",
        tool_name="seed_preflight",
    )
    if terminal := seed_review_gate_blocker(
        state,
        repaired_review,
        skip_run=skip_run,
        runs_degraded=degraded_seed_runs_as_stated(repaired_seed),
    ):
        blocker, code = terminal
        state.mark_blocked(blocker, tool_name="grade_gate", error_code=code)
        return AutoPreflightRepair(repaired_seed, repaired_review, True, blocker)
    return AutoPreflightRepair(repaired_seed, repaired_review, True)
