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
  ``construction_failed``): the legacy verifier decides, as it did then,
  except that the criteria the run's journaled decision failed through an
  artifact check (``boundary/base_regression.py``) fail again;
- undecidable (any lifecycle violation, duplicate, conflict, gap, or missing
  required record or field): every criterion is indeterminate
  (``boundary_record_missing``) and no check runs;
- bound (an admitted package, with its coverage, held-out checks, run contract
  and interpreter pin): then

  - in the same process (the run's task died, the process did not) the
    admitted package is still in memory (``run_wiring.live_state``); when its
    sealed id is the projection's and its pinned interpreter is the one the
    admission recorded and still verifies, the full terminal decision runs,
    held-out cases included, under the recorded run contract and the bound
    version's admission (``run_wiring.verify_check_package``, the live rule:
    a verified pass needs a passing held-out case the base failed). It is
    journaled like a fresh one, through the same gateway: the resume's own
    bindings (``phase="resumed"``), its candidate verification (and re-run),
    then its decision, which the reducer judges by that recorded run;
  - otherwise no check runs. The held-out cases were never written to disk,
    so no covered criterion can be a verified pass here, and running the
    visible cases would only choose between two rejections. Every covered
    criterion is indeterminate (``held_out_unavailable``, or
    ``interpreter_changed`` when the live interpreter is not the pin);
    uncovered criteria are decided by the legacy verifier, as in the live
    run. Only the decision is recorded: it claims nothing but indeterminate
    and uncovered statuses.

The resumed authority belongs to the run the projection names: its execution
id, the Seed digest the package was sealed for, and the frozen criterion keys
in order (``authority.mismatched_run``, the live authority's rule). A call for
any other run is refused before any check runs and before anything is
recorded (``run_mismatch:<field>``): covered criteria are undecided, the
legacy verifier decides the rest, and the run's one decision is not used. An
undecidable boundary names no Seed, so only its execution id is checked. With
the package in memory, the same refusal (``package_record_changed``) follows
when the package, or the record the store holds for it, does not have the
digest the frozen record journaled (``record_sha256``), or the stored record
does not name the Seed's criteria in order.

Artifact checks are never run again on resume: the fails a journaled
decision of the bound version recorded (``artifact_check``) are replayed onto
every criterion the resumed decision leaves unverified or uncovered
(``base_regression.replay_recorded``); a journal without them changes nothing.

The journal is as writable as the workspace; removing every record of the
run, the enabled record included, still reads as "off" (a documented
residual). What counts as an attempt is the live rule with the check package
on (``existing_outcomes_from_results(..., gated=True)``): a root that failed
for any reason other than the package gate is not accepted, whatever the
package says. The decision is recorded as ``boundary.acceptance.resumed``;
the live run's final bindings are not written again.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.acceptance import (
    ArtifactCheck,
    CriterionVerdict,
    PackageCriterionStatus,
    artifact_verdict,
    attempted_keys,
    reconcile_acceptance,
    render_reconciliation,
)
from ouroboros.boundary.authority import (
    AUTHORITY_ERROR_PREFIX,
    RUN_MISMATCH_PREFIX,
    AuthorityOutcome,
    RunIdentity,
    _declared_from,
    _fail_attempted,
    _legacy_owned,
    apply_reconciliation,
    decide_without_package,
    existing_outcomes_from_results,
    mismatched_run,
)
from ouroboros.boundary.base_regression import replay_recorded
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.check_env import INTERPRETER_CHANGED
from ouroboros.boundary.events import (
    PACKAGE_FROZEN,
    ResumedPayload,
    RunContract,
    parse_boundary_version,
)
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    BoundaryOrderError,
    RecoveryBound,
    RecoveryNoPackage,
    RecoveryUndecidable,
    recovery_projection,
    version_state,
)
from ouroboros.boundary.package import (
    CheckPackage,
    package_record_bytes,
    seed_criterion_keys,
    sha256_bytes,
)
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    BoundaryVerdict,
    forget_live_state,
    live_state,
    render_verdict,
    verify_check_package,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

HELD_OUT_UNAVAILABLE = "held_out_unavailable"
BOUNDARY_RECORD_MISSING = "boundary_record_missing"
PACKAGE_RECORD_CHANGED = "package_record_changed"
NO_ADMITTED_PACKAGE = "no_admitted_package"
"""Reason of a resumed run bound to no package whose decision recorded artifact-check fails."""


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
    """The criterion keys the frozen manifest names, in order (``None``: undecidable boundary)."""
    seed_digest: str | None = None
    """The Seed the package was sealed for (``None``: undecidable boundary)."""
    record_sha256: str | None = None
    """The digest of the stored package record the frozen record journaled."""
    held_out_checks: frozenset[str] = frozenset()
    contract: RunContract | None = None
    """The settings the run started with (its enabled record), never the live config."""
    live: BoundaryRunState | None = None
    """The admitted run state still in this process, checked against the projection."""
    reason: str | None = None
    """Why no check runs (``None`` when the live package decides)."""
    recorded_artifact_checks: tuple[tuple[str, ArtifactCheck], ...] = ()
    """``(criterion key, check)`` of every fail an artifact check made in the bound
    version's journaled decision; replayed, never run again."""

    @property
    def package(self) -> CheckPackage | None:
        return self.live.package if self.live is not None else None

    @property
    def run(self) -> RunIdentity | None:
        """The run the projection names; ``None`` for an undecidable boundary."""
        if self.seed_digest is None or self.criterion_keys is None:
            return None
        return (self.execution_id, self.seed_digest, self.criterion_keys)

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
    versions = await ledger.run_versions(execution_id)
    projection = recovery_projection(execution_id, await ledger.events(execution_id), versions)
    if isinstance(projection, RecoveryNoPackage):
        parsed = parse_boundary_version(projection.boundary_id)
        assert parsed is not None
        recorded = _recorded_artifact_checks(versions[parsed[1]])
        if not recorded:
            return None
        return ResumedBoundary(
            execution_id=execution_id,
            boundary_id=projection.boundary_id,
            package_id="",
            covered=(),
            contract=projection.contract,
            reason=NO_ADMITTED_PACKAGE,
            recorded_artifact_checks=recorded,
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
    # The projection replayed every record through the journal gateway: the
    # bound version holds exactly one frozen record, with its record digest.
    parsed = parse_boundary_version(projection.boundary_id)
    assert parsed is not None
    (frozen,) = [event for event in versions[parsed[1]] if event.type == PACKAGE_FROZEN]
    return ResumedBoundary(
        execution_id=execution_id,
        boundary_id=projection.boundary_id,
        package_id=projection.package_id,
        covered=tuple(sorted(projection.covered)),
        criterion_keys=projection.criterion_keys,
        seed_digest=projection.seed_digest,
        record_sha256=str(frozen.data["record_sha256"]),
        held_out_checks=projection.held_out_checks,
        contract=projection.contract,
        live=live if problem is None else None,
        reason=problem,
        recorded_artifact_checks=_recorded_artifact_checks(versions[parsed[1]]),
    )


def _recorded_artifact_checks(events: Any) -> tuple[tuple[str, ArtifactCheck], ...]:
    """The artifact-check fails of a version's journaled decision (none without one)."""
    decision = version_state(events).decision
    return tuple(
        (item.criterion_key, ArtifactCheck(item.artifact_check))
        for item in (decision.criteria if decision is not None else ())
        if item.artifact_check is not None
    )


def _undecided(key: str, reason: str) -> CriterionVerdict:
    return CriterionVerdict(
        key, PackageCriterionStatus.INDETERMINATE, CheckTier.A, reason, declared_binding_pass=False
    )


def _uncovered(key: str) -> CriterionVerdict:
    """Not the package's: the legacy verifier decides it."""
    return CriterionVerdict(
        key, PackageCriterionStatus.UNCOVERED, CheckTier.U, "uncovered", declared_binding_pass=False
    )


async def decide_resumed(
    boundary: ResumedBoundary,
    *,
    seed: Seed,
    candidate: Path,
    event_store: EventStore,
    declared: dict[str, list[Any]] | None = None,
    attempted: Collection[str] | None = None,
) -> BoundaryVerdict:
    """The package's per-criterion verdicts on ``candidate`` (see the module docstring).

    With the package in memory the resume's bindings and verification are
    recorded on the bound version (``verify_check_package``); otherwise
    nothing runs and nothing is recorded here.
    """
    keys = seed_criterion_keys(seed)
    run = boundary.run
    mismatch = None if run is None else mismatched_run(run, seed, None)
    if run is not None and mismatch is not None:
        # Not the run's Seed: nothing runs. When the criteria are not the
        # run's either, coverage is unknown and every criterion counts as covered.
        covered_now = boundary.covered if keys == run[2] else None
        boundary = replace(boundary, covered=covered_now, live=None, reason=mismatch)
    covered = set(keys) if boundary.covered is None else set(boundary.covered)
    live = boundary.live
    if live is None or live.package is None or live.admission is None:
        reason = boundary.reason or HELD_OUT_UNAVAILABLE
        return _verdict(
            boundary,
            {key: _undecided(key, reason) if key in covered else _uncovered(key) for key in keys},
            keys,
            attempted,
        )
    computed = (
        await verify_check_package(
            live,
            event_store=event_store,
            candidate_checkout=candidate,
            declared_entry_points=declared,
            phase="resumed",
        )
    ).verdicts
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
    return _verdict(boundary, verdicts, keys, attempted)


def _verdict(
    boundary: ResumedBoundary,
    verdicts: dict[str, CriterionVerdict],
    keys: Sequence[str],
    attempted: Collection[str] | None,
) -> BoundaryVerdict:
    # Replayed only onto this Seed's criteria the worker attempted; without a
    # package a regression never fails anything, whatever a journal holds.
    recorded = {
        key: check
        for key, check in boundary.recorded_artifact_checks
        if boundary.reason != NO_ADMITTED_PACKAGE or check is not ArtifactCheck.BASE_REGRESSION
    }
    verdicts = replay_recorded(verdicts, recorded, keys, attempted)
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
        # Set once the resumed decision starts (held-out cases may run).
        self._terminal_started = False

    async def __call__(self, *, seed: Seed, execution_id: str, parallel_result: Any) -> Any:
        if self.outcome is not None:
            return parallel_result
        mismatch = self._run_mismatch(seed, execution_id) or self._record_changed(seed)
        if mismatch is not None:
            return self._refuse_foreign_run(seed, execution_id, parallel_result, mismatch)
        if self._terminal_started:
            # An earlier resumed verification was interrupted after its
            # held-out cases may have reached the candidate: they decide nothing again.
            return self._undecided(seed, parallel_result, "terminal_interrupted")
        self._terminal_started = True
        try:
            return await self._decide(seed, self._keys(seed), parallel_result)
        except Exception as exc:  # noqa: BLE001 - fail closed below, never open
            log.warning(
                "boundary.resume.failed",
                execution_id=execution_id,
                boundary_id=self.boundary.boundary_id,
                error_type=type(exc).__name__,
            )
            return self._undecided(seed, parallel_result, type(exc).__name__)
        finally:
            # Once the resumed decision starts, decided or interrupted
            # (cancelled included), nothing in this process re-derives the held-out cases.
            forget_live_state(live_state(self.boundary.execution_id))

    def _run_mismatch(self, seed: Seed, execution_id: str) -> str | None:
        """``run_mismatch:<field>`` when a call is not for the projected run, else ``None``."""
        run = self.boundary.run
        if run is not None:
            return mismatched_run(run, seed, execution_id)
        # An undecidable boundary names no Seed: only the run can be checked.
        if execution_id != self.boundary.execution_id:
            return f"{RUN_MISMATCH_PREFIX}execution_id"
        return None

    def _record_changed(self, seed: Seed) -> str | None:
        """``package_record_changed`` unless the package in memory is the one the journal sealed.

        The package and the record the store holds for it must both have the
        frozen record's digest, and the stored record must name the Seed's
        criteria in order. Without the package in memory nothing runs.
        """
        live = self.boundary.live
        if live is None:
            return None
        try:
            assert live.package is not None and live.package_path is not None
            stored = live.package_path.read_bytes()
            expected = self.boundary.record_sha256
            if (
                sha256_bytes(stored) != expected
                or sha256_bytes(package_record_bytes(live.package)) != expected
                or tuple(json.loads(stored)["package"]["criterion_keys"])
                != seed_criterion_keys(seed)
            ):
                return PACKAGE_RECORD_CHANGED
        except Exception:  # noqa: BLE001 - a record that cannot be read is not the sealed one
            return PACKAGE_RECORD_CHANGED
        return None

    def _keys(self, seed: Seed) -> tuple[str, ...]:
        """The run's criterion keys: the projection's (checked), or the Seed's when undecidable."""
        return self.boundary.criterion_keys or seed_criterion_keys(seed)

    def _refuse_foreign_run(
        self, seed: Seed, execution_id: str, parallel_result: Any, mismatch: str
    ) -> Any:
        """Another run's call: covered criteria undecided, the rest legacy, nothing recorded.

        The live authority's rule: this run's one decision is not used and its
        journal is not written. When the criteria are not the run's, coverage
        is unknown, so every criterion counts as covered.
        """
        log.warning(
            "boundary.resume.foreign_run",
            execution_id=execution_id,
            boundary_id=self.boundary.boundary_id,
            mismatch=mismatch,
        )
        try:
            keys = seed_criterion_keys(seed)
            covered = (
                self.boundary.covered
                if self.boundary.covered is not None and keys == self.boundary.criterion_keys
                else keys
            )
            decided, _reconciliation, _legacy = decide_without_package(
                keys, set(covered), mismatch, parallel_result, gated=True
            )
        except Exception:  # noqa: BLE001 - no criteria can be read: fail every root
            return _fail_attempted(parallel_result)
        return decided

    async def _decide(self, seed: Seed, keys: tuple[str, ...], parallel_result: Any) -> Any:
        # The live rule: only a root that succeeded, or that only the package
        # gate failed, is an attempt the package may accept. A runtime
        # failure, a failed verify command, or a resumed attempt the
        # (ungated) legacy verifier rejected is never accepted here.
        # A run bound to no package had no gate: its attempts are the legacy run's.
        gated = self.boundary.reason != NO_ADMITTED_PACKAGE
        legacy = existing_outcomes_from_results(parallel_result, gated=gated)
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
            event_store=self._event_store,
            declared=declared,
            attempted=attempted_keys(
                keys, legacy, existing_run_accepted=bool(parallel_result.all_succeeded)
            ),
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
        if self.boundary.reason == NO_ADMITTED_PACKAGE:
            # Bound to no package: the legacy verifier owns every criterion
            # but the replayed fails, which the live decision already journaled.
            reconciliation = _legacy_owned(reconciliation)
        elif self.boundary.package_id:
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

    def _undecided(self, seed: Seed, parallel_result: Any, error: str) -> Any:
        """The live authority's fail-closed rule: covered criteria undecided, the rest legacy."""
        try:
            keys = self._keys(seed)
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
        if self.boundary.reason == NO_ADMITTED_PACKAGE:
            lines = [
                "No admitted package on resume: the artifact-check fails the run recorded "
                "are replayed, the legacy verifier decided the rest."
            ]
        elif self.boundary.source == "memory":
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
    "NO_ADMITTED_PACKAGE",
    "PACKAGE_RECORD_CHANGED",
    "ResumedBoundary",
    "ResumedCheckPackageAuthority",
    "decide_resumed",
    "load_resumed_boundary",
]
