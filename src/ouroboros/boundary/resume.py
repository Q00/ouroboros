"""Resume with the check package on: the package decision after the controller died.

A target can kill the controller (it is a child of it), or the controller can
die for any other reason, after the worker stopped and before the package
decided. The resumed run must not fall back to the legacy verifier for the
criteria the package covers: that would turn "kill the controller" into
"skip the package".

What a resumed run may rely on is one recovery projection, computed by the
ledger from the journal alone (``ledger.recovery_projection``): every boundary
version of the run replayed through the same reducer as every write, and the
run's enabled record (``boundary.check_package.enabled``, written before
construction, with the run contract). It is exactly one of:

- off (no record of the run at all): the package was off; the legacy verifier
  decides, as it did then;
- no package (a valid lifecycle whose bound version was sealed
  ``construction_failed``): the legacy verifier decides, as it did then;
- undecidable (any lifecycle violation, duplicate, conflict, gap, or missing
  required record or field): every criterion is indeterminate
  (``boundary_record_missing``) and no check runs;
- bound (an admitted package, with its coverage, held-out checks, run contract
  and interpreter pin): then

  - in the same process (the run's task died, the process did not) the
    admitted package is still in memory (``run_wiring.live_state``); when its
    sealed id is the projection's and its pinned interpreter is the one the
    admission recorded and still verifies, the full terminal decision runs,
    held-out cases included, under the recorded run contract;
  - otherwise no check runs. The held-out cases were never written to disk,
    so no covered criterion can be a verified pass here, and running the
    visible cases would only choose between two rejections. Every covered
    criterion is indeterminate (``held_out_unavailable``, or
    ``interpreter_changed`` when the live interpreter is not the pin);
    uncovered criteria are decided by the legacy verifier, as in the live
    run.

The journal is as writable as the workspace; removing every record of the
run, the enabled record included, still reads as "off" (a documented
residual). What counts as an attempt is the live rule with the check package
on (``existing_outcomes_from_results(..., gated=True)``): a root that failed
for any reason other than the package gate is not accepted, whatever the
package says. The decision is recorded as ``boundary.acceptance.resumed``;
the frozen boundary's single-shot records (final bindings, candidate
verification) are not written again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.acceptance import (
    CriterionVerdict,
    PackageCriterionStatus,
    artifact_verdict,
    criterion_verdicts,
    reconcile_acceptance,
    render_reconciliation,
)
from ouroboros.boundary.authority import (
    AUTHORITY_ERROR_PREFIX,
    AuthorityOutcome,
    _declared_from,
    _fail_attempted,
    apply_reconciliation,
    decide_without_package,
    existing_outcomes_from_results,
)
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.binding_flow import assign_tiers, verify_with_bindings
from ouroboros.boundary.check_env import INTERPRETER_CHANGED
from ouroboros.boundary.events import ResumedPayload, RunContract
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    BoundaryOrderError,
    RecoveryBound,
    RecoveryUndecidable,
    recovery_projection,
)
from ouroboros.boundary.package import CheckPackage, seed_criterion_keys
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    BoundaryVerdict,
    forget_live_state,
    live_state,
    render_verdict,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

HELD_OUT_UNAVAILABLE = "held_out_unavailable"
BOUNDARY_RECORD_MISSING = "boundary_record_missing"


@dataclass(frozen=True, slots=True)
class ResumedBoundary:
    """What a resumed run recovered of the boundary its worker was bound to."""

    execution_id: str
    boundary_id: str
    package_id: str
    """Empty for an undecidable boundary (``BOUNDARY_RECORD_MISSING``)."""
    covered: tuple[str, ...] | None
    """The criteria admitted checks cover; ``None`` when unknown (every criterion)."""
    criterion_keys: tuple[str, ...] | None = None
    """The criterion keys the frozen manifest names (``None``: undecidable boundary)."""
    held_out_checks: frozenset[str] = frozenset()
    contract: RunContract | None = None
    """The settings the run started with (its enabled record), never the live config."""
    live: BoundaryRunState | None = None
    """The admitted run state still in this process, checked against the projection."""
    reason: str | None = None
    """Why no check runs (``None`` when the live package decides)."""

    @property
    def package(self) -> CheckPackage | None:
        return self.live.package if self.live is not None else None

    @property
    def source(self) -> str:
        """``memory`` when the live package decides, else ``journal`` (nothing runs)."""
        return "memory" if self.live is not None else "journal"


def _live_problem(live: BoundaryRunState | None, bound: RecoveryBound) -> str | None:
    """Why the in-process state cannot decide, or ``None`` when it is the projection's."""
    if live is None or not live.admitted or live.package is None:
        return HELD_OUT_UNAVAILABLE
    try:
        same_package = live.package.package_id == bound.package_id
    except Exception:  # noqa: BLE001 - a package edited after its seal is not the frozen one
        same_package = False
    if not same_package or live.contract != bound.contract:
        return HELD_OUT_UNAVAILABLE
    interpreter = live.interpreter
    if (
        interpreter.sha256 != bound.interpreter_sha256
        or interpreter.realpath_sha256 != bound.interpreter_realpath_sha256
        or interpreter.problem() is not None
    ):
        return INTERPRETER_CHANGED
    return None


async def load_resumed_boundary(
    event_store: EventStore, execution_id: str
) -> ResumedBoundary | None:
    """The boundary a resumed run's worker was bound to, or ``None`` when legacy decides.

    ``None`` means the recovery projection is off or no package (see the
    module docstring). Raises ``BoundaryOrderError`` without an execution id.
    """
    if not execution_id:
        raise BoundaryOrderError("a resumed run needs its execution id to find its boundary")
    ledger = BoundaryLedger(event_store)
    projection = recovery_projection(
        execution_id, await ledger.events(execution_id), await ledger.run_versions(execution_id)
    )
    if isinstance(projection, RecoveryUndecidable):
        log.warning("boundary.resume.boundary_record_missing", detail=projection.reason)
        return ResumedBoundary(
            execution_id=execution_id,
            boundary_id=projection.boundary_id,
            package_id="",
            covered=None,
            reason=BOUNDARY_RECORD_MISSING,
        )
    if not isinstance(projection, RecoveryBound):
        return None
    live = live_state(execution_id)
    problem = _live_problem(live, projection)
    return ResumedBoundary(
        execution_id=execution_id,
        boundary_id=projection.boundary_id,
        package_id=projection.package_id,
        covered=tuple(sorted(projection.covered)),
        criterion_keys=projection.criterion_keys,
        held_out_checks=projection.held_out_checks,
        contract=projection.contract,
        live=live if problem is None else None,
        reason=problem,
    )


def _undecided(key: str, reason: str) -> CriterionVerdict:
    return CriterionVerdict(key, PackageCriterionStatus.INDETERMINATE, CheckTier.A, reason)


def _uncovered(key: str) -> CriterionVerdict:
    """Not the package's: the legacy verifier decides it."""
    return CriterionVerdict(key, PackageCriterionStatus.UNCOVERED, CheckTier.U, "uncovered")


async def decide_resumed(
    boundary: ResumedBoundary,
    *,
    seed: Seed,
    candidate: Path,
    declared: dict[str, list[Any]] | None = None,
) -> BoundaryVerdict:
    """The package's per-criterion verdicts on ``candidate`` (see the module docstring)."""
    keys = seed_criterion_keys(seed)
    if boundary.criterion_keys is not None and set(boundary.criterion_keys) != set(keys):
        # The frozen manifest names other criteria than this Seed: nothing is known.
        boundary = replace(boundary, covered=None, live=None, reason=BOUNDARY_RECORD_MISSING)
    covered = set(keys) if boundary.covered is None else set(boundary.covered)
    live = boundary.live
    if live is None or live.package is None or live.admission is None:
        reason = boundary.reason or HELD_OUT_UNAVAILABLE
        return _verdict(
            boundary,
            {key: _undecided(key, reason) if key in covered else _uncovered(key) for key in keys},
        )
    package = live.package
    assignments, _results = await assign_tiers(
        package,
        base=live.base_snapshot,
        declared=declared,
        expected_base_digest=live.admission.base_tree_digest,
        admitted_tiers=live.admission.check_tiers,
        run_options={"interpreter": live.interpreter},
    )
    bound = await verify_with_bindings(
        package,
        candidate,
        assignments,
        timeout_seconds=live.contract.check_timeout_seconds,
        interpreter=live.interpreter,
    )
    computed = criterion_verdicts(package, bound.effective, assignments=assignments)
    verdicts: dict[str, CriterionVerdict] = {}
    for key in keys:
        item = computed.get(key)
        if key not in covered:
            verdicts[key] = _uncovered(key)
        elif item is None or item.status is PackageCriterionStatus.UNCOVERED:
            # The journal says an admitted check covers it; the package disagrees.
            verdicts[key] = _undecided(key, BOUNDARY_RECORD_MISSING)
        else:
            verdicts[key] = item
    return _verdict(boundary, verdicts)


def _verdict(boundary: ResumedBoundary, verdicts: dict[str, CriterionVerdict]) -> BoundaryVerdict:
    overall = artifact_verdict(item.status for item in verdicts.values())
    return BoundaryVerdict(
        verdict=overall.value,
        reasons=(f"resumed:{boundary.source}",),
        boundary_id=boundary.boundary_id,
        package_id=boundary.package_id or None,
        criteria={key: item.status for key, item in verdicts.items()},
        verdicts=verdicts,
        artifact_verdict=overall,
    )


class ResumedCheckPackageAuthority:
    """The acceptance authority of a resumed run (installed as the runner's)."""

    def __init__(
        self,
        boundary: ResumedBoundary,
        *,
        event_store: EventStore,
        candidate_checkout: Path,
    ) -> None:
        self.boundary = boundary
        self._event_store = event_store
        self._candidate = candidate_checkout
        self.outcome: AuthorityOutcome | None = None

    async def __call__(self, *, seed: Seed, execution_id: str, parallel_result: Any) -> Any:
        if self.outcome is not None:
            return parallel_result
        keys = seed_criterion_keys(seed)
        try:
            return await self._decide(seed, keys, parallel_result)
        except Exception as exc:  # noqa: BLE001 - fail closed below, never open
            log.warning(
                "boundary.resume.failed",
                execution_id=execution_id,
                boundary_id=self.boundary.boundary_id,
                error_type=type(exc).__name__,
            )
            return self._undecided(keys, parallel_result, type(exc).__name__)
        finally:
            live = live_state(execution_id)
            if self.outcome is not None and live is not None:
                forget_live_state(live)

    async def _decide(self, seed: Seed, keys: tuple[str, ...], parallel_result: Any) -> Any:
        # The live rule: only a root that succeeded, or that only the package
        # gate failed, is an attempt the package may accept. A runtime
        # failure, a failed verify command, or a resumed attempt the
        # (ungated) legacy verifier rejected is never accepted here.
        legacy = existing_outcomes_from_results(parallel_result, gated=True)
        declared: dict[str, list[Any]] = {}
        for result in getattr(parallel_result, "results", ()) or ():
            index = getattr(result, "ac_index", -1)
            entries = _declared_from(result) if 0 <= index < len(keys) else []
            if entries:
                declared[keys[index]] = entries
        verdict = await decide_resumed(
            self.boundary,
            seed=seed,
            candidate=self._candidate.resolve(),
            declared=declared,
        )
        reconciliation = reconcile_acceptance(
            keys,
            verdict.verdicts,
            legacy,
            existing_run_accepted=bool(parallel_result.all_succeeded),
            legacy_decides_unverified=True,
        )
        record = {
            **reconciliation.to_dict(),
            "source": self.boundary.source,
            "held_out_checks": sorted(self.boundary.held_out_checks),
        }
        ledger = BoundaryLedger(self._event_store)
        if self.boundary.package_id:
            await ledger.record_acceptance_resumed(
                self.boundary.boundary_id,
                package_id=self.boundary.package_id,
                payload=ResumedPayload.model_validate(record),
            )
        else:
            # No boundary version can be cited: record it on the run's aggregate.
            await ledger.record_resumed_undecided(
                self.boundary.execution_id,
                payload=ResumedPayload.model_validate({**record, "reason": self.boundary.reason}),
            )
        self.outcome = AuthorityOutcome(
            bool(legacy) and all(item.passed for item in legacy.values()),
            verdict=verdict,
            reconciliation=reconciliation,
            legacy=legacy,
        )
        return apply_reconciliation(parallel_result, reconciliation)

    def _undecided(self, keys: tuple[str, ...], parallel_result: Any, error: str) -> Any:
        """The live authority's fail-closed rule: covered criteria undecided, the rest legacy."""
        try:
            decided, reconciliation, legacy = decide_without_package(
                keys,
                set(keys) if self.boundary.covered is None else set(self.boundary.covered),
                f"{AUTHORITY_ERROR_PREFIX}{error}",
                parallel_result,
                gated=True,
            )
        except Exception:  # noqa: BLE001 - no decision could be built: fail every root
            log.warning("boundary.resume.undecided_failed", error_type=error)
            self.outcome = AuthorityOutcome(False, error=error)
            return _fail_attempted(parallel_result)
        self.outcome = AuthorityOutcome(
            bool(legacy) and all(item.passed for item in legacy.values()),
            reconciliation=reconciliation,
            error=error,
            legacy=legacy,
        )
        return decided

    def render(self) -> list[str]:
        """Lines for the person running the command."""
        outcome = self.outcome
        if outcome is None:
            return []
        if outcome.error is not None:
            lines = [
                f"Check package could not be recomputed on resume ({outcome.error}); "
                "covered criteria are undecided, the legacy verifier decided the rest."
            ]
            if outcome.reconciliation is not None:
                lines.extend(render_reconciliation(outcome.reconciliation))
            return lines
        if self.boundary.source == "memory":
            lines = ["Check package recomputed on resume from memory (held-out cases available)."]
        else:
            lines = [
                "Check package not re-run on resume "
                f"({self.boundary.reason}): covered criteria are undecided, the legacy "
                "verifier decided the rest."
            ]
        if outcome.verdict is not None:
            lines.extend(render_verdict(outcome.verdict))
        if outcome.reconciliation is not None:
            lines.extend(render_reconciliation(outcome.reconciliation))
        return lines


__all__ = [
    "BOUNDARY_RECORD_MISSING",
    "HELD_OUT_UNAVAILABLE",
    "ResumedBoundary",
    "ResumedCheckPackageAuthority",
    "decide_resumed",
    "load_resumed_boundary",
]
