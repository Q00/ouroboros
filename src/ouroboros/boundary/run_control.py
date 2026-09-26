"""One run's check package boundary, shared by ``ooo run`` and ``ouroboros_execute_seed``.

``CheckPackageRun`` resolves the arm (``boundary/rollout.py``), prepares the
package before the worker starts, installs ``CheckPackageAuthority`` on the
runner so the package decides the criteria it covers before the terminal
status is persisted, and afterwards renders the outcome and the enumerated
``workflow_outcome`` dimensions (TELEMETRY.md, "Randomized defaults").

With the arm ``off`` nothing here calls a model, writes an event, or touches
the runner: the run is the legacy run. The legacy failure-class dimensions are
still derived (read-only) so the ``off`` arm is a baseline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.authority import CheckPackageAuthority
from ouroboros.boundary.ledger import BoundaryOrderError
from ouroboros.boundary.rollout import Arm, AssignmentSource, CheckPackageAssignment
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    CheckPackageSettings,
    prepare_check_package,
    render_preparation,
    render_verdict,
    resolve_check_package_settings,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

RECOVERY_EXHAUSTED_EVENT_TYPE = "execution.ac.recovery_exhausted"
_EVIDENCE_LIMIT = 5000
# orchestrator/failure_taxonomy.FailureClass values, lower-cased; the telemetry
# serializer holds the same closed set (telemetry._LEGACY_FAILURE_CLASSES).
_FAILURE_CLASS_VALUES = frozenset(
    {
        "evidence_missing",
        "evidence_form_mismatch",
        "fabrication_suspected",
        "scope_creep",
        "stall",
        "blocked",
        "transcript_missing_infrastructure",
    }
)
_FALLBACK_ASSIGNMENT = CheckPackageAssignment(Arm.OFF, AssignmentSource.FALLBACK)


def _count_bucket(count: int) -> str:
    return "3+" if count >= 3 else str(max(0, count))


TIER_SUMMARY_TIERS = ("A", "A_prime", "U")


def tier_summary_value(counts: dict[str, int]) -> str:
    """``check_tier_summary``: bucketed counts of tiers A, A' and U (closed set)."""
    return ",".join(f"{tier}:{_count_bucket(counts.get(tier, 0))}" for tier in TIER_SUMMARY_TIERS)


def legacy_failure_class_from_annotations(legacy: dict[int, Any]) -> tuple[str, str]:
    """``(class, count bucket)`` from the legacy verdicts the authority annotated."""
    rejected = sorted(
        index
        for index, item in legacy.items()
        if item.outcome == "failed" and item.terminal_status == "failed"
    )
    if not rejected:
        return "other", "0"
    raw = legacy[rejected[0]].failure_class
    first = raw.lower() if isinstance(raw, str) else ""
    return (first if first in _FAILURE_CLASS_VALUES else "other"), _count_bucket(len(rejected))


def legacy_failure_class_from_events(
    events: Iterable[Any], *, session_id: str | None
) -> tuple[str, str]:
    """``(class, count bucket)`` for the criteria the legacy verifier rejected.

    Reads ``execution.ac.recovery_exhausted``, which the executor writes once
    per root criterion it finally rejected, with the worker failure class of
    the last attempt. The class of the lowest criterion index wins; a class
    outside the failure taxonomy is ``other``. ``("other", "0")`` means the
    legacy verdict was a rejection with no per-criterion record.
    """
    by_index: dict[int, str] = {}
    for event in events:
        data = getattr(event, "data", None) or {}
        if session_id is not None and data.get("session_id") not in (None, session_id):
            continue
        index = data.get("root_ac_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        raw = data.get("last_failure_class")
        by_index[index] = raw.lower() if isinstance(raw, str) else ""
    if not by_index:
        return "other", "0"
    first = by_index[min(by_index)]
    return (first if first in _FAILURE_CLASS_VALUES else "other"), _count_bucket(len(by_index))


async def legacy_failure_dimensions(
    event_store: EventStore,
    *,
    execution_id: str | None,
    session_id: str | None,
    legacy_verdict: str,
) -> dict[str, str]:
    """``legacy_failure_class`` and ``legacy_failure_class_count`` for a run."""
    if legacy_verdict == "accept":
        return {"legacy_failure_class": "accepted", "legacy_failure_class_count": "0"}
    if legacy_verdict != "reject" or not execution_id:
        return {"legacy_failure_class": "none", "legacy_failure_class_count": "0"}
    try:
        events = await event_store.query_events(
            aggregate_id=execution_id,
            event_type=RECOVERY_EXHAUSTED_EVENT_TYPE,
            limit=_EVIDENCE_LIMIT,
        )
    except Exception:  # noqa: BLE001 - enrichment must not drop the outcome
        events = []
    # query_events is newest first; replay oldest first so the latest record
    # for a criterion wins.
    failure_class, count = legacy_failure_class_from_events(
        list(reversed(events)), session_id=session_id
    )
    return {"legacy_failure_class": failure_class, "legacy_failure_class_count": count}


ConstructorFactory = Callable[..., Any]


@dataclass
class CheckPackageRun:
    """The check package boundary of one run, from arm to telemetry."""

    settings: CheckPackageSettings
    state: BoundaryRunState | None = None
    authority: CheckPackageAuthority | None = None
    attempted: bool = False
    skipped_reason: str | None = None
    preparation_error: str | None = None
    _binding: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def resolve(cls, cli_value: bool | None = None) -> CheckPackageRun:
        """Resolve the arm and budgets; never raises (an error means ``off``)."""
        try:
            return cls(resolve_check_package_settings(cli_value))
        except Exception:  # noqa: BLE001 - resolving the default must not fail a run
            log.warning("boundary.run_control.resolve_failed")
            return cls(CheckPackageSettings(enabled=False, assignment=_FALLBACK_ASSIGNMENT))

    @property
    def assignment(self) -> CheckPackageAssignment:
        return self.settings.assignment or (
            CheckPackageAssignment(Arm.ON, AssignmentSource.USER_FORCED_ON)
            if self.settings.enabled
            else _FALLBACK_ASSIGNMENT
        )

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    async def prepare(
        self,
        runner: Any,
        seed: Seed,
        *,
        event_store: EventStore,
        execution_id: str | None,
        worker_dir: Path,
        runtime_backend: str,
        model: str | None,
        resume: bool,
        constructor_factory: ConstructorFactory | None = None,
    ) -> list[str]:
        """Build, freeze, and admit the package; install the authority on ``runner``.

        Returns lines for the person running the command. Raises
        ``BoundaryOrderError`` (including ``BoundaryLeakError``) when the
        ledger refuses the worker start; the caller must not dispatch then.
        Any other preparation error is recorded and the run continues under
        the legacy verifier.
        """
        if not self.enabled:
            return []
        if resume or not execution_id:
            self.skipped_reason = "resume"
            return ["Check package is not applied on resume; the session keeps its boundary."]
        self.attempted = True
        if constructor_factory is None:
            from ouroboros.boundary.constructor import CheckConstructor

            constructor_factory = CheckConstructor
        lines = [
            "Check package: constructing checks from the acceptance criteria "
            f"(read-only; arm on, {self.assignment.source.value})..."
        ]
        try:
            constructor = constructor_factory(
                runtime_backend=runtime_backend,
                model=model,
                timeout_seconds=self.settings.constructor_timeout_seconds,
            )
            state = await prepare_check_package(
                seed,
                event_store=event_store,
                constructor=constructor,
                execution_id=execution_id,
                base_checkout=worker_dir,
                worker_workspace=worker_dir,
                runtime_label=runtime_backend,
                settings=self.settings,
            )
        except BoundaryOrderError:
            raise
        except Exception as exc:  # noqa: BLE001 - a preparation fault must not fail the run
            self.preparation_error = type(exc).__name__
            log.warning(
                "boundary.run_control.prepare_failed",
                execution_id=execution_id,
                error_type=self.preparation_error,
            )
            return [
                *lines,
                f"Check package could not be prepared ({self.preparation_error}); "
                "the existing verifier decides this run.",
            ]
        self.state = state
        self.authority = CheckPackageAuthority(
            state, self.settings, event_store=event_store, candidate_checkout=worker_dir
        )
        runner.acceptance_authority = self.authority
        return [*lines, *render_preparation(state)]

    # ------------------------------------------------------------------
    # Compact entry points for the MCP ``execute_seed`` handler

    def bind(
        self, runner: Any, event_store: EventStore, worker_dir: Path, runtime_backend: str
    ) -> CheckPackageRun:
        """Remember the handler's runner and workspace for ``prepare_bound``."""
        from ouroboros.config.loader import resolve_execution_model

        self._binding = {
            "runner": runner,
            "event_store": event_store,
            "worker_dir": worker_dir,
            "runtime_backend": runtime_backend,
            "model": resolve_execution_model(runtime_backend),
        }
        return self

    async def prepare_bound(self, seed: Seed, execution_id: str) -> None:
        """``prepare`` for a fresh run with the bound context; lines go to the log."""
        binding = self._binding
        lines = await self.prepare(
            binding["runner"],
            seed,
            event_store=binding["event_store"],
            execution_id=execution_id,
            worker_dir=binding["worker_dir"],
            runtime_backend=binding["runtime_backend"],
            model=binding["model"],
            resume=False,
        )
        for line in lines:
            log.info("boundary.run_control.prepared", execution_id=execution_id, line=line)

    async def meta_for(self, tracker: Any, session_status: Any) -> dict[str, str]:
        """``outcome_meta`` for a finished MCP run; empty while it is still running."""
        status = getattr(session_status, "value", None)
        if not isinstance(status, str):
            return {}
        try:
            return await self.outcome_meta(
                self._binding["event_store"],
                execution_id=tracker.execution_id,
                session_id=tracker.session_id,
                terminal_status=status,
            )
        except Exception:  # noqa: BLE001 - enrichment must not fail the tool result
            return {}

    # ------------------------------------------------------------------
    # Outcome

    @property
    def status(self) -> str:
        """``check_package_status`` (TELEMETRY.md)."""
        if not self.enabled or not self.attempted:
            return "not_run"
        if self.preparation_error is not None or self.state is None:
            return "construction_failed"
        if self.state.admitted:
            return "admitted"
        reason = self.state.failure_reason or ""
        return "rejected" if reason.startswith("package_") else "construction_failed"

    def render_outcome(self) -> list[str]:
        """Lines describing what the package decided (empty when it did not run)."""
        if self.authority is None:
            return []
        outcome = self.authority.outcome
        if outcome is None:
            return [
                "Check package was not consulted: this execution path does not support it; "
                "the existing verifier decided the run."
            ]
        if outcome.error is not None:
            return [
                f"Check package verification failed ({outcome.error}); "
                "the existing verifier decided the run."
            ]
        lines = [] if outcome.verdict is None else render_verdict(outcome.verdict)
        repairs = [entry for entry in self.authority.gate.log if entry["status"] == "fail"]
        if repairs:
            lines.append(
                f"Repairs driven by check package counterexamples: {len(repairs)} "
                "(the legacy verifier triggered none)."
            )
        reconciliation = outcome.reconciliation
        if reconciliation is not None:
            from ouroboros.boundary.acceptance import render_reconciliation

            lines.extend(render_reconciliation(reconciliation))
            if reconciliation.run_accepted and not outcome.legacy_run_accepted:
                lines.append(
                    "The check package accepted criteria the legacy verifier rejected; "
                    "the legacy verdict is advisory only."
                )
            elif outcome.legacy_run_accepted and not reconciliation.run_accepted:
                lines.append("The finished workspace fails the frozen check package.")
        return lines

    def _legacy_verdict(self, terminal_status: str | None, *, verdict_available: bool) -> str:
        outcome = self.authority.outcome if self.authority is not None else None
        if outcome is not None:
            return "accept" if outcome.legacy_run_accepted else "reject"
        if not verdict_available:
            return "none"
        return {"completed": "accept", "failed": "reject"}.get(terminal_status or "", "none")

    def _package_verdict(self) -> str:
        outcome = self.authority.outcome if self.authority is not None else None
        if outcome is None:
            return "none"
        if outcome.error is not None:
            return "indeterminate"
        if outcome.verdict is None or outcome.verdict.package_sha256 is None:
            return "none"
        return outcome.verdict.verdict

    def _reconciliation(self, legacy_verdict: str) -> str:
        if self.status == "not_run":
            return "none"
        outcome = self.authority.outcome if self.authority is not None else None
        if outcome is None or outcome.reconciliation is None or not outcome.package_decided:
            return "fallback_to_legacy"
        accepted = outcome.reconciliation.run_accepted
        legacy_accepted = legacy_verdict == "accept"
        if accepted == legacy_accepted:
            return "agree"
        if accepted:
            return "package_accepted_over_legacy_reject"
        return "package_rejected_over_legacy_accept"

    async def outcome_meta(
        self,
        event_store: EventStore,
        *,
        execution_id: str | None,
        session_id: str | None,
        terminal_status: str | None,
        verdict_available: bool = True,
    ) -> dict[str, str]:
        """The enumerated ``workflow_outcome`` dimensions for this run."""
        legacy_verdict = self._legacy_verdict(terminal_status, verdict_available=verdict_available)
        meta = {
            "check_package_arm": self.assignment.arm.value,
            "check_package_assignment": self.assignment.source.value,
            "check_package_status": self.status,
            "package_verdict": self._package_verdict(),
            "legacy_verdict": legacy_verdict,
            "reconciliation": self._reconciliation(legacy_verdict),
        }
        outcome = self.authority.outcome if self.authority is not None else None
        if outcome is not None and outcome.legacy and legacy_verdict == "reject":
            failure_class, count = legacy_failure_class_from_annotations(outcome.legacy)
            meta.update(
                {"legacy_failure_class": failure_class, "legacy_failure_class_count": count}
            )
        else:
            meta.update(
                await legacy_failure_dimensions(
                    event_store,
                    execution_id=execution_id,
                    session_id=session_id,
                    legacy_verdict=legacy_verdict,
                )
            )
        reconciliation = outcome.reconciliation if outcome is not None else None
        if reconciliation is not None:
            meta["unverified_count"] = _count_bucket(len(reconciliation.unverified))
            meta["check_tier_summary"] = tier_summary_value(reconciliation.tiers)
        return meta


__all__ = [
    "TIER_SUMMARY_TIERS",
    "CheckPackageRun",
    "legacy_failure_class_from_annotations",
    "tier_summary_value",
    "legacy_failure_class_from_events",
    "legacy_failure_dimensions",
]
