"""EventStore-backed ordering guard for the check-package boundary.

A boundary is one (Seed, verifier source) slot, for example one task and one
check variant. Its lifecycle is one reducer (``advance``), applied at write
time to the event about to be appended and at replay time by
``verify_boundary_order``:

1. exactly one seal: ``record_package_frozen`` (the sealed package's opaque
   id and Seed digest persisted, ``package.seal_package``) or
   ``record_construction_failed``; a second package for the same boundary is
   refused, so admission feedback can never produce a regenerated package;
2. for a frozen package, exactly one ``record_admission`` whose receipt names
   the frozen package id and Seed digest, recorded before any actor starts;
   the frozen manifest must be one the product writes (``frozen_manifest``)
   and an ``admitted`` receipt one admission can write
   (``admitted_exclusions``: a tier and one result per check, exclusions only
   for a role violation or a per-check indeterminate result on the base);
3. ``record_actor_started`` refuses to start a worker until every boundary it
   binds is sealed (and, with a package, admitted) and not superseded, and
   every later version of its run was abandoned in its favor, and
   starts at most one worker per version; a
   version of a run binds only that run's worker; with a workspace it
   refuses one that contains a generated check file (scanned with the live
   sealed packages, so a renamed copy of the oracle data file is found);
4. every later record (bindings, candidate verification, acceptance) must
   cite the frozen package id (a receipt also its Seed digest) and name only
   the frozen checks and criteria (a decision exactly the frozen criteria),
   and a superseded version accepts none; before any transition reads a
   record, the journal gateway (``events.validate_record``) validates its
   envelope, its exact closed schema, and these identities;
5. a version is superseded only by a later version of the same run;
6. every record is one the product could write in that state: final
   bindings bind every frozen check once, at a tier ``assign_tiers`` can
   give it; a verification runs only the runnable bound checks (a re-run
   only those that ended indeterminate); a decision records only the
   package statuses the recorded results support (a pass needs a held-out
   case the base failed at admission), and a criterion the package did not
   verify is the existing verifier's own verdict;
7. a resumed run records its own bindings (``resumed``), then the
   verification it ran, then its decision, judged by the same rule; a
   resumed decision without them verified nothing and records only
   undecided statuses.

A boundary version of a run (``events.boundary_version_id``) is sealed only
after the run's ``record_check_package_enabled``, and that record is refused
once a version of the run exists, so it is always the run's first record.

Recovery. ``recovery_projection`` is the one thing a resumed run relies on:
the run's records replayed through the same reducer, reduced to off, no
package, a bound admitted package (coverage, held-out checks, run contract,
interpreter pin), or undecidable for anything the product could not have
written, including a version history other than superseded predecessors
followed by the one bound version, last but for replacement versions recorded
abandoned in its favor before its worker started.

Regeneration policy. The seal rule above is per boundary id and never
changes. The product run path gives each attempt its own boundary version id
and calls ``record_superseded`` on the old version once the new one is
sealed; the old package stays in the journal, marked superseded. A
replacement version that is not admitted is closed instead
(``record_replacement_abandoned``), naming the version the worker stays
bound to; a worker starts only on the last version of its run that is not
abandoned. The ledger
assumes one writer per boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ouroboros.boundary.binding import BindingSource, CheckTier
from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    ACCEPTANCE_RESUMED,
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BINDING_RECORDED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    CHECK_PACKAGE_ENABLED,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    REFERENCE_CHECKED,
    REPLACEMENT_ABANDONED,
    SUPERSEDED,
    AbandonedRecord,
    ActorStartedRecord,
    AdmissionRecord,
    BindingsPayload,
    BindingsRecord,
    CheckBindingRecord,
    ConstructionFailedRecord,
    EnabledRecord,
    FrozenIdentity,
    FrozenRecord,
    JournalRecordError,
    ReconciledRecord,
    ReconciliationPayload,
    ReferenceCheckedRecord,
    ReferenceCheckPayload,
    ResumedPayload,
    ResumedRecord,
    RunContract,
    SupersededRecord,
    VerificationRecord,
    acceptance_reconciled_event,
    acceptance_resumed_event,
    actor_started_event,
    admission_completed_event,
    binding_recorded_event,
    boundary_version_id,
    candidate_verified_event,
    check_package_enabled_event,
    construction_failed_event,
    enabled_contract,
    package_frozen_event,
    parse_boundary_version,
    reference_checked_event,
    replacement_abandoned_event,
    superseded_event,
    validate_record,
    validate_run_record,
)
from ouroboros.boundary.oracle import case_id_for
from ouroboros.boundary.package import (
    CheckPackage,
    CheckRole,
    find_workspace_leaks,
    mint_check_ids,
    script_assertion_id,
    validate_package_for_seed,
)
from ouroboros.boundary.per_check import (
    ALL_CHECKS_EXCLUDED,
    EXCLUDED_STATUS_HINT,
    HELD_OUT_NOT_DISCRIMINATING,
    ROLE_EXCLUSION_REASONS,
    base_failing_held_out,
    criteria_without_admitted_check,
    exclusion_reason,
    held_out_all_passed,
)
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerification,
    CheckStatus,
    JournalCheckExecution,
    JournalOracleResult,
)
from ouroboros.core.errors import OuroborosError
from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore


class BoundaryOrderError(OuroborosError):
    """A boundary write would violate the seal, admission, or actor ordering."""


class BoundaryLeakError(BoundaryOrderError):
    """A worker workspace contains generated check files."""


def _utc(value: datetime) -> datetime:
    """Replayed SQLite timestamps are naive UTC; compare everything as aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _first(events: Sequence[BaseEvent], event_type: str) -> BaseEvent | None:
    return next((event for event in events if event.type == event_type), None)


# --------------------------------------------------------------------------
# The frozen manifest and the admission record, as the reducer reads them.


@dataclass(frozen=True, slots=True)
class ManifestLink:
    criterion_key: str


@dataclass(frozen=True, slots=True)
class ManifestCheck:
    """One check of the frozen manifest: its id, role and linked criteria."""

    check_id: str
    role: CheckRole
    assertions: tuple[ManifestLink, ...]


@dataclass(frozen=True, slots=True)
class FrozenManifest:
    """What the frozen event recorded before any worker started (``manifest_summary``).

    Shaped like a package for the coverage rule
    (``per_check.criteria_without_admitted_check``).
    """

    criterion_keys: tuple[str, ...]
    checks: tuple[ManifestCheck, ...]
    held_out_checks: frozenset[str]
    """Oracle checks that had at least one held-out case."""
    oracle_checks: frozenset[str] = frozenset()
    """Checks that run an oracle (every other check is a model-written script)."""
    oracle_cases: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    """Per oracle check, its case count and held-out case count."""
    named_targets: frozenset[str] = frozenset()
    """Oracle checks whose criterion names their target (``target_named_in_criterion``)."""


def _strings(value: object, what: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise BoundaryOrderError(f"the frozen manifest's {what} are malformed")
    return tuple(value)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def frozen_manifest(data: Mapping[str, Any]) -> FrozenManifest:
    """The manifest a frozen event carries; ``BoundaryOrderError`` unless the product could write it.

    The product writes it from a sealed package (``package_frozen_event``):
    every criterion is linked to a check or uncovered, never both; every
    oracle belongs to one of its checks and names one of that check's
    criteria; and the record digest (``record_sha256``) is present.
    """
    manifest = data.get("manifest")
    if not isinstance(manifest, Mapping):
        raise BoundaryOrderError("the frozen event has no manifest")
    if not _is_digest(data.get("record_sha256")):
        raise BoundaryOrderError("the frozen event has no record digest")
    if manifest.get("package_id") != data.get("package_id") or manifest.get(
        "seed_digest"
    ) != data.get("seed_digest"):
        raise BoundaryOrderError("the frozen manifest names another package or Seed")
    keys = _strings(manifest.get("criterion_keys"), "criterion keys")
    raw_checks = manifest.get("checks")
    if not isinstance(raw_checks, list):
        raise BoundaryOrderError("the frozen manifest's checks are malformed")
    checks: list[ManifestCheck] = []
    for item in raw_checks:
        if not isinstance(item, Mapping) or not isinstance(item.get("check_id"), str):
            raise BoundaryOrderError("the frozen manifest's checks are malformed")
        linked = _strings(item.get("criterion_keys"), "check links")
        _strings(item.get("assertion_ids"), "assertion ids")
        try:
            role = CheckRole(item.get("role"))
        except ValueError as exc:
            raise BoundaryOrderError("the frozen manifest names an unknown role") from exc
        if not item["check_id"] or not set(linked) <= set(keys):
            raise BoundaryOrderError("the frozen manifest links an unknown criterion")
        checks.append(
            ManifestCheck(item["check_id"], role, tuple(ManifestLink(key) for key in linked))
        )
    by_id = {check.check_id: check for check in checks}
    if len(by_id) != len(checks):
        raise BoundaryOrderError("the frozen manifest repeats a check id")
    uncovered_raw = manifest.get("uncovered")
    if not isinstance(uncovered_raw, list) or not all(
        isinstance(item, Mapping) for item in uncovered_raw
    ):
        raise BoundaryOrderError("the frozen manifest's uncovered criteria are malformed")
    uncovered = [item.get("criterion_key") for item in uncovered_raw]
    linked_keys = {link.criterion_key for check in checks for link in check.assertions}
    if (
        len(set(uncovered)) != len(uncovered)
        or linked_keys & set(uncovered)
        or linked_keys | set(uncovered) != set(keys)
    ):
        raise BoundaryOrderError("the frozen manifest does not link or uncover every criterion")
    oracles = manifest.get("oracles", [])
    if not isinstance(oracles, list):
        raise BoundaryOrderError("the frozen manifest's oracles are malformed")
    held: set[str] = set()
    oracle_ids: set[str] = set()
    oracle_cases: dict[str, tuple[int, int]] = {}
    named: set[str] = set()
    for spec in oracles:
        check = by_id.get(spec.get("check_id")) if isinstance(spec, Mapping) else None
        count = spec.get("held_out_count") if isinstance(spec, Mapping) else None
        if (
            check is None
            or spec.get("criterion_key") not in {link.criterion_key for link in check.assertions}
            or type(count) is not int
            or count < 0
        ):
            raise BoundaryOrderError("the frozen manifest names a malformed oracle")
        if check.check_id in oracle_ids:
            raise BoundaryOrderError("the frozen manifest names an oracle twice")
        oracle_ids.add(check.check_id)
        cases = spec.get("case_count")
        oracle_cases[check.check_id] = (cases if type(cases) is int else 0, count)
        if spec.get("target_named_in_criterion") is True:
            named.add(check.check_id)
        if count:
            held.add(check.check_id)
    if not _minted(raw_checks, oracles, keys):
        raise BoundaryOrderError(
            "the frozen manifest names a check by an id the product never mints"
        )
    return FrozenManifest(
        keys, tuple(checks), frozenset(held), frozenset(oracle_ids), oracle_cases, frozenset(named)
    )


def _minted(
    checks: Sequence[Mapping[str, Any]], oracles: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> bool:
    """Every id is the one ``package.mint_check_ids`` gives the manifest's structure.

    Oracles in their order, by the position of their criterion; script
    checks in package order, by the position of the criterion they first
    link. The manifest lists a check's links as a set, so a script check
    linking several criteria is matched against the id the mint gives each
    of them (at most one can match). Each oracle check links only its
    criterion with one assertion per case (``<id>.c1..c<n>``); each script
    check's assertions are ``<id>.a1..a<m>``.
    """
    numbers = {key: number for number, key in enumerate(keys, start=1)}
    specs = {spec["check_id"]: spec for spec in oracles}
    oracle_numbers = [numbers.get(spec.get("criterion_key"), 0) for spec in oracles]
    if 0 in oracle_numbers:
        return False
    expected_oracles, _ = mint_check_ids(oracle_numbers, [])
    if [spec["check_id"] for spec in oracles] != expected_oracles:
        return False
    script_numbers: list[int] = []
    for item in checks:
        check_id, ids = item["check_id"], list(item["assertion_ids"])
        spec = specs.get(check_id)
        if spec is not None:
            count = spec.get("case_count")
            if type(count) is not int:
                return False
            cases = [f"{check_id}.{case_id_for(n)}" for n in range(1, count + 1)]
            if list(item["criterion_keys"]) != [spec["criterion_key"]] or ids != cases:
                return False
            continue
        linked = sorted(numbers[key] for key in item["criterion_keys"])
        chosen = [
            number
            for number in linked
            if mint_check_ids([], [*script_numbers, number])[1][-1] == check_id
        ]
        if len(chosen) != 1:
            return False
        script_numbers.append(chosen[0])
        if ids != [script_assertion_id(check_id, n) for n in range(1, len(ids) + 1)]:
            return False
    scripts = [item["check_id"] for item in checks if item["check_id"] not in specs]
    return mint_check_ids(oracle_numbers, script_numbers) == (expected_oracles, scripts)


def _not_discriminating(manifest: FrozenManifest, check_id: str, item: Mapping[str, Any]) -> bool:
    """An oracle check whose recorded base run passed every held-out case it ran."""
    try:
        result = JournalOracleResult.model_validate(item.get("oracle_result"))
    except ValidationError:
        return False
    return check_id in manifest.oracle_checks and held_out_all_passed(result)


# Tiers admission records (``admission.base_run_tiers``): an oracle check is
# ``A`` or ``U`` from its base run, a script check ``S``, and any check ``C``
# when ``per_check`` excluded it.
_ORACLE_TIERS = frozenset({"A", "U"})
_SCRIPT_TIERS = frozenset({"S"})


def admitted_exclusions(manifest: FrozenManifest, data: Mapping[str, Any]) -> frozenset[str]:
    """The excluded checks of an ``admitted`` record; ``BoundaryOrderError`` unless admission wrote it.

    ``admission.admit_check_package`` followed by ``per_check.per_check_admission``
    records an admitted package only as: no protected-byte mutation and an
    unchanged base; a tier for every check of the frozen manifest (``A`` or
    ``U`` for an oracle check, ``S`` for a script check, ``C`` for either when
    excluded); one result per check with the manifest's role; every
    excluded check (tier ``C``) exactly an ``excluded_checks`` entry,
    carrying the exclusion reason its recorded status and base reason give
    (``per_check.exclusion_reason``), one its role allows
    (``held_out_not_discriminating`` only for a reproduction oracle whose base
    run passed every held-out case); every other check ``expected``; and at
    least one check not excluded.
    """
    by_id = {check.check_id: check for check in manifest.checks}
    if data.get("protected_bytes_mutated") is not False or data.get("base_tree_digest") != data.get(
        "base_tree_digest_after"
    ):
        raise BoundaryOrderError("an admitted package's base changed during admission")
    tiers = data.get("check_tiers")
    if not isinstance(tiers, Mapping) or set(tiers) != set(by_id):
        raise BoundaryOrderError("an admission record's tiers do not cover the frozen checks")
    for check_id, tier in tiers.items():
        allowed = _ORACLE_TIERS if check_id in manifest.oracle_checks else _SCRIPT_TIERS
        if tier != "C" and tier not in allowed:
            raise BoundaryOrderError(
                "an admission record gives a check a tier its kind cannot have"
            )
    excluded = frozenset(check_id for check_id, tier in tiers.items() if tier == "C")
    recorded = data.get("excluded_checks") or {}
    if not isinstance(recorded, Mapping) or set(recorded) != excluded:
        raise BoundaryOrderError("an admission record's exclusions disagree with its tiers")
    if excluded == set(by_id):
        raise BoundaryOrderError("an admitted package excludes every check")
    results = data.get("checks")
    if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
        raise BoundaryOrderError("an admission record's check results are malformed")
    ids = [item.get("check_id") for item in results]
    if len(set(ids)) != len(ids) or set(ids) != set(by_id):
        raise BoundaryOrderError("an admission record does not hold one result per check")
    for item in results:
        check = by_id[item["check_id"]]
        if item.get("role") != check.role.value:
            raise BoundaryOrderError("an admission result names another role")
        if check.check_id in excluded:
            reason = exclusion_reason(str(item.get("status")), str(item.get("reason")))
            if (
                reason not in ROLE_EXCLUSION_REASONS[check.role]
                or recorded[check.check_id] != reason
                or (
                    reason == HELD_OUT_NOT_DISCRIMINATING
                    and not _not_discriminating(manifest, check.check_id, item)
                )
            ):
                raise BoundaryOrderError("an excluded check was not excluded for its role")
        elif item.get("status") != "expected":
            raise BoundaryOrderError("an admitted package holds a check that did not meet its role")
    return excluded


# --------------------------------------------------------------------------
# The transition reducer: the one definition of a valid boundary journal.
# Every write path advances the replayed state with the event it is about to
# append, and ``verify_boundary_order`` advances it over replayed events; so a
# record the ledger would refuse is exactly a record replay flags.


class Phase(StrEnum):
    """Where one boundary version's lifecycle stands (``TRANSITIONS``)."""

    NONE = "none"
    """Nothing recorded yet."""
    NO_PACKAGE = "no_package"
    """Sealed as ``construction_failed``: a worker may start, the legacy verifier decides."""
    FROZEN = "frozen"
    """A sealed package, not yet admitted."""
    REJECTED = "rejected"
    """Admission ran and did not admit the package: no worker may start on it."""
    ADMITTED = "admitted"
    STARTED = "started"
    """A worker started on the version (with a package, or after ``construction_failed``)."""
    VERIFIED = "verified"
    """The candidate was verified against the frozen package."""
    DECIDED = "decided"
    """The acceptance decision was recorded."""
    RESUMING = "resuming"
    """A resumed run recorded its own bindings; its verification and decision follow."""
    SUPERSEDED = "superseded"
    """A later version of the run replaced this one; nothing more is recorded on it."""
    ABANDONED = "abandoned"
    """A replacement version not admitted; the worker starts on the earlier version it names."""


@dataclass(frozen=True, slots=True)
class VersionState:
    """What one boundary version's journal has established so far."""

    phase: Phase = Phase.NONE
    package_id: str | None = None
    seed_digest: str | None = None
    manifest: FrozenManifest | None = None
    """What the frozen event says the package links (checked when the seal is recorded)."""
    gate_time: datetime | None = None
    """When the latest seal or admission record was written (an actor starts after it)."""
    excluded: Mapping[str, str] = field(default_factory=dict)
    """Checks the admission excluded (tier ``C``), with the recorded reason."""
    interpreter_sha256: str | None = None
    interpreter_realpath_sha256: str | None = None
    final_bindings: bool = False
    runnable: bool = False
    """The final bindings put at least one check on a runnable tier (``A``, ``A_prime``, ``S``)."""
    admitted_tiers: Mapping[str, CheckTier] = field(default_factory=dict)
    """The tier admission gave each check (``C`` for an excluded one)."""
    bound: Mapping[str, CheckBindingRecord] = field(default_factory=dict)
    """The final bindings, by check id: the tier and binding each check ran through."""
    verifications: tuple[VerificationRecord, ...] = ()
    """The candidate verifications recorded (the run, then at most one re-run)."""
    base_failing: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Per admitted oracle check, the held-out cases its base run failed at admission."""
    admitted_cases: Mapping[str, tuple[tuple[str, bool], ...]] = field(default_factory=dict)
    """Per oracle check, its cases (id, held out) as admission ran them: every later run's."""
    decision: ReconciledRecord | ResumedRecord | None = None
    """The decision the reducer admitted (``DECIDED``): the one evaluation reads."""

    @property
    def seal(self) -> str | None:
        """``PACKAGE_FROZEN``, ``CONSTRUCTION_FAILED``, or ``None`` (not sealed)."""
        if self.phase is Phase.NONE:
            return None
        return CONSTRUCTION_FAILED if self.manifest is None else PACKAGE_FROZEN

    @property
    def frozen(self) -> bool:
        return self.seal == PACKAGE_FROZEN

    @property
    def started(self) -> bool:
        return self.phase in (Phase.STARTED, Phase.VERIFIED, Phase.DECIDED, Phase.RESUMING)

    @property
    def superseded(self) -> bool:
        return self.phase is Phase.SUPERSEDED

    @property
    def identity(self) -> FrozenIdentity | None:
        """What the seal fixed for every later record (``None`` before the seal)."""
        if self.phase is Phase.NONE:
            return None
        manifest = self.manifest
        return FrozenIdentity(
            package_id=self.package_id,
            seed_digest=self.seed_digest,
            criterion_keys=None if manifest is None else frozenset(manifest.criterion_keys),
            check_ids=frozenset(() if manifest is None else (c.check_id for c in manifest.checks)),
        )


Transition = Callable[[VersionState, BaseEvent, Any], VersionState]
"""A transition reads the record the journal gateway validated (``events.validate_record``)."""


def _seal_frozen(state: VersionState, event: BaseEvent, record: FrozenRecord) -> VersionState:
    return replace(
        state,
        phase=Phase.FROZEN,
        package_id=record.package_id,
        seed_digest=record.seed_digest,
        manifest=frozen_manifest(event.data),
        gate_time=_utc(event.timestamp),
    )


def _seal_failed(
    state: VersionState, event: BaseEvent, record: ConstructionFailedRecord
) -> VersionState:
    return replace(
        state,
        phase=Phase.NO_PACKAGE,
        seed_digest=record.seed_digest,
        gate_time=_utc(event.timestamp),
    )


def _reference_checked(
    state: VersionState, _event: BaseEvent, record: ReferenceCheckedRecord
) -> VersionState:
    manifest = state.manifest
    assert manifest is not None
    if not {item.check_id for item in record.excluded_cases} <= manifest.oracle_checks:
        raise BoundaryOrderError("a reference check excludes cases of a check that is no oracle")
    linked = {link.criterion_key for check in manifest.checks for link in check.assertions}
    if any(item.criterion_key in linked for item in record.uncovered):
        # A criterion whose oracles were all dropped has no check in the frozen package.
        raise BoundaryOrderError("a reference check uncovers a criterion the frozen package covers")
    return state


def _admission(state: VersionState, event: BaseEvent, record: AdmissionRecord) -> VersionState:
    time = _utc(event.timestamp)
    _require_observed_results(
        state,
        record,
        tree=(record.base_tree_digest, record.base_tree_digest_after),
        bindings=None,
        on_base=True,
    )
    if record.verdict != "admitted":
        # Recorded, but not an admission: the version can only be superseded.
        return replace(state, phase=Phase.REJECTED, gate_time=time)
    assert state.manifest is not None  # set with every frozen seal
    if not _is_digest(record.interpreter_sha256) or not _is_digest(
        record.interpreter_realpath_sha256
    ):
        raise BoundaryOrderError("an admitted package's record has no interpreter pin")
    admitted_exclusions(state.manifest, event.data)
    excluded = dict(record.excluded_checks or {})
    base_failing = base_failing_held_out(record.checks, excluded)
    admitted_cases = {
        check.check_id: tuple((case.case_id, case.held_out) for case in check.oracle_result.cases)
        for check in record.checks
        if check.oracle_result is not None
    }
    return replace(
        state,
        phase=Phase.ADMITTED,
        excluded=excluded,
        base_failing=base_failing,
        admitted_cases=admitted_cases,
        interpreter_sha256=record.interpreter_sha256,
        interpreter_realpath_sha256=record.interpreter_realpath_sha256,
        admitted_tiers=dict(record.check_tiers or {}),
        gate_time=time,
    )


def _actor_started(
    state: VersionState, event: BaseEvent, record: ActorStartedRecord
) -> VersionState:
    run = parse_boundary_version(event.aggregate_id)
    if run is not None and record.actor_id != run[0]:
        raise BoundaryOrderError("a boundary version of a run binds only that run's worker")
    if state.gate_time is not None and _utc(event.timestamp) <= state.gate_time:
        raise BoundaryOrderError("actor start timestamp does not follow the boundary seal")
    return replace(state, phase=Phase.STARTED)


def _superseded(state: VersionState, event: BaseEvent, record: SupersededRecord) -> VersionState:
    _require_successor(event.aggregate_id, record.superseded_by)
    return replace(state, phase=Phase.SUPERSEDED)


def _abandoned(state: VersionState, event: BaseEvent, record: AbandonedRecord) -> VersionState:
    old = parse_boundary_version(record.bound)
    new = parse_boundary_version(event.aggregate_id)
    if old is None or new is None or old[0] != new[0] or old[1] >= new[1]:
        raise BoundaryOrderError(
            "an abandoned replacement names an earlier version of its run as the bound one"
        )
    return replace(state, phase=Phase.ABANDONED)


def _bindings(state: VersionState, _event: BaseEvent, record: BindingsRecord) -> VersionState:
    if not state.frozen:
        raise BoundaryOrderError("bindings must cite the boundary's frozen package")
    for item in record.checks:
        _require_assignable(state, item)
    _require_attempt(state, record)
    if record.phase != "resumed" and state.phase is not Phase.STARTED:
        raise BoundaryOrderError("the run's bindings are recorded before its verification")
    if record.phase == "repair":
        return state
    if record.phase == "final" and state.final_bindings:
        raise BoundaryOrderError("final bindings already recorded")
    assert state.manifest is not None
    if {item.check_id for item in record.checks} != {c.check_id for c in state.manifest.checks}:
        raise BoundaryOrderError("final bindings bind every check of the frozen package once")
    runnable = any(check.tier in _RUNNABLE_TIERS for check in record.checks)
    bound = {item.check_id: item for item in record.checks}
    # A resumed run's bindings open its own verification and decision: the
    # resumed decision is judged by what that verification recorded.
    phase = Phase.RESUMING if record.phase == "resumed" else state.phase
    return replace(
        state, phase=phase, final_bindings=True, runnable=runnable, bound=bound, verifications=()
    )


def _candidate_verified(
    state: VersionState, _event: BaseEvent, record: VerificationRecord
) -> VersionState:
    if not state.frozen:
        raise BoundaryOrderError("candidate verification requires a frozen, admitted package")
    if not state.final_bindings:
        raise BoundaryOrderError("a candidate verification follows the final bindings it ran")
    _require_bound_run(state, record)
    phase = Phase.RESUMING if state.phase is Phase.RESUMING else Phase.VERIFIED
    return replace(state, phase=phase, verifications=(*state.verifications, record))


def _reconciled(state: VersionState, _event: BaseEvent, record: ReconciledRecord) -> VersionState:
    if state.frozen and state.phase is not Phase.VERIFIED:
        _require_unverified_decision(state, record)
    if state.frozen:
        _require_supported_statuses(state, record)
    return replace(state, phase=Phase.DECIDED, decision=record)


def _resumed(state: VersionState, _event: BaseEvent, record: ResumedRecord) -> VersionState:
    """A resumed decision is judged like a fresh one, by the resume's own recorded run.

    Without resumed bindings (the resume had no live package) nothing was
    verified: only undecided statuses, and uncovered where no admitted check
    covers the criterion, are supported.
    """
    if not state.frozen:
        raise BoundaryOrderError("a resumed decision must cite the boundary's frozen package")
    judged = state if state.phase is Phase.RESUMING else replace(state, bound={}, verifications=())
    _require_supported_statuses(judged, record)
    return replace(state, phase=Phase.DECIDED, decision=record)


# --------------------------------------------------------------------------
# What the product could write in a given state. The gateway has checked each
# record's schema and identities; these rules relate it to what the journal
# already holds: the tier every check can be bound through (admission and the
# oracle rule, ``binding_flow.assign_tiers``), the checks a verification may
# run (the runnable final bindings; a re-run only of checks that ended
# indeterminate), and the package statuses the recorded results support
# (``acceptance.criterion_verdicts``).


def _require_assignable(state: VersionState, item: CheckBindingRecord) -> None:
    """``item`` is a tier assignment ``assign_tiers`` can make for its check."""
    manifest = state.manifest
    assert manifest is not None
    check = next(c for c in manifest.checks if c.check_id == item.check_id)
    keys = {link.criterion_key for link in check.assertions}
    bindings = [item.binding, item.declared.binding if item.declared is not None else None]
    if item.criterion_key not in keys or any(
        binding is not None and binding.criterion_key != item.criterion_key for binding in bindings
    ):
        raise BoundaryOrderError("a binding names a criterion its check does not link")
    admitted = state.admitted_tiers.get(item.check_id)
    if admitted is CheckTier.C:
        allowed, hints = {CheckTier.C}, {EXCLUDED_STATUS_HINT}
    elif check.check_id not in manifest.oracle_checks:
        allowed, hints = {CheckTier.S}, {"run"}
    elif admitted is CheckTier.A:
        allowed, hints = {CheckTier.A}, {"run"}
    else:
        allowed, hints = {CheckTier.A_PRIME, CheckTier.U}, {"run", "unverified", "indeterminate"}
    # The binding each tier runs through: the default one (A), a declared one
    # (A'), none (S, C); a declared binding that did not validate stays on U.
    sources = {
        CheckTier.A: {BindingSource.DEFAULT},
        CheckTier.A_PRIME: {BindingSource.DECLARED},
        CheckTier.U: {None, BindingSource.DECLARED},
    }.get(item.tier, {None})
    # A worker-declared binding is recorded with its validation: a valid one
    # (with a binding) is A', any other stays on U as undecided; a U check
    # without one is unverified.
    bound = item.tier in (CheckTier.A, CheckTier.A_PRIME)
    declared = item.declared
    usable = declared is not None and declared.valid and declared.binding is not None
    # The reason ``assign_tier`` gives each assignment.
    reason = {
        CheckTier.A: "default_binding_resolves",
        CheckTier.S: "script_check",
        CheckTier.C: state.excluded.get(item.check_id),
    }.get(item.tier, declared.reason if declared is not None else "no_binding")
    if (
        item.reason != reason
        or item.tier not in allowed
        or item.status_hint not in hints
        or (item.status_hint == "run") != (item.tier in _RUNNABLE_TIERS)
        or item.binding_source not in sources
        or (bound and item.binding is None)
        or (item.binding_source is None and item.binding is not None)
        or (declared is not None) != (item.binding_source is BindingSource.DECLARED)
        or (declared is not None and declared.binding != item.binding)
        or usable != (item.tier is CheckTier.A_PRIME)
        or (
            item.tier is CheckTier.U
            and (item.status_hint == "indeterminate") != (declared is not None)
        )
    ):
        raise BoundaryOrderError("a binding gives a check a tier the product cannot assign")


def _require_attempt(state: VersionState, record: BindingsRecord) -> None:
    """A repair record names its attempt and binds that criterion's admitted checks; no other does.

    The per-attempt gate (``authority``) records the bindings of one root
    criterion's admitted (not excluded) checks with its index and retry
    attempt; the final and resumed records carry neither.
    """
    manifest = state.manifest
    assert manifest is not None
    attempt = (record.root_ac_index, record.retry_attempt, record.status)
    if record.phase != "repair":
        if attempt != (None, None, None):
            raise BoundaryOrderError("only a repair record names an attempt")
        return
    index = record.root_ac_index
    if index is None or not 0 <= index < len(manifest.criterion_keys):
        raise BoundaryOrderError("a repair record names no root criterion")
    if record.retry_attempt is None or record.retry_attempt < 0:
        raise BoundaryOrderError("a repair record names no retry attempt")
    key = manifest.criterion_keys[index]
    admitted = {
        check.check_id
        for check in manifest.checks
        if check.check_id not in state.excluded
        and any(link.criterion_key == key for link in check.assertions)
    }
    if {item.check_id for item in record.checks} != admitted:
        raise BoundaryOrderError("a repair record binds exactly its criterion's admitted checks")


def _require_bound_run(state: VersionState, record: VerificationRecord) -> None:
    """A verification runs exactly the checks it selected, at their bound tiers and bindings.

    ``binding_flow.verify_with_bindings``: the run selects every runnable
    bound check (the re-run, the ones it re-runs) and records that selection
    as ``check_tiers`` with each check's bound tier, and ``bindings`` with the
    binding of each selected check that has one; it runs every selected check
    unless a precondition stopped it (then none). A script check runs
    through no binding; an oracle check through its bound one.
    """
    runnable = {key for key, item in state.bound.items() if item.tier in _RUNNABLE_TIERS}
    assert state.manifest is not None
    roles = {check.check_id: check.role for check in state.manifest.checks}
    ran = [check.check_id for check in record.checks]
    tiers = {**(record.check_tiers or {})}
    tiers.update({c.check_id: c.tier for c in record.checks if c.tier is not None})
    if (
        not set(ran) | set(tiers) | set(record.bindings or {}) <= runnable
        or any(state.bound[key].tier is not tier for key, tier in tiers.items())
        or any(check.role is not roles[check.check_id] for check in record.checks)
        or any(state.bound[key].binding != value for key, value in (record.bindings or {}).items())
        or any(
            check.binding is not None and check.binding != state.bound[check.check_id].binding
            for check in record.checks
        )
    ):
        raise BoundaryOrderError("a verification runs a check other than the bound runnable ones")
    selected = set(record.check_tiers or {})
    handed = {
        key: state.bound[key].binding
        for key in sorted(selected)
        if state.bound[key].binding is not None
    }
    if (
        (not state.verifications and selected != runnable)
        or not selected
        or (ran and set(ran) != selected)
        or dict(record.bindings or {}) != handed
        or any(
            check.binding is not None and check.check_id not in state.manifest.oracle_checks
            for check in record.checks
        )
    ):
        raise BoundaryOrderError("a verification's selection is not the one its bindings give")
    _require_observed_results(
        state,
        record,
        tree=(record.artifact_tree_digest, record.artifact_tree_digest_after),
        bindings=record.bindings,
        on_base=False,
    )
    if not state.verifications:
        return
    first = state.verifications[0]
    rerunnable = {c.check_id for c in first.checks if c.status is CheckStatus.INDETERMINATE}
    if len(state.verifications) > 1 or not selected <= rerunnable:
        raise BoundaryOrderError("a verification is re-run once, only for indeterminate checks")


# The reasons ``admission._classify`` derives from what a check run observed;
# any other reason is a run that observed nothing (a sandbox or interpreter
# that was unavailable, a launch failure, an unreadable candidate).
_OBSERVED_REASONS = frozenset(
    {
        "passed",
        "preservation_passed",
        "preservation_failed",
        "reproduction_passed_on_base",
        "reproduction_still_failing",
        "reached_failing_assertion",
        "failure_signature_absent",
        HELD_OUT_NOT_DISCRIMINATING,
        "timeout",
        "protected_bytes_mutated",
        "output_oversized",
    }
)


def _classified(
    check: JournalCheckExecution, *, oracle: bool, on_base: bool
) -> tuple[CheckStatus, str] | None:
    """The status and reason ``admission._classify`` gives what ``check`` recorded.

    ``None`` for a run that observed nothing: no exit code, no time-out, no
    oracle result, no changed byte. Its recorded reason (for example an
    unavailable sandbox or a launch failure) is then the fact that decided
    it, and such a run is only ever indeterminate. An oversized output is
    recorded by its reason alone (the killed process has no telling exit
    code): a failure on a candidate, undecided on the base.
    """
    if check.mutated_paths:
        return CheckStatus.INDETERMINATE, "protected_bytes_mutated"
    if check.reason == "output_oversized" and not oracle:
        return (CheckStatus.INDETERMINATE if on_base else CheckStatus.VIOLATED), "output_oversized"
    if check.timed_out:
        return CheckStatus.INDETERMINATE, "timeout"
    code = check.return_code
    if oracle and check.oracle_result is None:
        return None
    if not oracle and code is None:
        return None
    if oracle and code not in (0, 1):
        # The oracle never observed the target (``oracle_undecided``).
        return CheckStatus.INDETERMINATE, "failure_signature_absent"
    passed, signature = code == 0, check.signature_seen
    reproduction = check.role is CheckRole.REPRODUCTION
    if not on_base:
        if passed:
            return CheckStatus.EXPECTED, "passed"
        if not reproduction:
            return CheckStatus.VIOLATED, "preservation_failed"
        if signature:
            return CheckStatus.VIOLATED, "reproduction_still_failing"
        return CheckStatus.INDETERMINATE, "failure_signature_absent"
    if not reproduction:
        if passed:
            return CheckStatus.EXPECTED, "preservation_passed"
        return CheckStatus.VIOLATED, "preservation_failed"
    if passed:
        return CheckStatus.VIOLATED, "reproduction_passed_on_base"
    if signature and held_out_all_passed(check.oracle_result):
        return CheckStatus.VIOLATED, HELD_OUT_NOT_DISCRIMINATING
    if signature:
        return CheckStatus.EXPECTED, "reached_failing_assertion"
    return CheckStatus.INDETERMINATE, "failure_signature_absent"


def _require_observed_results(
    state: VersionState,
    record: AdmissionRecord | VerificationRecord,
    *,
    tree: tuple[str, str],
    bindings: Mapping[str, Any] | None,
    on_base: bool,
) -> None:
    """Every check's status is the one the product computes from what the run recorded.

    Mutation evidence first, the same for admission and verification
    (``admission._mutation_reasons``): a check changed a protected byte
    exactly when its protected digest changed and it lists the changed paths,
    and the receipt flags a mutation exactly when the checked-out tree
    changed under the run (``tree``: its digest before and after) or a check
    changed a protected byte.

    Admission (``on_base``) and verification receipts alike, script and
    oracle checks alike: the recorded status and reason must equal
    ``_classified`` of the recorded observations (``admission._classify``),
    and a run that observed nothing is indeterminate for a reason no
    observation gives. An oracle check's result is one ``oracle_run`` and the
    harness (``harness.compare``) can write: only an oracle check carries
    one; its cases are the frozen oracle's (``c1..c<n>`` with the frozen
    held-out count, and on a candidate exactly the cases admission ran,
    held-out flags included, since a journaled run includes held-out cases);
    no case passes unless the target resolved (``resolve`` ``ok``); the exit
    code is 0 or 1 exactly when the harness decided (the target resolved or
    was missing, or failed to import on a candidate), 0 exactly when every
    case passed, and an undecided result passed none; the failure signature
    is exit code 1; the source says whether the run was handed a binding
    (``declared``, a verification's ``bindings``) or used the default
    (admission).
    """
    manifest = state.manifest
    assert manifest is not None
    checks = record.checks
    if any(
        bool(check.mutated_paths) != (check.protected_digest_before != check.protected_digest_after)
        for check in checks
    ) or record.protected_bytes_mutated != (
        tree[0] != tree[1] or any(check.mutated_paths for check in checks)
    ):
        raise BoundaryOrderError("a receipt's mutation evidence contradicts itself")
    for check in checks:
        oracle = check.check_id in manifest.oracle_checks
        result = check.oracle_result
        if not oracle and result is not None:
            raise BoundaryOrderError("a script check reports an oracle result")
        derived = _classified(check, oracle=oracle, on_base=on_base)
        if derived is None:
            observed = (
                check.status is CheckStatus.INDETERMINATE
                and check.reason not in _OBSERVED_REASONS
                and check.return_code is None
                and not check.signature_seen
            )
        else:
            observed = (check.status, check.reason) == derived
        if not observed:
            raise BoundaryOrderError("a check's status is not what its recorded run shows")
        if result is None:
            continue
        count, held = manifest.oracle_cases[check.check_id]
        cases = tuple((case.case_id, case.held_out) for case in result.cases)
        reference = None if on_base else state.admitted_cases.get(check.check_id)
        passed = [case.passed for case in result.cases]
        source = "declared" if check.check_id in (bindings or {}) else "default"
        decided = result.resolve in ("ok", "missing") or (
            result.resolve == "import_error" and not on_base
        )
        code = check.return_code
        if (
            [case_id for case_id, _held in cases] != [case_id_for(n) for n in range(1, count + 1)]
            or sum(held_out for _id, held_out in cases) != held
            or (reference is not None and cases != reference)
            or (result.resolve != "ok" and any(passed))
            or result.binding_source != source
            or (code in (0, 1)) != decided
            or (code == 0) != (decided and all(passed))
            or (not decided and any(passed))
            or check.signature_seen != (code == 1)
        ):
            raise BoundaryOrderError("an oracle result is not one the oracle harness can write")
    _require_derived_receipt(state, record, tree=tree, on_base=on_base)


def _base_tier(manifest: FrozenManifest, check: JournalCheckExecution) -> CheckTier:
    """The tier admission gives a check from its base run (``admission.base_run_tiers``).

    A script check is ``S``; an oracle check ``A`` when its target resolved on
    the base, or was missing there while its criterion names it
    (``OracleSpec.base_run_tier``), else ``U``.
    """
    if check.check_id not in manifest.oracle_checks:
        return CheckTier.S
    resolve = check.oracle_result.resolve if check.oracle_result is not None else None
    named = check.check_id in manifest.named_targets
    return CheckTier.A if resolve == "ok" or (resolve == "missing" and named) else CheckTier.U


def _require_derived_receipt(
    state: VersionState,
    record: AdmissionRecord | VerificationRecord,
    *,
    tree: tuple[str, str],
    on_base: bool,
) -> None:
    """The receipt's verdict, reasons and tiers are the ones the product derives from its checks.

    ``admission.admit_check_package`` and ``verify_candidate`` alike: the
    reasons are the run's preconditions (a run has some exactly when it ran
    no check), then its mutation reasons (a changed tree, each check that
    changed a protected byte), then each violated check and each other
    undecided check as ``<reason>:<check id>``; the verdict is undecided on a
    precondition or a mutation, else rejected (fail) on a violated check,
    else undecided on an undecided one, else admitted (pass). On the base,
    ``per_check.per_check_admission`` then admits a rejected or undecided
    package (no precondition, no mutation) whose every unmet check has an
    exclusion reason (``per_check.exclusion_reason``), excluding those checks
    (tier ``C``, ``excluded_checks``), or, when that would exclude every
    check, keeps its verdict and adds ``all_checks_excluded``. Each
    check's tier is its base tier (``_base_tier``) and ``check_tiers`` maps
    every check to it, ``C`` for an excluded one.
    """
    manifest = state.manifest
    assert manifest is not None
    checks = record.checks
    mutation = ["source_checkout_mutated"] if tree[0] != tree[1] else []
    mutation += [f"protected_bytes_mutated:{c.check_id}" for c in checks if c.mutated_paths]
    violated = [c for c in checks if c.status is CheckStatus.VIOLATED]
    undecided = [c for c in checks if c.status is CheckStatus.INDETERMINATE]
    tail = [
        *mutation,
        *(f"{c.reason}:{c.check_id}" for c in violated),
        *(f"{c.reason}:{c.check_id}" for c in undecided if c.reason != "protected_bytes_mutated"),
    ]
    reasons = list(record.reasons)
    stated_all = on_base and bool(checks) and reasons[-1:] == [ALL_CHECKS_EXCLUDED]
    body = reasons[:-1] if stated_all else reasons
    split = len(body) - len(tail)
    if split < 0 or body[split:] != tail or bool(body[:split]) != (not checks):
        raise BoundaryOrderError("a receipt's reasons are not the ones its checks give")
    good, bad = ("admitted", "rejected") if on_base else ("pass", "fail")
    if body[:split] or mutation:
        verdict = "indeterminate"
    elif violated:
        verdict = bad
    elif undecided:
        verdict = "indeterminate"
    else:
        verdict = good
    excluded: dict[str, str] = {}
    derived_all = False
    if on_base and verdict != good and not (body[:split] or mutation):
        unmet = [c for c in checks if c.status is not CheckStatus.EXPECTED]
        mapped = {c.check_id: exclusion_reason(c.status, c.reason) for c in unmet}
        if None not in mapped.values():
            if len(unmet) == len(checks):
                derived_all = True
            else:
                verdict = good
                excluded = {key: value for key, value in mapped.items() if value is not None}
    if stated_all != derived_all:
        raise BoundaryOrderError("a receipt's reasons are not the ones its checks give")
    if record.verdict != verdict:
        raise BoundaryOrderError("a receipt's verdict is not the one its checks give")
    if not on_base:
        return
    assert isinstance(record, AdmissionRecord)
    base = {check.check_id: _base_tier(manifest, check) for check in checks}
    tiers = {key: CheckTier.C if key in excluded else tier for key, tier in base.items()}
    if (
        any(check.tier is not base[check.check_id] for check in checks)
        or dict(record.check_tiers or {}) != tiers
        or dict(record.excluded_checks or {}) != excluded
    ):
        raise BoundaryOrderError("a receipt's tiers are not the ones its checks give")


def _effective(state: VersionState) -> tuple[dict[str, JournalCheckExecution], bool]:
    """The deciding results (the re-run over the run) and whether they are trusted.

    The product's own rule (``binding_flow.BoundVerification.effective``,
    ``acceptance.criterion_verdicts`` and the candidate identity check of
    ``run_wiring``): the results decide only when every run saw the same
    candidate tree, unchanged under it (one tree digest before and after
    each run); the deciding run's receipt flags no mutation; no deciding
    check changed a protected byte; and at least one check ran. A check the
    re-run replaced is judged by the re-run only (a transient
    protected-byte mutation is why a check is re-run).
    """
    runs = state.verifications
    if not runs:
        return {}, False
    results = {check.check_id: check for check in runs[0].checks}
    for rerun in runs[1:]:
        results.update({c.check_id: c for c in rerun.checks if c.check_id in results})
    trees = {
        digest
        for run in runs
        for digest in (run.artifact_tree_digest, run.artifact_tree_digest_after)
    }
    flagged = (
        runs[0].protected_bytes_mutated if len(runs) == 1 else runs[-1].protected_bytes_mutated
    )
    mutated = (
        flagged
        or len(trees) != 1
        or any(
            check.mutated_paths or check.protected_digest_before != check.protected_digest_after
            for check in results.values()
        )
    )
    return results, not mutated and bool(results)


def _supported(state: VersionState, key: str, lost: Mapping[str, str]) -> tuple[str, bool]:
    """The package status the recorded results give ``key``, and whether a pass is declared."""
    manifest = state.manifest
    assert manifest is not None
    linked = [
        check
        for check in manifest.checks
        if check.check_id not in state.excluded
        and any(link.criterion_key == key for link in check.assertions)
    ]
    if key in lost or not linked:
        return "uncovered", False
    results, trusted = _effective(state)
    failed = undecided = unverified = False
    passed = held_out = by_default = 0
    for check in linked:
        item = state.bound.get(check.check_id)
        if item is not None and item.tier is CheckTier.U:
            undecided |= item.status_hint == "indeterminate"
            unverified |= item.status_hint != "indeterminate"
            continue
        execution = results.get(check.check_id)
        if not trusted or execution is None:
            undecided = True
        elif execution.status is CheckStatus.VIOLATED:
            failed = True
        elif execution.status is not CheckStatus.EXPECTED:
            undecided = True
        elif check.check_id in manifest.oracle_checks:
            passed += 1
            oracle = execution.oracle_result
            # Only a held-out case the base failed at admission verifies a pass.
            failing = state.base_failing.get(check.check_id, frozenset())
            if check.role is CheckRole.REPRODUCTION and oracle is not None:
                if any(c.held_out and c.passed and c.case_id in failing for c in oracle.cases):
                    held_out += 1
                    by_default += int(item is not None and item.tier is CheckTier.A)
    if failed:
        return "fail", False
    if undecided:
        return "indeterminate", False
    if unverified or not passed or not held_out:
        return "unverified", False
    return "pass", not by_default


_SUPPORTED_STATUSES: Mapping[str, frozenset[str]] = {
    "fail": frozenset({"fail", "indeterminate"}),
    "indeterminate": frozenset({"indeterminate"}),
    "pass": frozenset({"pass", "indeterminate"}),
    "unverified": frozenset({"unverified", "uncovered", "indeterminate"}),
    "uncovered": frozenset({"unverified", "uncovered", "indeterminate"}),
}
"""Package statuses a decision may record, by what the results support.

The package may always be more cautious (indeterminate, for example when the
candidate changed under verification), never more favourable: no pass the
results do not show, and no failure or undecided criterion recorded as one
the package did not verify (which would hand it to the legacy verifier)."""


def _require_supported_statuses(
    state: VersionState, record: ReconciledRecord | ResumedRecord
) -> None:
    assert state.manifest is not None
    keys = state.manifest.criterion_keys
    lost = criteria_without_admitted_check(state.manifest, state.excluded)
    for item in record.criteria:
        # The frozen manifest lists the criteria in Seed order, the order the
        # decision's root indexes follow.
        if keys[item.root_ac_index] != item.criterion_key:
            raise BoundaryOrderError("a decision's root index names another criterion")
        supported, declared = _supported(state, item.criterion_key, lost)
        if item.package_status not in _SUPPORTED_STATUSES[supported] or (
            item.package_status == "pass" and item.declared_binding_pass != declared
        ):
            raise BoundaryOrderError(
                "a decision records a package status the candidate verification does not show"
            )


TRANSITIONS: Mapping[tuple[Phase, str], Transition] = {
    (Phase.NONE, PACKAGE_FROZEN): _seal_frozen,
    (Phase.NONE, CONSTRUCTION_FAILED): _seal_failed,
    (Phase.FROZEN, REFERENCE_CHECKED): _reference_checked,
    (Phase.FROZEN, ADMISSION_COMPLETED): _admission,
    (Phase.FROZEN, SUPERSEDED): _superseded,
    (Phase.REJECTED, SUPERSEDED): _superseded,
    (Phase.ADMITTED, SUPERSEDED): _superseded,
    (Phase.NO_PACKAGE, SUPERSEDED): _superseded,
    (Phase.REJECTED, REPLACEMENT_ABANDONED): _abandoned,
    (Phase.NO_PACKAGE, REPLACEMENT_ABANDONED): _abandoned,
    (Phase.ADMITTED, ACTOR_STARTED): _actor_started,
    (Phase.NO_PACKAGE, ACTOR_STARTED): _actor_started,
    (Phase.STARTED, BINDING_RECORDED): _bindings,
    (Phase.VERIFIED, BINDING_RECORDED): _bindings,
    (Phase.DECIDED, BINDING_RECORDED): _bindings,
    (Phase.RESUMING, BINDING_RECORDED): _bindings,
    (Phase.RESUMING, CANDIDATE_VERIFIED): _candidate_verified,
    (Phase.RESUMING, ACCEPTANCE_RESUMED): _resumed,
    (Phase.STARTED, CANDIDATE_VERIFIED): _candidate_verified,
    (Phase.VERIFIED, CANDIDATE_VERIFIED): _candidate_verified,
    (Phase.STARTED, ACCEPTANCE_RECONCILED): _reconciled,
    (Phase.VERIFIED, ACCEPTANCE_RECONCILED): _reconciled,
    (Phase.STARTED, ACCEPTANCE_RESUMED): _resumed,
    (Phase.VERIFIED, ACCEPTANCE_RESUMED): _resumed,
    (Phase.DECIDED, ACCEPTANCE_RESUMED): _resumed,
}
"""Every allowed (phase, record) pair of a boundary version and what it does.

A pair not in the table is refused: the version's journal cannot hold that
record in that phase. The transition then checks what the record says
(citations, Seed, verdict, the decision's statuses) and may still refuse.
"""
VERSION_RECORDS: tuple[str, ...] = (
    PACKAGE_FROZEN,
    CONSTRUCTION_FAILED,
    REFERENCE_CHECKED,
    ADMISSION_COMPLETED,
    ACTOR_STARTED,
    SUPERSEDED,
    REPLACEMENT_ABANDONED,
    BINDING_RECORDED,
    CANDIDATE_VERIFIED,
    ACCEPTANCE_RECONCILED,
    ACCEPTANCE_RESUMED,
)
"""Every record a boundary version's journal can hold."""


def _refusal(state: VersionState, kind: str) -> str:
    """Why ``kind`` is refused in ``state.phase`` (the table has no such pair)."""
    phase = state.phase
    if kind not in VERSION_RECORDS:
        return f"{kind} is not a boundary version record"
    if phase is Phase.SUPERSEDED:
        if kind == SUPERSEDED:
            return "boundary version already superseded"
        return f"{kind} recorded on a superseded boundary version"
    if phase is Phase.ABANDONED:
        return f"{kind} recorded on an abandoned boundary version"
    if kind in (PACKAGE_FROZEN, CONSTRUCTION_FAILED):
        return "boundary already sealed; a package cannot be regenerated or replaced"
    if phase is Phase.NONE:
        return {
            ACTOR_STARTED: "actor started before the seal (the boundary is not sealed)",
            ADMISSION_COMPLETED: "admission recorded before the seal (no frozen package)",
        }.get(kind, f"{kind} recorded before the seal")
    return {
        ADMISSION_COMPLETED: {
            Phase.NO_PACKAGE: "admission recorded without a frozen package",
            Phase.ADMITTED: "admission already recorded",
            Phase.REJECTED: "admission already recorded",
        }.get(phase, "admission must be recorded before any actor starts"),
        REFERENCE_CHECKED: "the reference check follows the seal and precedes admission",
        ACTOR_STARTED: {
            Phase.FROZEN: "actor started before admission",
            Phase.REJECTED: "actor started on a package admission did not admit",
        }.get(phase, "a worker already started on this boundary version"),
        SUPERSEDED: "a boundary version bound to a worker cannot be superseded",
        REPLACEMENT_ABANDONED: (
            "only a sealed version without an admitted package can be abandoned"
        ),
        BINDING_RECORDED: "bindings are recorded only after admission and the worker start",
        CANDIDATE_VERIFIED: (
            "candidate verification requires a frozen, admitted package and a started worker"
        ),
        ACCEPTANCE_RECONCILED: (
            "acceptance already reconciled"
            if phase is Phase.DECIDED
            else "a resumed run records a resumed decision"
            if phase is Phase.RESUMING
            else "acceptance is decided only after the worker start"
        ),
        ACCEPTANCE_RESUMED: "a resumed decision needs an admitted package and a started worker",
    }[kind]


def advance(state: VersionState, event: BaseEvent) -> VersionState:
    """The state after ``event``; ``BoundaryOrderError`` when the journal forbids it.

    The lifecycle table decides whether the record may follow; the journal
    gateway (``events.validate_record``) then validates its envelope, exact
    schema and the identities the seal fixed, before the transition reads it.
    """
    transition = TRANSITIONS.get((state.phase, event.type))
    if transition is None:
        raise BoundaryOrderError(_refusal(state, event.type))
    try:
        record = validate_record(event, state.identity)
    except JournalRecordError as exc:
        raise BoundaryOrderError(str(exc)) from exc
    return transition(state, event, record)


_RUNNABLE_TIERS = frozenset({CheckTier.A, CheckTier.A_PRIME, CheckTier.S})
_PAGE = 500
# Statuses a decision may carry without a candidate verification: when the
# final bindings left no check runnable, the checks were not run (unverified or
# indeterminate); when the package could not decide at all, every covered
# criterion is indeterminate and the rest are uncovered.
_UNRUN_STATUSES = frozenset({"unverified", "uncovered", "indeterminate"})
_UNDECIDED_STATUSES = frozenset({"uncovered", "indeterminate"})


def _require_unverified_decision(state: VersionState, record: ReconciledRecord) -> None:
    """A decision recorded without a candidate verification claims nothing a run would show.

    Allowed only when the final bindings left no check runnable, or when the
    decision says the package could not decide (``undecided_reason``) after
    the worker started; either way no criterion is a pass or a fail.
    """
    if state.final_bindings and not state.runnable:
        allowed = _UNRUN_STATUSES
    elif record.undecided_reason and state.frozen and state.started:
        allowed = _UNDECIDED_STATUSES
    else:
        raise BoundaryOrderError("acceptance must cite a verification of the frozen package")
    statuses = {item.package_status for item in record.criteria}
    if not statuses <= allowed:
        raise BoundaryOrderError(
            "a decision recorded without a candidate verification claims a verified status"
        )


def _require_successor(boundary_id: str, successor: object) -> None:
    """A version is superseded only by a later version of the same run."""
    old = parse_boundary_version(boundary_id)
    new = parse_boundary_version(successor) if isinstance(successor, str) else None
    if old is None or new is None:
        raise BoundaryOrderError("only a boundary version of a run can supersede another")
    if new[0] != old[0]:
        raise BoundaryOrderError("a boundary version is superseded only within its own run")
    if new[1] <= old[1]:
        raise BoundaryOrderError("a boundary version is superseded only by a later version")


def version_state(events: Iterable[BaseEvent]) -> VersionState:
    """Advance over a boundary version's replayed events; raises on the first violation."""
    state = VersionState()
    for event in events:
        state = advance(state, event)
    return state


class BoundaryLedger:
    """Write-time ordering guard over an initialized ``EventStore``.

    Every write replays the boundary version's journal, advances the reducer
    (``advance``) with the event it is about to append, and appends only when
    that succeeds; a journal that is already inconsistent refuses every write.
    """

    def __init__(self, store: EventStore) -> None:
        self._store = store

    async def events(self, boundary_id: str) -> list[BaseEvent]:
        """Replay one boundary's events in journal order."""
        return await self._store.replay(BOUNDARY_AGGREGATE_TYPE, boundary_id)

    async def _state(self, boundary_id: str) -> VersionState:
        state = version_state(await self.events(boundary_id))
        return state

    async def _append(self, boundary_id: str, event: BaseEvent) -> BaseEvent:
        advance(await self._state(boundary_id), event)
        await self._store.append(event)
        return event

    async def record_check_package_enabled(
        self, execution_id: str, contract: RunContract
    ) -> BaseEvent:
        """Record, once and before anything else, that the check package is on for a run.

        ``contract`` holds the settings that decide anything after the worker
        starts; a resumed run uses them instead of the live config
        (``run_contract``).

        Refused once any boundary version of the run exists: the record must
        come first. A resumed run reads it back
        (``resume.load_resumed_boundary``): with this record present, a
        missing or malformed boundary makes the resumed decision undecided
        instead of letting the legacy verifier decide the criteria the package
        may cover.
        """
        if not execution_id:
            raise BoundaryOrderError("the check package needs the run's execution id")
        if await self.check_package_enabled(execution_id):
            raise BoundaryOrderError(
                "the check package was already enabled for this run",
                details={"execution_id": execution_id},
            )
        if await self.run_versions(execution_id):
            raise BoundaryOrderError(
                "the enabled record must precede every boundary version of the run",
                details={"execution_id": execution_id},
            )
        event = check_package_enabled_event(execution_id, contract)
        _run_record(event, execution_id)
        await self._store.append(event)
        return event

    async def run_versions(self, execution_id: str) -> dict[int, list[BaseEvent]]:
        """Every boundary version of the run in the journal, by version number.

        Found by what the journal holds, not by counting up from ``v1``: a
        version recorded without its predecessors (for example written into
        the store directly) is found too, so a run-level rule cannot be
        passed by a gap.
        """
        found: dict[int, str] = {}
        offset = 0
        while True:
            page = await self._store.query_events(
                aggregate_type=BOUNDARY_AGGREGATE_TYPE, limit=_PAGE, offset=offset
            )
            for event in page:
                run = parse_boundary_version(event.aggregate_id)
                if run is not None and run[0] == execution_id:
                    found[run[1]] = event.aggregate_id
            if len(page) < _PAGE:
                break
            offset += _PAGE
        return {version: await self.events(found[version]) for version in sorted(found)}

    async def check_package_enabled(self, execution_id: str) -> bool:
        """Whether ``record_check_package_enabled`` ran for ``execution_id``."""
        return _first(await self.events(execution_id), CHECK_PACKAGE_ENABLED) is not None

    async def run_contract(self, execution_id: str) -> RunContract | None:
        """The run contract recorded for ``execution_id``; ``None`` when the run was never on.

        Raises ``BoundaryOrderError`` when the enabled record exists but its
        contract is missing or malformed: the settings the run started with
        are then unknown.
        """
        event = _first(await self.events(execution_id), CHECK_PACKAGE_ENABLED)
        if event is None:
            return None
        try:
            return enabled_contract(event.data)
        except ValueError as exc:
            raise BoundaryOrderError(
                "the run's check package contract is malformed",
                details={"execution_id": execution_id},
            ) from exc

    async def _require_run_enabled(self, boundary_id: str) -> None:
        run = parse_boundary_version(boundary_id)
        if run is not None and not await self.check_package_enabled(run[0]):
            raise BoundaryOrderError(
                "a boundary version of a run is sealed only after the run's enabled record",
                details={"boundary_id": boundary_id},
            )

    async def record_package_frozen(
        self,
        boundary_id: str,
        package: CheckPackage,
        *,
        seed: Seed,
    ) -> BaseEvent:
        """Persist the package id, Seed digest and manifest; the boundary's only seal.

        The package id (``package.seal_package``) is what I2 orders before any
        worker start. Every later receipt and event must cite the same one.
        The package must be the one for ``seed`` (``validate_package_for_seed``:
        its Seed digest and its criterion keys in Seed order), checked before
        anything is appended: no package is sealed for a Seed it was not built for.
        """
        validate_package_for_seed(package, seed)
        await self._require_run_enabled(boundary_id)
        return await self._append(boundary_id, package_frozen_event(boundary_id, package))

    async def record_construction_failed(
        self,
        boundary_id: str,
        *,
        seed_digest: str,
        input_digest: str,
        reason: str,
    ) -> BaseEvent:
        """Seal a boundary whose generation produced no valid package."""
        await self._require_run_enabled(boundary_id)
        event = construction_failed_event(
            boundary_id, seed_digest=seed_digest, input_digest=input_digest, reason=reason
        )
        return await self._append(boundary_id, event)

    async def record_admission(self, boundary_id: str, result: AdmissionResult) -> BaseEvent:
        """Persist the single admission receipt (same package id and Seed as the seal)."""
        return await self._append(boundary_id, admission_completed_event(boundary_id, result))

    async def record_actor_started(
        self,
        actor_id: str,
        boundary_ids: Sequence[str],
        *,
        workspace: Path | None = None,
        runtime: str | None = None,
        packages: Sequence[CheckPackage] = (),
    ) -> list[BaseEvent]:
        """Record a worker start on every boundary it is bound to.

        Raises ``BoundaryOrderError`` unless each boundary is sealed, not
        superseded and, when it holds a package, admitted, and every later
        version of its run is recorded abandoned in its favor
        (``record_replacement_abandoned``). With
        ``workspace``, every frozen boundary's live sealed package must be in
        ``packages`` (the one whose id the seal cites), and
        ``BoundaryLeakError`` is raised when the workspace contains a
        generated check file, by path or by the in-memory digest of any
        package file, the oracle data file included (the journal manifest
        cannot find a renamed copy of it). Call this before launching the
        worker; launch only on success.
        """
        if not boundary_ids:
            raise BoundaryOrderError("an actor must be bound to at least one boundary")
        started: list[BaseEvent] = []
        live: list[CheckPackage] = []
        by_id = {package.package_id: package for package in packages}
        # The whole batch is validated against tentative state before the one
        # append, so a boundary named twice is refused, not written twice.
        tentative: dict[str, VersionState] = {}
        for boundary_id in boundary_ids:
            if boundary_id not in tentative:
                tentative[boundary_id] = version_state(await self.events(boundary_id))
            state = tentative[boundary_id]
            event = actor_started_event(
                boundary_id, actor_id=actor_id, package_id=state.package_id, runtime=runtime
            )
            try:
                tentative[boundary_id] = advance(state, event)
            except BoundaryOrderError as exc:
                raise BoundaryOrderError(
                    f"actor cannot start: {exc.message}",
                    details={"boundary_id": boundary_id, "actor_id": actor_id},
                ) from exc
            if state.frozen and workspace is not None:
                package = by_id.get(state.package_id or "")
                if package is None:
                    raise BoundaryOrderError(
                        "actor cannot start: the leak scan needs the boundary's sealed package",
                        details={"boundary_id": boundary_id, "actor_id": actor_id},
                    )
                live.append(package)
            started.append(event)
        for boundary_id in tentative:
            run = parse_boundary_version(boundary_id)
            if run is None:
                continue
            versions = await self.run_versions(run[0])
            later = {number: events for number, events in versions.items() if number > run[1]}
            if _standing_after(boundary_id, later):
                raise BoundaryOrderError(
                    "actor cannot start: a later version of the run is neither superseded nor "
                    "abandoned in this version's favor",
                    details={"boundary_id": boundary_id, "actor_id": actor_id},
                )
        if workspace is not None:
            leaks = find_workspace_leaks(workspace, live)
            if leaks:
                raise BoundaryLeakError(
                    "worker workspace contains generated check files",
                    details={"actor_id": actor_id, "paths": list(leaks)},
                )
        await self._store.append_batch(started)
        return started

    async def record_superseded(
        self,
        boundary_id: str,
        *,
        superseded_by: str,
        reason: str,
    ) -> BaseEvent:
        """Mark ``boundary_id`` as replaced by the sealed version ``superseded_by``.

        Refused unless both ids are versions of the same run and
        ``superseded_by`` is a later version, both versions are sealed, the old version
        is not already superseded, and no actor was ever bound to the old
        version (a worker's verdict must cite the package it was bound to).
        A superseded version accepts no later record, an actor start included.
        """
        try:
            _require_successor(boundary_id, superseded_by)
        except BoundaryOrderError as exc:
            raise BoundaryOrderError(
                exc.message, details={"boundary_id": boundary_id, "superseded_by": superseded_by}
            ) from exc
        old = await self._state(boundary_id)
        new = await self._state(superseded_by)
        if old.seal is None or new.seal is None:
            raise BoundaryOrderError(
                "both boundary versions must be sealed before one supersedes the other",
                details={"boundary_id": boundary_id, "superseded_by": superseded_by},
            )
        event = superseded_event(
            boundary_id,
            superseded_by=superseded_by,
            package_id=old.package_id,
            successor_package_id=new.package_id,
            reason=reason,
        )
        return await self._append(boundary_id, event)

    async def record_replacement_abandoned(self, boundary_id: str, *, bound: str) -> BaseEvent:
        """Close the replacement version ``boundary_id``; the worker stays bound to ``bound``.

        Refused unless ``boundary_id`` was sealed without a package or not
        admitted (the table's rule) and ``bound`` is an earlier version of
        the same run whose package is admitted and on which no worker has
        started yet. An abandoned version accepts no later record.
        """
        if (await self._state(bound)).phase is not Phase.ADMITTED:
            raise BoundaryOrderError(
                "a replacement is abandoned only while its bound version is admitted and unstarted",
                details={"boundary_id": boundary_id, "bound": bound},
            )
        state = await self._state(boundary_id)
        event = replacement_abandoned_event(boundary_id, bound=bound, package_id=state.package_id)
        return await self._append(boundary_id, event)

    async def record_bindings(
        self, boundary_id: str, *, package_id: str, payload: BindingsPayload
    ) -> BaseEvent:
        """Record every check's tier and binding once the worker has stopped.

        ``package_id`` is the sealed package id (``CheckPackage.package_id``).
        Refused unless the frozen, admitted package is cited and the worker
        started on this boundary. A ``final`` record is single and must precede
        the candidate verification it governs; ``repair`` records may repeat.
        """
        event = binding_recorded_event(boundary_id, package_id=package_id, payload=payload)
        return await self._append(boundary_id, event)

    async def record_candidate_verification(
        self, boundary_id: str, verification: CandidateVerification
    ) -> BaseEvent:
        """Persist a candidate run of the frozen, admitted package."""
        return await self._append(boundary_id, candidate_verified_event(boundary_id, verification))

    async def record_acceptance_reconciled(
        self, boundary_id: str, *, package_id: str | None, reconciliation: ReconciliationPayload
    ) -> BaseEvent:
        """Persist the per-criterion acceptance decision once, after verification.

        With a package it must follow a candidate verification of the frozen
        package; without one it may only record a decision that claims no
        verified status: after final bindings that left no check runnable, or
        a decision the package could not make (``undecided_reason``, every
        covered criterion indeterminate). Without a package (``package_id`` is ``None``) the boundary
        must be sealed as ``construction_failed`` and a worker must have
        started on it.
        """
        event = acceptance_reconciled_event(
            boundary_id, package_id=package_id, reconciliation=reconciliation
        )
        return await self._append(boundary_id, event)

    async def record_reference_checked(
        self, boundary_id: str, *, package_id: str, payload: ReferenceCheckPayload
    ) -> BaseEvent:
        """Record what the reference check excluded, after the seal and before admission."""
        event = reference_checked_event(boundary_id, package_id=package_id, payload=payload)
        return await self._append(boundary_id, event)

    async def record_acceptance_resumed(
        self, boundary_id: str, *, package_id: str, payload: ResumedPayload
    ) -> BaseEvent:
        """Record a resumed run's recomputed package decision (one per resume).

        Refused unless the frozen, admitted package is cited and a worker was
        started on this boundary. It cites the frozen package id instead of
        recording a second candidate verification: the frozen boundary's
        single-shot records are not written again.
        """
        event = acceptance_resumed_event(boundary_id, package_id=package_id, payload=payload)
        return await self._append(boundary_id, event)

    async def record_resumed_undecided(
        self, execution_id: str, *, payload: ResumedPayload
    ) -> BaseEvent:
        """Record a resumed decision made without a usable boundary (no package cited).

        Written on the run's own aggregate (``execution_id``), next to the
        record that the check package was on, because no boundary version
        can be cited. Refused unless the run's recovery projection is
        undecidable: with a usable boundary the resume cites its version.
        """
        if not execution_id or parse_boundary_version(execution_id) is not None:
            raise BoundaryOrderError("a resumed decision needs the run's execution id")
        event = acceptance_resumed_event(execution_id, package_id=None, payload=payload)
        _run_record(event, execution_id)
        if not await self.check_package_enabled(execution_id) and not await self.run_versions(
            execution_id
        ):
            # The enabled record, or any boundary version of the run, shows the
            # package was on; a run with neither was off and has nothing to record.
            raise BoundaryOrderError(
                "an undecided resume is recorded only for a run whose check package was on",
                details={"execution_id": execution_id},
            )
        projection = recovery_projection(
            execution_id, await self.events(execution_id), await self.run_versions(execution_id)
        )
        if not isinstance(projection, RecoveryUndecidable):
            raise BoundaryOrderError(
                "an undecided resume is recorded only when the run's boundary is unusable",
                details={"execution_id": execution_id},
            )
        await self._store.append(event)
        return event


def verify_boundary_order(
    events: Sequence[BaseEvent], *, run_events: Sequence[BaseEvent] | None = None
) -> tuple[str, ...]:
    """Return ordering violations in one boundary version's replayed events.

    The same reducer (``advance``) as every write path decides each event; an
    event it refuses is reported and skipped. An empty result means: one
    seal, the seal precedes admission, admission precedes every actor start,
    no record follows a supersession, and every receipt cites the frozen
    package id. With ``run_events`` (the run aggregate's events) the run's
    enabled record must also precede the version's first record.
    """
    violations: list[str] = []
    state = VersionState()
    for event in events:
        try:
            state = advance(state, event)
        except BoundaryOrderError as exc:
            violations.append(exc.message)
    seals = [e for e in events if e.type in {PACKAGE_FROZEN, CONSTRUCTION_FAILED}]
    if len(seals) != 1:
        violations.append(f"expected exactly one seal, found {len(seals)}")
    if state.frozen and not state.superseded:
        admissions = [e for e in events if e.type == ADMISSION_COMPLETED]
        if len(admissions) != 1:
            violations.append(f"expected exactly one admission, found {len(admissions)}")
    if any(key.endswith("package_sha256") for event in events for key in event.data):
        # An unkeyed digest of the full package would let a reader confirm
        # guessed held-out values; events cite the opaque package id only.
        violations.append("a boundary event records an unkeyed package digest")
    if run_events is not None and events:
        enabled = _first(run_events, CHECK_PACKAGE_ENABLED)
        if enabled is None:
            violations.append("the run has no enabled record")
        elif _utc(enabled.timestamp) > _utc(events[0].timestamp):
            violations.append("a boundary version was recorded before the run's enabled record")
    return tuple(violations)


# --------------------------------------------------------------------------
# The recovery projection: what a resumed run may rely on, from the journal.


@dataclass(frozen=True, slots=True)
class RecoveryOff:
    """No record of the run: the check package was off, the legacy verifier decides."""


@dataclass(frozen=True, slots=True)
class RecoveryNoPackage:
    """The worker was bound to a version sealed without a package: the legacy verifier decides."""

    boundary_id: str
    contract: RunContract


@dataclass(frozen=True, slots=True)
class RecoveryBound:
    """The worker was bound to an admitted package; everything here was validated by the reducer."""

    execution_id: str
    boundary_id: str
    package_id: str
    seed_digest: str
    contract: RunContract
    interpreter_sha256: str
    interpreter_realpath_sha256: str
    criterion_keys: tuple[str, ...]
    covered: frozenset[str]
    """Criteria an admitted check covers, under the live coverage rule."""
    held_out_checks: frozenset[str]
    """Admitted checks that had held-out cases."""


@dataclass(frozen=True, slots=True)
class RecoveryUndecidable:
    """The journal says the package was on but is not one the product could write."""

    reason: str
    boundary_id: str = ""


RecoveryProjection = RecoveryOff | RecoveryNoPackage | RecoveryBound | RecoveryUndecidable


def recovery_projection(
    execution_id: str,
    run_events: Sequence[BaseEvent],
    versions: Mapping[int, Sequence[BaseEvent]],
) -> RecoveryProjection:
    """The one recovery authority of a resumed run, computed from the journal alone.

    ``run_events`` are the run aggregate's events and ``versions`` every
    boundary version of the run (``BoundaryLedger.run_versions``). Every
    version is replayed through the same reducer as every write
    (``verify_boundary_order``); the run's records must be exactly what the
    product writes. Anything else (a lifecycle violation, a duplicate, a
    conflict, a gap, a missing required field) is ``RecoveryUndecidable``.
    """
    try:
        return _project(execution_id, run_events, versions)
    except BoundaryOrderError as exc:
        return RecoveryUndecidable(exc.message)


def _run_record(event: BaseEvent, execution_id: str) -> EnabledRecord | ResumedRecord:
    """The journal gateway for the run aggregate (``events.validate_run_record``)."""
    try:
        return validate_run_record(event, execution_id)
    except JournalRecordError as exc:
        raise BoundaryOrderError(str(exc), details={"execution_id": execution_id}) from exc


def _standing_after(
    bound: str, later: Mapping[int, Sequence[BaseEvent]], *, before: datetime | None = None
) -> list[int]:
    """The versions in ``later`` not recorded abandoned in favor of ``bound``.

    With ``before`` (the worker start on ``bound``), an abandonment recorded
    after it stands too: the product abandons a replacement before the worker starts.
    """
    standing = []
    for number, events in later.items():
        closing = _first(events, REPLACEMENT_ABANDONED)
        if (
            closing is None
            or version_state(events).phase is not Phase.ABANDONED
            or closing.data.get("bound") != bound
            or (before is not None and _utc(closing.timestamp) > before)
        ):
            standing.append(number)
    return standing


def _project(
    execution_id: str,
    run_events: Sequence[BaseEvent],
    versions: Mapping[int, Sequence[BaseEvent]],
) -> RecoveryProjection:
    records = [_run_record(event, execution_id) for event in run_events]
    enabled = [record for record in records if isinstance(record, EnabledRecord)]
    if not enabled:
        if versions or run_events:
            raise BoundaryOrderError("boundary records exist without the run's enabled record")
        return RecoveryOff()
    if len(enabled) != 1:
        raise BoundaryOrderError("the run's enabled record is repeated")
    if run_events[0].type != CHECK_PACKAGE_ENABLED:
        raise BoundaryOrderError("a run record precedes the run's enabled record")
    if len(records) > 1:
        # The product records a run-level resume only when recovery was
        # already undecidable, and nothing it records later makes it decidable.
        raise BoundaryOrderError("the run was resumed once without a usable boundary")
    contract = enabled[0].contract
    if list(versions) != list(range(1, len(versions) + 1)):
        raise BoundaryOrderError("the run's boundary versions are not v1, v2, ... in order")
    states: dict[int, VersionState] = {}
    successors: dict[int, tuple[int, object]] = {}
    for number, events in versions.items():
        if any(event.aggregate_id != boundary_version_id(execution_id, number) for event in events):
            raise BoundaryOrderError("a boundary version holds a record of another aggregate")
        problems = verify_boundary_order(events, run_events=run_events)
        if problems:
            raise BoundaryOrderError(problems[0])
        states[number] = version_state(events)
        for event in events:
            if event.type == SUPERSEDED:
                successor = parse_boundary_version(str(event.data.get("superseded_by")))
                if successor is None or successor[1] not in versions:
                    raise BoundaryOrderError("a version is superseded by one the journal lacks")
                successors[number] = (successor[1], event.data.get("successor_package_id"))
    started = [number for number, state in states.items() if state.started]
    if len(started) != 1:
        raise BoundaryOrderError("the run's worker is not bound to exactly one version")
    # The product seals each regeneration as the next version and supersedes
    # the one before; the worker starts on the last version not abandoned
    # (``_standing_after``), and only an admitted version has a replacement to
    # abandon. Any other history (a version recorded after the worker started,
    # an unsuperseded predecessor) is one the product could not write.
    start = _first(versions[started[0]], ACTOR_STARTED)
    assert start is not None  # the bound version started
    after = {number: versions[number] for number in versions if number > started[0]}
    if (
        set(successors) != set(range(1, started[0]))
        or (after and not states[started[0]].frozen)
        or _standing_after(
            boundary_version_id(execution_id, started[0]), after, before=_utc(start.timestamp)
        )
    ):
        raise BoundaryOrderError("the bound version is not the last, or an earlier one stands")
    for later, package_id in successors.values():
        if states[later].package_id != package_id:
            raise BoundaryOrderError("a supersession names a package its successor does not hold")
    state = states[started[0]]
    boundary_id = boundary_version_id(execution_id, started[0])
    if state.seal == CONSTRUCTION_FAILED:
        return RecoveryNoPackage(boundary_id, contract)
    manifest = state.manifest
    if manifest is None or state.package_id is None or state.seed_digest is None:
        raise BoundaryOrderError("the bound version has no frozen package")
    if not _is_digest(state.interpreter_sha256) or not _is_digest(
        state.interpreter_realpath_sha256
    ):
        raise BoundaryOrderError("the bound version's admission has no interpreter pin")
    assert state.interpreter_sha256 is not None and state.interpreter_realpath_sha256 is not None
    lost = criteria_without_admitted_check(manifest, state.excluded)
    admitted = [check for check in manifest.checks if check.check_id not in state.excluded]
    covered = {link.criterion_key for check in admitted for link in check.assertions} - set(lost)
    return RecoveryBound(
        execution_id=execution_id,
        boundary_id=boundary_id,
        package_id=state.package_id,
        seed_digest=state.seed_digest,
        contract=contract,
        interpreter_sha256=state.interpreter_sha256,
        interpreter_realpath_sha256=state.interpreter_realpath_sha256,
        criterion_keys=manifest.criterion_keys,
        covered=frozenset(covered),
        held_out_checks=frozenset(
            check.check_id for check in admitted if check.check_id in manifest.held_out_checks
        ),
    )
