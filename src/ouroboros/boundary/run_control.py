"""One run's check package boundary, shared by ``ooo run`` and ``ouroboros_execute_seed``.

``CheckPackageRun`` resolves the switch (``boundary/switch.py``, on by
default), prepares the package before the worker starts, installs
``CheckPackageAuthority`` on the runner so the package decides the criteria it
covers before the terminal status is persisted, and afterwards renders the
outcome and a closed-value summary of it (``outcome_meta``) that the MCP
``execute_seed`` result carries. The only telemetry is one anonymous
``acceptance_no_evidence`` count per decided run of the criteria accepted
without evidence and why (``boundary/no_evidence.py``), and one
``acceptance_artifact_checks`` row of what the controller-run artifact checks
observed (``boundary/base_regression.py``); neither decides anything.

With the switch ``off`` nothing here calls a model, writes an event, or
touches the runner: the run is the legacy run.

On resume the switch is read from the journal, not resolved again: when the
original run bound its worker to an admitted package, the resumed run
recomputes the package decision (``boundary/resume.py``) instead of letting
the legacy verifier decide the covered criteria.

One instance governs one run. ``CheckPackageRun.load`` resolves the switch of
a fresh run, or reads a resumed run's mode and boundary from the journal,
before anything answers for the run; the receipt (``pending_meta``), the job's
result preservation (``keeps_runner_result``), the preparation, and the final
summary (``outcome_meta``) all derive from that one loaded instance.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.acceptance import VerificationCoverage
from ouroboros.boundary.authority import (
    AUTHORITY_ERROR_PREFIX,
    AuthorityOutcome,
    CheckPackageAuthority,
)
from ouroboros.boundary.base_regression import report_artifact_checks
from ouroboros.boundary.ledger import BoundaryLedger, BoundaryOrderError
from ouroboros.boundary.no_evidence import report_no_evidence
from ouroboros.boundary.resume import (
    ResumedBoundary,
    ResumedCheckPackageAuthority,
    load_resumed_boundary,
)
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    CheckPackageSettings,
    forget_live_state,
    prepare_check_package,
    render_preparation,
    render_verdict,
    unavailable_line,
)
from ouroboros.boundary.switch import resolve_check_package_settings

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

RECOVERY_EXHAUSTED_EVENT_TYPE = "execution.ac.recovery_exhausted"
_EVIDENCE_LIMIT = 5000
# orchestrator/failure_taxonomy.FailureClass values, lower-cased.
_FAILURE_CLASS_VALUES = frozenset(
    {
        "evidence_missing",
        "evidence_form_mismatch",
        "fabrication_suspected",
        "scope_creep",
        "stall",
        "blocked",
        "transcript_missing_infrastructure",
        "script_absent_from_artifact",
        "no_call_evidence",
    }
)
SWITCH_TEXT = (
    "on by default; opt out with --no-check-package, OUROBOROS_CHECK_PACKAGE=off, "
    "or boundary.check_package: off"
)


def legacy_failure_class_from_annotations(legacy: dict[int, Any]) -> str:
    """The failure class of the lowest criterion the legacy verifier rejected (closed set)."""
    rejected = sorted(
        index
        for index, item in legacy.items()
        if item.outcome == "failed" and item.terminal_status == "failed"
    )
    if not rejected:
        return "other"
    raw = legacy[rejected[0]].failure_class
    first = raw.lower() if isinstance(raw, str) else ""
    return first if first in _FAILURE_CLASS_VALUES else "other"


def legacy_failure_class_from_events(events: Iterable[Any], *, session_id: str | None) -> str:
    """The failure class of the lowest criterion the legacy verifier rejected.

    Reads ``execution.ac.recovery_exhausted``, which the executor writes once
    per root criterion it finally rejected, with the worker failure class of
    the last attempt. The class of the lowest criterion index wins; a class
    outside the failure taxonomy, or a rejection with no per-criterion record,
    is ``other``.
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
        return "other"
    first = by_index[min(by_index)]
    return first if first in _FAILURE_CLASS_VALUES else "other"


async def legacy_failure_dimensions(
    event_store: EventStore,
    *,
    execution_id: str | None,
    session_id: str | None,
    legacy_verdict: str,
) -> dict[str, str]:
    """``legacy_failure_class`` for a run (closed set)."""
    if legacy_verdict == "accept":
        return {"legacy_failure_class": "accepted"}
    if legacy_verdict != "reject" or not execution_id:
        return {"legacy_failure_class": "none"}
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
    failure_class = legacy_failure_class_from_events(list(reversed(events)), session_id=session_id)
    return {"legacy_failure_class": failure_class}


def _no_package_coverage_line(state: BoundaryRunState | None) -> str:
    total = len(state.criterion_keys) if state is not None else 0
    return (
        "WARNING: insufficient verification: the check package decided 0 of "
        f"{total} criteria (verification_coverage=low)."
    )


ConstructorFactory = Callable[..., Any]


@dataclass
class CheckPackageRun:
    """The check package boundary of one run, from the switch to the outcome summary."""

    settings: CheckPackageSettings
    state: BoundaryRunState | None = None
    authority: CheckPackageAuthority | None = None
    attempted: bool = False
    skipped_reason: str | None = None
    preparation_error: str | None = None
    resumed: ResumedCheckPackageAuthority | None = None
    resuming: bool = False
    _resume_boundary: ResumedBoundary | None = field(default=None, repr=False)
    _binding: dict[str, Any] = field(default_factory=dict, repr=False)
    runtime_backend: str | None = None
    _no_evidence_reported: bool = field(default=False, repr=False)

    @classmethod
    def resolve(cls, cli_value: bool | None = None) -> CheckPackageRun:
        """Resolve the switch and budgets; never raises (an error means ``off``)."""
        try:
            return cls(resolve_check_package_settings(cli_value))
        except Exception:  # noqa: BLE001 - resolving the default must not fail a run
            log.warning("boundary.run_control.resolve_failed")
            return cls(CheckPackageSettings(enabled=False))

    @classmethod
    async def load(
        cls,
        event_store: EventStore,
        execution_id: str | None,
        resume: bool,
        *,
        cli_value: bool | None = None,
    ) -> CheckPackageRun:
        """The run's control, loaded before anything answers for the run.

        A fresh run resolves the switch (``resolve``). A resumed run reads its
        mode, settings, and boundary from the journal under ``execution_id``;
        the live switch is not consulted. Raises ``BoundaryOrderError`` when a
        resumed run's journal cannot be read (or it has no execution id): the
        caller must not resume then.
        """
        if not resume:
            return cls.resolve(cli_value)
        run = cls(CheckPackageSettings(enabled=False))
        await run._load_resume(event_store, execution_id)
        return run

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
        the legacy verifier. A resumed run installs what ``load`` read from
        the journal (read here when the run was not loaded as a resume).
        """
        self.runtime_backend = runtime_backend
        if resume and not self.resuming:
            await self._load_resume(event_store, execution_id)
        if self.resuming:
            return self._install_resume(runner, event_store, worker_dir)
        if not self.enabled:
            return []
        if not execution_id:
            raise BoundaryOrderError("the check package needs the run's execution id")
        self.attempted = True
        if constructor_factory is None:
            from ouroboros.boundary.constructor import CheckConstructor

            constructor_factory = CheckConstructor
        lines = [
            "Check package: constructing checks from the acceptance criteria "
            f"(read-only; {SWITCH_TEXT})..."
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
        if not state.admitted:
            # No admitted package: this run is the legacy run, exactly as with
            # the check package off. Nothing is
            # installed on the runner; the outcome summary keeps the failure
            # status and reports reconciliation=fallback_to_legacy.
            return [*lines, *render_preparation(state)]
        self.authority = CheckPackageAuthority(
            state, self.settings, event_store=event_store, candidate_checkout=worker_dir
        )
        runner.acceptance_authority = self.authority
        return [*lines, *render_preparation(state)]

    async def _load_resume(self, event_store: EventStore, execution_id: str | None) -> None:
        """Read a resumed run's mode, settings, and boundary from the journal.

        Raises ``BoundaryOrderError`` when the journal cannot be read: whether
        the package decides the covered criteria is then unknown, and letting
        the legacy verifier decide them could skip the package. The
        caller must not resume then.
        """
        if not execution_id:
            # Whether the package decides is in the journal under the
            # execution id; without it the resume cannot know.
            raise BoundaryOrderError("a resumed run needs its execution id")
        self.skipped_reason = "resume"
        try:
            boundary = await load_resumed_boundary(event_store, execution_id)
            # The mode and the settings are the run's, from its enabled record
            # (``boundary.check_package.enabled``), never the live config: a
            # run that was off resumes off, one that was on keeps its settings.
            # A malformed record is an undecidable boundary (on, nothing runs).
            recorded = await BoundaryLedger(event_store).check_package_enabled(execution_id)
            contract = boundary.contract if boundary is not None else None
        except Exception as exc:  # noqa: BLE001 - any read failure refuses the resume
            log.warning("boundary.run_control.resume_unreadable", error_type=type(exc).__name__)
            raise BoundaryOrderError(
                "check package state could not be read on resume "
                f"({type(exc).__name__}); retry the resume",
                details={"execution_id": execution_id},
            ) from exc
        self.settings = (
            CheckPackageSettings(enabled=True, check_timeout_seconds=contract.check_timeout_seconds)
            if contract is not None
            else CheckPackageSettings(enabled=recorded or boundary is not None)
        )
        self.resuming = True
        self._resume_boundary = boundary

    def _install_resume(self, runner: Any, event_store: EventStore, worker_dir: Path) -> list[str]:
        """Install the resumed authority when the original run was bound to a package."""
        boundary = self._resume_boundary
        if boundary is None:
            if not self.enabled:
                return []
            return ["Check package is not applied on resume: no admitted package was bound."]
        self.resumed = ResumedCheckPackageAuthority(
            boundary, event_store=event_store, candidate_checkout=worker_dir
        )
        runner.acceptance_authority = self.resumed
        if boundary.package is None and boundary.covered is None:
            return [
                "Check package: resumed run whose boundary records are missing "
                f"({boundary.reason}); every criterion is undecided."
            ]
        if boundary.source == "memory":
            return [
                "Check package: resumed run; the package decision is recomputed on the "
                "workspace (held-out cases in memory)."
            ]
        return [
            f"Check package: resumed run; the package is not re-run ({boundary.reason}): "
            "covered criteria are undecided, the legacy verifier decides the rest."
        ]

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
        """``prepare`` with the bound context, fresh or resumed as loaded; lines go to the log.

        Raises ``BoundaryOrderError`` when the ledger refuses the worker start.
        """
        binding = self._binding
        lines = await self.prepare(
            binding["runner"],
            seed,
            event_store=binding["event_store"],
            execution_id=execution_id,
            worker_dir=binding["worker_dir"],
            runtime_backend=binding["runtime_backend"],
            model=binding["model"],
            resume=self.resuming,
        )
        event = "boundary.run_control.resumed" if self.resuming else "boundary.run_control.prepared"
        for line in lines:
            log.info(event, execution_id=execution_id, line=line)

    def pending_meta(self) -> dict[str, str]:
        """The fields of a receipt returned before the run has a terminal status.

        Every public entry point that answers before the run ends (the
        ``ouroboros_execute_seed`` background receipt and the
        ``ouroboros_start_execute_seed`` receipt) reports these: whether the
        check package applies (``check_package``; a resume's recorded mode,
        which ``load`` read from the journal) and ``check_package_status``
        (``pending``, or ``not_run`` when it is off).
        """
        if not self.enabled:
            return {"check_package": "off", "check_package_status": "not_run"}
        return {"check_package": "on", "check_package_status": "pending"}

    def keeps_runner_result(self) -> bool:
        """Whether a background job must keep the runner's returned result authoritative.

        The closed outcome summary (``outcome_meta``) exists only in the
        result the run's handler returns after the terminal status is
        persisted, and every in-process run promises it: on or off, fresh or
        resumed. A job monitor that completes a job from the execution's
        terminal event, before that result exists, would report the run
        without it, so the job always keeps the runner's result.
        """
        return True

    async def meta_for(self, tracker: Any, session_status: Any) -> dict[str, str]:
        """``outcome_meta`` for a finished MCP run; the pending fields while it runs.

        A run still in the background (the direct ``ouroboros_execute_seed``
        receipt) has no package outcome yet: its receipt says only whether the
        check package applies (``check_package``) and its pending status
        (``pending_meta``). The decision itself is the session's terminal
        status, which the authority reconciled before it was persisted;
        ``ouroboros_start_execute_seed`` reports the full summary in its job
        result.
        """
        status = getattr(session_status, "value", None)
        if not isinstance(status, str):
            return self.pending_meta()
        try:
            return await self.outcome_meta(
                self._binding["event_store"],
                execution_id=tracker.execution_id,
                session_id=tracker.session_id,
                terminal_status=status,
                surface="mcp_execute",
            )
        except Exception:  # noqa: BLE001 - enrichment must not fail the tool result
            return {}

    # ------------------------------------------------------------------
    # Outcome

    @property
    def status(self) -> str:
        """``check_package_status`` of the outcome summary (``outcome_meta``)."""
        if self.resumed is not None:
            # Only a run whose worker was bound to an admitted package resumes
            # with a package decision.
            return "admitted"
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
        if self.resumed is not None:
            return self.resumed.render()
        if self.authority is None:
            if self.state is not None and not self.state.admitted:
                return [unavailable_line(self.state), _no_package_coverage_line(self.state)]
            return []
        outcome = self.authority.outcome
        if outcome is None:
            return [
                "Check package was not consulted: this execution path does not support it; "
                "the existing verifier decided the run."
            ]
        if outcome.error is not None:
            from ouroboros.boundary.acceptance import render_reconciliation

            lines = [
                f"Check package could not decide this run ({AUTHORITY_ERROR_PREFIX}"
                f"{outcome.error}); the criteria it covers are undecided (not accepted), "
                "the legacy verifier decided the rest."
            ]
            if outcome.reconciliation is not None:
                lines.extend(render_reconciliation(outcome.reconciliation))
            return lines
        lines = [] if outcome.verdict is None else render_verdict(outcome.verdict)
        repairs = [entry for entry in self.authority.gate.log if entry["status"] == "fail"]
        if repairs:
            lines.append(
                f"Repairs driven by check package counterexamples: {len(repairs)} "
                "(the legacy verifier drives retries only for legacy-decided criteria)."
            )
        if self.authority.gate.legacy_failures:
            lines.append(
                "Attempts the legacy verifier rejected on legacy-decided criteria: "
                f"{self.authority.gate.legacy_failures}."
            )
        if self.authority.gate.artifact_repairs:
            lines.append(
                "Repairs driven by failing existing or added tests: "
                f"{self.authority.gate.artifact_repairs}."
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

    def _coverage(self) -> str | None:
        """``verification_coverage``: ``low`` when the package decided nothing (switch on)."""
        if self.status == "not_run" and self.resumed is None:
            return None
        outcome = self._outcome()
        reconciliation = outcome.reconciliation if outcome is not None else None
        if reconciliation is None or not reconciliation.legacy_rule:
            return VerificationCoverage.LOW.value
        return reconciliation.coverage.value

    def _outcome(self) -> AuthorityOutcome | None:
        """The decision of this run's authority, live or resumed."""
        if self.resumed is not None:
            return self.resumed.outcome
        return self.authority.outcome if self.authority is not None else None

    def _legacy_verdict(self, terminal_status: str | None, *, verdict_available: bool) -> str:
        outcome = self._outcome()
        if outcome is not None:
            return "accept" if outcome.legacy_run_accepted else "reject"
        if not verdict_available:
            return "none"
        return {"completed": "accept", "failed": "reject"}.get(terminal_status or "", "none")

    def _package_verdict(self) -> str:
        outcome = self._outcome()
        if outcome is None:
            return "none"
        if outcome.error is not None:
            return "indeterminate"
        if outcome.verdict is None or outcome.verdict.package_id is None:
            return "none"
        return outcome.verdict.verdict

    def _reconciliation(self, legacy_verdict: str) -> str:
        if self.status == "not_run":
            return "none"
        outcome = self._outcome()
        if outcome is not None and outcome.error is not None:
            return "undecided"
        if outcome is None or outcome.reconciliation is None:
            return "fallback_to_legacy"
        reconciliation = outcome.reconciliation
        rejected = [d for d in reconciliation.decisions if not d.accepted]
        if rejected and all(d.legacy_decided for d in rejected):
            # Every rejection came from the legacy verifier on a criterion the
            # package could not verify.
            return "legacy_decided_unverified"
        if not outcome.package_decided:
            return "fallback_to_legacy"
        accepted = outcome.reconciliation.run_accepted
        legacy_accepted = legacy_verdict == "accept"
        if accepted == legacy_accepted:
            return "agree"
        if accepted:
            return "package_accepted_over_legacy_reject"
        return "package_rejected_over_legacy_accept"

    def finish(self, terminal_status: str | None, *, surface: str | None = None) -> None:
        """Close the run's check package record once the final verdict exists.

        On a terminal status (``completed``, ``failed``, ``cancelled``; never
        ``paused``) after the authority decided, the in-process state that
        still holds the held-out cases is dropped (the authority already did
        for its own), and the criteria accepted without evidence are counted
        once per run (``report_no_evidence``; ``surface`` names the caller),
        with what the artifact checks observed (``report_artifact_checks``).
        """
        if terminal_status in ("completed", "failed", "cancelled") and (
            self.authority is None or self.authority.outcome is not None
        ):
            forget_live_state(self.state)
        outcome = self._outcome()
        if (
            terminal_status in ("completed", "failed", "cancelled")
            and outcome is not None
            and not self._no_evidence_reported
        ):
            self._no_evidence_reported = True
            report_no_evidence(
                outcome,
                surface=surface,
                check_package="on" if self.enabled or self.resumed is not None else "off",
                check_package_status=self.status,
                runtime_backend=self.runtime_backend,
            )
            self._report_artifact_checks(outcome, surface)

    def _report_artifact_checks(self, outcome: AuthorityOutcome, surface: str | None) -> None:
        """One ``acceptance_artifact_checks`` row when this run's authority ran the checks."""
        authority = self.authority
        reconciliation = outcome.reconciliation
        if authority is None or not authority.artifact_findings or reconciliation is None:
            return
        report_artifact_checks(
            authority.artifact_findings,
            failed_criteria=sum(1 for d in reconciliation.decisions if d.artifact_check),
            criterion_count=len(reconciliation.decisions),
            repairs=authority.gate.artifact_repairs,
            surface=surface,
            runtime_backend=self.runtime_backend,
        )

    async def outcome_meta(
        self,
        event_store: EventStore,
        *,
        execution_id: str | None,
        session_id: str | None,
        terminal_status: str | None,
        verdict_available: bool = True,
        surface: str | None = None,
    ) -> dict[str, str]:
        """The closed-value summary of this run's check package outcome (local only)."""
        self.finish(terminal_status, surface=surface)
        legacy_verdict = self._legacy_verdict(terminal_status, verdict_available=verdict_available)
        meta = {
            "check_package": "on" if self.enabled or self.resumed is not None else "off",
            "check_package_status": self.status,
            "package_verdict": self._package_verdict(),
            "legacy_verdict": legacy_verdict,
            "reconciliation": self._reconciliation(legacy_verdict),
        }
        outcome = self._outcome()
        if outcome is not None and outcome.legacy and legacy_verdict == "reject":
            meta["legacy_failure_class"] = legacy_failure_class_from_annotations(outcome.legacy)
        else:
            meta.update(
                await legacy_failure_dimensions(
                    event_store,
                    execution_id=execution_id,
                    session_id=session_id,
                    legacy_verdict=legacy_verdict,
                )
            )
        coverage = self._coverage()
        if coverage is not None:
            meta["verification_coverage"] = coverage
        return meta


class NotAppliedReason(StrEnum):
    """Why the check package does not govern an execution (closed set)."""

    PLUGIN_DISPATCH = "plugin_dispatch"
    """The execution runs in a host plugin's child session, outside this process."""


def not_applied_meta(reason: NotAppliedReason) -> dict[str, str]:
    """The result fields of an execution the check package does not govern."""
    return {"check_package": "not_applied", "check_package_reason": reason.value}


__all__ = [
    "CheckPackageRun",
    "NotAppliedReason",
    "not_applied_meta",
    "legacy_failure_class_from_annotations",
    "legacy_failure_class_from_events",
    "legacy_failure_dimensions",
]
