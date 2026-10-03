"""Event factories for the check-package boundary.

Event Types (aggregate_type ``boundary``, aggregate_id = boundary id):
    boundary.check_package.enabled - aggregate_id is the run's execution id,
        not a boundary version: the check package was on for this run; the
        first boundary event of the run, written before construction
    boundary.check_package.frozen - package digest and event-safe manifest
    boundary.check_package.construction_failed - no package; verifier indeterminate
    boundary.check_package.admission_completed - base admission receipt
    boundary.actor.started - a worker bound to this boundary started
    boundary.candidate.verified - frozen package run on a candidate
    boundary.check_package.superseded - product regeneration: this boundary
        version was replaced by a later version before any worker bound to it
    boundary.check_package.replacement_abandoned - this replacement version
        was not admitted (or not built), and the worker starts on the earlier
        version it names; nothing more is recorded on it
    boundary.binding.recorded - after the worker stopped: the tier and binding
        of every check (late bindings are data; no code, no cases)
    boundary.acceptance.resumed - a run resumed after its controller died:
        the package decision recovered from the journal (recomputed only
        while the same process holds the admitted package, else undecided)

Payloads never carry generated file contents, check argv, or check output, so
the shared journal does not expose check code or counterexamples. Every event
names a package by its opaque id (``package.seal_package``), never by an
unkeyed digest. The package record (held-out cases as ids only) and complete
receipts are stored separately (``package.write_package_record``,
``receipts.write_receipt``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ouroboros.boundary.binding import (
    BINDING_GRAMMAR,
    Binding,
    BindingSource,
    CallKind,
    CheckTier,
    tier_summary,
)
from ouroboros.boundary.package import (
    PACKAGE_ID_BYTES,
    CheckPackage,
    CheckRole,
    package_record_bytes,
    sha256_bytes,
)
from ouroboros.boundary.receipts import (
    AdmissionJournal,
    AdmissionResult,
    CandidateVerification,
    VerificationJournal,
)
from ouroboros.events.base import BaseEvent

BOUNDARY_AGGREGATE_TYPE = "boundary"

PACKAGE_FROZEN = "boundary.check_package.frozen"
CONSTRUCTION_FAILED = "boundary.check_package.construction_failed"
ADMISSION_COMPLETED = "boundary.check_package.admission_completed"
ACTOR_STARTED = "boundary.actor.started"
CANDIDATE_VERIFIED = "boundary.candidate.verified"
SUPERSEDED = "boundary.check_package.superseded"
REPLACEMENT_ABANDONED = "boundary.check_package.replacement_abandoned"
ACCEPTANCE_RECONCILED = "boundary.acceptance.reconciled"
BINDING_RECORDED = "boundary.binding.recorded"
ACCEPTANCE_RESUMED = "boundary.acceptance.resumed"
REFERENCE_CHECKED = "boundary.oracle.reference_checked"
CHECK_PACKAGE_ENABLED = "boundary.check_package.enabled"

_VERSION_INFIX = "/check_package/v"


def boundary_version_id(execution_id: str, version: int) -> str:
    """The id of a run's ``version``-th boundary version (``<execution_id>/check_package/v<n>``)."""
    if not execution_id or version < 1:
        raise ValueError("a boundary version needs an execution id and a version >= 1")
    return f"{execution_id}{_VERSION_INFIX}{version}"


def parse_boundary_version(boundary_id: str) -> tuple[str, int] | None:
    """``(execution_id, version)`` of an id ``boundary_version_id`` made, else ``None``.

    The inverse of ``boundary_version_id``: a boundary id that it did not
    produce is a standalone boundary (not a version of a run).
    """
    execution_id, infix, tail = boundary_id.rpartition(_VERSION_INFIX)
    if not infix or not execution_id or not tail.isdecimal():
        return None
    version = int(tail)
    if version < 1 or boundary_version_id(execution_id, version) != boundary_id:
        return None
    return execution_id, version


# --------------------------------------------------------------------------
# Typed payloads. Every event a caller supplies content for takes one of these
# models (closed fields, ``extra="forbid"``); the factories add the identity
# fields (the package id) after validation, so a payload can neither name
# another package nor carry fields the journal does not define.


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def journal_data(self) -> dict[str, Any]:
        """JSON-safe dump with only the fields that were given."""
        return self.model_dump(mode="json", exclude_unset=True)


ArtifactCheckModeName = Literal["decide", "record", "off"]
"""How an artifact check acts (``base_regression.ArtifactCheckMode``)."""


class RunContract(_Payload):
    """The settings that decide anything after the worker starts, fixed for the run.

    Written on the run's ``boundary.check_package.enabled`` record before
    construction; a resumed run reads them back instead of the live config,
    so a resume decides with the settings the run started with.
    """

    check_timeout_seconds: int = Field(gt=0)
    base_regression: ArtifactCheckModeName = "off"
    """How the base regression check acts (``boundary/base_regression.py``)."""
    worker_test_gate: ArtifactCheckModeName = "off"
    """How the worker-test gate acts. Both are ``off`` when absent, so a run
    recorded before the settings existed resumes exactly as it started."""

    @field_validator("base_regression", "worker_test_gate", mode="before")
    @classmethod
    def _switch_form(cls, value: Any) -> Any:
        # The earlier on/off form of the setting.
        if isinstance(value, bool):
            return "decide" if value else "off"
        return value


class BaseRunRecord(_Payload):
    """The base run of one declared binding (status and reason, no output)."""

    status: str
    reason: str
    signature_seen: bool
    return_code: int | None


class DeclaredBindingRecord(_Payload):
    """Grammar check and base run of one worker-declared binding."""

    criterion_key: str
    valid: bool
    indeterminate: bool
    reason: str
    binding: Binding | None
    base_run: BaseRunRecord | None


class CheckBindingRecord(_Payload):
    """The tier and binding one check ran (or would run) through."""

    criterion_key: str
    check_id: str
    tier: CheckTier
    binding_source: BindingSource | None
    binding: Binding | None
    status_hint: str | None
    reason: str
    declared: DeclaredBindingRecord | None


class BindingsPayload(_Payload):
    """``boundary.binding.recorded``: every check's tier and binding (data, no code).

    ``final``: the run's bindings, once, before its verification; ``repair``:
    one attempt's bindings while the worker runs; ``resumed``: a resumed run's
    bindings, before the verification and decision it records.
    """

    schema_version: Literal["ouroboros.binding_record.v1"] = "ouroboros.binding_record.v1"
    phase: Literal["final", "repair", "resumed"]
    checks: tuple[CheckBindingRecord, ...]
    root_ac_index: int | None = None
    retry_attempt: int | None = None
    status: str | None = None


ArtifactCheckName = Literal["base_regression", "worker_tests"]
"""A controller-run check of the whole artifact (``acceptance.ArtifactCheck``)."""


class CriterionDecisionRecord(_Payload):
    """One criterion's final acceptance and the signals behind it.

    ``tier`` is for display only (the weakest tier over the passing checks)
    and has no authority: an A' oracle plus an advisory script shows ``S``.
    What decided a pass is ``declared_binding_pass``.
    """

    root_ac_index: int
    criterion_key: str
    package_status: str
    tier: CheckTier
    reason: str
    failed_heldout_only: bool
    binding: Binding | None
    existing_outcome: str | None
    existing_failure_class: str | None
    existing_accepted: bool
    accepted: bool
    governed_by: str
    declared_binding_pass: bool
    """The criterion's package ``pass`` depends on a worker-declared binding (tier A').

    Such a pass only corroborates: it is decided by the existing verifier
    when that verifier rejected the attempt, and it never accepts a criterion
    the existing verifier rejected. ``False`` for every status but ``pass``.
    """
    artifact_check: ArtifactCheckName | None = None
    """The controller-run artifact check that failed this criterion
    (``boundary/base_regression.py``), or ``None``. Only a ``fail`` the check
    package decided carries one, and only on a criterion the package's own
    results left unverified or uncovered (``ledger._require_supported_statuses``)."""


_DECISION_STATUSES = frozenset({"pass", "fail", "indeterminate", "unverified", "uncovered"})
_GOVERNORS = frozenset({"check_package", "execution", "existing_verifier"})
_PACKAGE_ACCEPTS = frozenset({"pass", "unverified", "uncovered"})
_EXISTING_DECIDES = frozenset({"unverified", "uncovered"})
_UNDECIDED_STATUSES = frozenset({"indeterminate", "uncovered"})
_NOT_PACKAGE_DECIDED = frozenset({"unverified", "uncovered"})
LEGACY_RULE_SCHEMA = "ouroboros.acceptance_reconciliation.v3"
"""The decision schema of the product (``legacy_decides_unverified``): the only one journaled."""
LOW_COVERAGE_SHARE = 0.5
_EXISTING_PASS_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})


def artifact_verdict_of(statuses: Iterable[str]) -> str:
    """Precedence: fail, then indeterminate, then pass (one verified pass), else unverified."""
    values = set(statuses)
    for verdict in ("fail", "indeterminate", "pass"):
        if verdict in values:
            return verdict
    return "unverified"


def coverage_of(total: int, not_decided: int, unverified: int) -> str:
    """``low`` when half or more of ``total`` were not decided by the package or any is unverified."""
    if unverified or (total and not_decided / total >= LOW_COVERAGE_SHARE):
        return "low"
    return "partial" if not_decided else "full"


class ReconciliationPayload(_Payload):
    """``boundary.acceptance.reconciled``: per-criterion decisions and the run's."""

    schema_version: str
    run_accepted: bool
    existing_run_accepted: bool
    artifact_verdict: str
    verified_pass_count: int
    unverified_count: int
    criterion_count: int
    tier_summary: dict[str, int]
    criteria: tuple[CriterionDecisionRecord, ...]
    legacy_decided_count: int | None = None
    verification_coverage: str | None = None
    undecided_reason: str | None = None
    """Set when the package could not decide (for example the authority raised):
    every covered criterion is then indeterminate. Only such a decision may be
    recorded without a candidate verification of a runnable check."""

    @model_validator(mode="after")
    def _consistent(self) -> ReconciliationPayload:
        """Every acceptance bit agrees with the status and signal that decided it.

        The journal only admits decisions the reconciliation rule can produce
        (``acceptance.reconcile_acceptance``): the package accepts exactly its
        pass, unverified and uncovered criteria and never a fail or an
        indeterminate one; a criterion nobody attempted is never accepted; the
        existing verifier's decision is its own verdict, and of the package's
        passes it decides only one that rests on a worker-declared binding
        (``declared_binding_pass``; the display ``tier`` decides nothing); such
        a pass never accepts over the existing verifier's rejection; the run
        is accepted exactly when every criterion is. An undecided decision
        has no pass and no fail.
        """
        for item in self.criteria:
            status, governor = item.package_status, item.governed_by
            if status not in _DECISION_STATUSES or governor not in _GOVERNORS:
                raise ValueError("a decision names an unknown status or governor")
            if item.declared_binding_pass and status != "pass":
                raise ValueError("only a pass can rest on a worker-declared binding")
            if item.declared_binding_pass and item.accepted and not item.existing_accepted:
                raise ValueError("a declared-binding pass never overrules the existing verifier")
            if item.artifact_check is not None and (status, governor) != ("fail", "check_package"):
                raise ValueError("an artifact check only fails a criterion the package decides")
            if governor == "check_package":
                expected = status in _PACKAGE_ACCEPTS
            elif governor == "execution":
                expected = False
            else:
                expected = item.existing_accepted
                if status not in _EXISTING_DECIDES and not item.declared_binding_pass:
                    raise ValueError("the existing verifier decides only what the package did not")
            if item.accepted != expected:
                raise ValueError("a criterion's acceptance disagrees with what decided it")
            if self.undecided_reason and status not in _UNDECIDED_STATUSES:
                raise ValueError("an undecided decision carries only indeterminate or uncovered")
        if self.run_accepted != (bool(self.criteria) and all(i.accepted for i in self.criteria)):
            raise ValueError("the run's acceptance disagrees with its criteria")
        if self.criterion_count != len(self.criteria):
            raise ValueError("criterion_count disagrees with the criteria")
        statuses = [item.package_status for item in self.criteria]
        if (
            self.artifact_verdict != artifact_verdict_of(statuses)
            or self.verified_pass_count != statuses.count("pass")
            or self.unverified_count != len(self._unverified())
            or self.tier_summary != tier_summary(item.tier for item in self.criteria)
        ):
            raise ValueError("a decision's summary disagrees with its criteria")
        return self

    def _unverified(self) -> list[CriterionDecisionRecord]:
        """Criteria no verifier decided: not the package's, and not legacy-decided."""
        return [
            item
            for item in self.criteria
            if item.package_status in _NOT_PACKAGE_DECIDED
            and item.governed_by != "existing_verifier"
        ]

    def check_product_rule(self) -> None:
        """``ValueError`` unless the product's rule (``legacy_decides_unverified``) wrote it.

        The product decides every run under that rule (schema
        ``LEGACY_RULE_SCHEMA``): a criterion the package did not verify
        (unverified or uncovered) is decided by the existing verifier's own
        verdict; the package accepts one only where the existing verifier
        accepted it without evidence, so it never accepts over a rejection.
        The rule's summary (legacy-decided count, coverage) is present and
        agrees with the criteria.
        """
        if self.schema_version != LEGACY_RULE_SCHEMA:
            raise ValueError("a journaled decision is made under the product's legacy rule")
        for item in self.criteria:
            if (
                item.package_status in _NOT_PACKAGE_DECIDED
                and item.accepted
                and not item.existing_accepted
            ):
                raise ValueError("a criterion the package did not verify is the legacy verdict")
            # The existing verifier accepts only a successful outcome; with no
            # per-criterion outcome its verdict is the run's.
            if item.existing_outcome is None:
                if item.existing_accepted != self.existing_run_accepted:
                    raise ValueError("a criterion without an outcome takes the run's verdict")
            elif item.existing_accepted and item.existing_outcome not in _EXISTING_PASS_OUTCOMES:
                raise ValueError("the existing verifier accepted an outcome that is no success")
        legacy = sum(1 for item in self.criteria if item.governed_by == "existing_verifier")
        not_decided = sum(1 for i in self.criteria if i.package_status in _NOT_PACKAGE_DECIDED)
        accepted_unverified = [i for i in self._unverified() if i.governed_by != "execution"]
        coverage = coverage_of(len(self.criteria), not_decided, len(accepted_unverified))
        if self.legacy_decided_count != legacy or self.verification_coverage != coverage:
            raise ValueError("a decision's legacy summary disagrees with its criteria")
        indexes = sorted(item.root_ac_index for item in self.criteria)
        if indexes != list(range(len(self.criteria))):
            raise ValueError("a decision names each root criterion once, by position")


class ResumedPayload(ReconciliationPayload):
    """``boundary.acceptance.resumed``: the decision a resumed run recomputed."""

    source: str
    held_out_checks: tuple[str, ...] = ()
    reason: str | None = None


REFERENCE_CHECK_SCHEMA = "ouroboros.reference_check.v2"

ReferenceUncoveredReason = Literal[
    "oracle_inconsistent",
    "reference_contradicts_stated_case",
    "reference_unavailable",
    "reference_check_left_no_checks",
]
"""Why the reference check left a criterion uncovered (``reference_check``)."""


class ExcludedCasesRecord(_Payload):
    """A kept oracle of the frozen package and how many of its cases the reference check excluded.

    Named by its frozen (re-minted) check id and a count: case ids from before
    the rebuild name nothing in the frozen package, and values never appear.
    """

    check_id: str
    excluded_count: int = Field(ge=1)
    reason: Literal["oracle_inconsistent"]


class UncoveredRecord(_Payload):
    """A criterion the reference check left uncovered (a wholly dropped oracle), with its reason."""

    criterion_key: str
    reason: ReferenceUncoveredReason


class ReferenceCheckPayload(_Payload):
    """``boundary.oracle.reference_checked``: what the reference check excluded.

    Expressed against the frozen package: each kept oracle once, by check id
    with its excluded-case count; each criterion whose oracles were all
    dropped once, as uncovered with its reason (no check id).
    """

    schema_version: Literal["ouroboros.reference_check.v2"]
    excluded_cases: tuple[ExcludedCasesRecord, ...]
    uncovered: tuple[UncoveredRecord, ...]

    @model_validator(mode="after")
    def _once(self) -> ReferenceCheckPayload:
        checks = [item.check_id for item in self.excluded_cases]
        keys = [item.criterion_key for item in self.uncovered]
        if len(set(checks)) != len(checks) or len(set(keys)) != len(keys):
            raise ValueError("a reference check names each oracle and each criterion once")
        return self


def _require(payload: object, model: type[_Payload]) -> None:
    if not isinstance(payload, model):
        raise TypeError(f"{model.__name__} expected, not {type(payload).__name__}")


def _event(boundary_id: str, event_type: str, data: dict[str, Any]) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type=BOUNDARY_AGGREGATE_TYPE,
        aggregate_id=boundary_id,
        data=data,
    )


def cite(package_id: str | None, *, prefix: str = "") -> dict[str, str | None]:
    """``{"<prefix>package_id": package_id}``: how every event names a package."""
    return {f"{prefix}package_id": package_id}


def check_package_enabled_event(execution_id: str, contract: RunContract) -> BaseEvent:
    """The check package is on for the run ``execution_id``, with its run contract."""
    _require(contract, RunContract)
    return _event(
        execution_id,
        CHECK_PACKAGE_ENABLED,
        {"execution_id": execution_id, "contract": contract.journal_data()},
    )


def enabled_contract(data: dict[str, Any]) -> RunContract:
    """The run contract an enabled record carries; ``ValueError`` when it is malformed."""
    return RunContract.model_validate(data.get("contract"))


def package_frozen_event(boundary_id: str, package: CheckPackage) -> BaseEvent:
    """The package id and Seed digest (I2) plus the event-safe manifest summary.

    The package must be sealed (``package.seal_package``); its unkeyed digest
    is never recorded. ``record_sha256`` is computed here, from the same bytes
    the store writes (``package.package_record_bytes``: visible cases in full,
    held-out cases as ids only), so a resume in another process can tell
    whether the stored record was edited, and no caller can put another
    digest in its place.
    """
    return _event(
        boundary_id,
        PACKAGE_FROZEN,
        {
            **cite(package.package_id),
            "seed_digest": package.seed_digest,
            "record_sha256": sha256_bytes(package_record_bytes(package)),
            "manifest": package.manifest_summary(),
        },
    )


def construction_failed_event(
    boundary_id: str, *, seed_digest: str, input_digest: str, reason: str
) -> BaseEvent:
    """Record that no package exists for this boundary; its verifier is indeterminate."""
    return _event(
        boundary_id,
        CONSTRUCTION_FAILED,
        {"seed_digest": seed_digest, "input_digest": input_digest, "reason": reason},
    )


def admission_completed_event(boundary_id: str, result: AdmissionResult) -> BaseEvent:
    """Base admission receipt."""
    return _event(boundary_id, ADMISSION_COMPLETED, result.event_summary())


def actor_started_event(
    boundary_id: str,
    *,
    actor_id: str,
    package_id: str | None,
    runtime: str | None,
) -> BaseEvent:
    """A worker bound to this boundary started after the boundary was sealed."""
    return _event(
        boundary_id,
        ACTOR_STARTED,
        {
            "actor_id": actor_id,
            **cite(package_id),
            "runtime": runtime,
        },
    )


def candidate_verified_event(boundary_id: str, verification: CandidateVerification) -> BaseEvent:
    """Frozen package run on one candidate."""
    return _event(boundary_id, CANDIDATE_VERIFIED, verification.event_summary())


def superseded_event(
    boundary_id: str,
    *,
    superseded_by: str,
    package_id: str | None,
    successor_package_id: str | None,
    reason: str,
) -> BaseEvent:
    """Mark a sealed boundary version as replaced by a later version."""
    return _event(
        boundary_id,
        SUPERSEDED,
        {
            "superseded_by": superseded_by,
            **cite(package_id),
            **cite(successor_package_id, prefix="successor_"),
            "reason": reason,
        },
    )


def replacement_abandoned_event(
    boundary_id: str, *, bound: str, package_id: str | None
) -> BaseEvent:
    """Close a replacement version that was not admitted; ``bound`` stays the worker's version."""
    return _event(boundary_id, REPLACEMENT_ABANDONED, {"bound": bound, **cite(package_id)})


def acceptance_reconciled_event(
    boundary_id: str,
    *,
    package_id: str | None,
    reconciliation: ReconciliationPayload,
) -> BaseEvent:
    """Per-criterion acceptance: package verdict, existing verdict (advisory), decision."""
    _require(reconciliation, ReconciliationPayload)
    return _event(
        boundary_id,
        ACCEPTANCE_RECONCILED,
        {**reconciliation.journal_data(), **cite(package_id)},
    )


def binding_recorded_event(
    boundary_id: str, *, package_id: str, payload: BindingsPayload
) -> BaseEvent:
    """Tiers and bindings of every check, recorded after the worker stopped."""
    _require(payload, BindingsPayload)
    return _event(boundary_id, BINDING_RECORDED, {**payload.journal_data(), **cite(package_id)})


def reference_checked_event(
    boundary_id: str, *, package_id: str, payload: ReferenceCheckPayload
) -> BaseEvent:
    """Cases excluded and criteria uncovered by the reference check (ids, never values)."""
    _require(payload, ReferenceCheckPayload)
    return _event(boundary_id, REFERENCE_CHECKED, {**payload.journal_data(), **cite(package_id)})


def acceptance_resumed_event(
    boundary_id: str, *, package_id: str | None, payload: ResumedPayload
) -> BaseEvent:
    """The package decision a resumed run recomputed (statuses and reasons, no case values)."""
    _require(payload, ResumedPayload)
    return _event(boundary_id, ACCEPTANCE_RESUMED, {**payload.journal_data(), **cite(package_id)})


# --------------------------------------------------------------------------
# The journal gateway. Every record the ledger appends, and every record replay
# (``ledger.advance``) or the recovery projection reads, passes
# ``validate_record`` first: the envelope, the record's exact closed schema
# (what its factory above writes, no field missing or added), and, once a
# boundary version is sealed, the identities the seal fixed. A record that
# fails is refused on write and flagged on replay, so a record the product
# could not have written reaches no transition and no projection.


class JournalRecordError(ValueError):
    """A journal record is not one the product writes (envelope, schema, or identity)."""


@dataclass(frozen=True, slots=True)
class FrozenIdentity:
    """What a boundary version's seal fixed; every later record must agree with it."""

    package_id: str | None
    seed_digest: str | None
    criterion_keys: frozenset[str] | None
    """The frozen manifest's criterion keys; ``None`` when the version has no manifest."""
    check_ids: frozenset[str]
    """The frozen package's check ids (none without a package)."""


RUN_IDENTITY = FrozenIdentity(
    package_id=None, seed_digest=None, criterion_keys=None, check_ids=frozenset()
)
"""A run aggregate's records cite no package, no Seed and no check."""


@dataclass(frozen=True, slots=True)
class Cited:
    """The identities one record names, compared with the ``FrozenIdentity``."""

    package_id: str | None
    seed_digest: str | None = None
    """Set only by records that carry a Seed digest."""
    criterion_keys: tuple[str, ...] = ()
    check_ids: tuple[str, ...] = ()
    decided: tuple[str, ...] | None = None
    """A decision's criteria: exactly the frozen manifest's, each once."""


class _Citing(Protocol):
    def cited(self) -> Cited: ...


class EnabledRecord(_Payload):
    """``boundary.check_package.enabled`` as journaled."""

    execution_id: str
    contract: RunContract

    def cited(self) -> Cited:
        return Cited(None)


_HEX = frozenset("0123456789abcdef")


def _hex(value: str, length: int = 64) -> str:
    if len(value) != length or not set(value) <= _HEX:
        raise ValueError(f"a digest or id is {length} lowercase hex characters")
    return value


class ManifestCheck(_Payload):
    """One check in ``CheckPackage.manifest_summary``."""

    check_id: str = Field(min_length=1)
    role: CheckRole
    criterion_keys: tuple[str, ...]
    assertion_ids: tuple[str, ...]


class ManifestOracleData(_Payload):
    """The oracle data file in the manifest: named by kind, never by digest."""

    kind: Literal["oracle_data"]
    held_out_redacted: Literal[True]


class ManifestFile(_Payload):
    """Every other package file in the manifest: its kind only (no digest, no size)."""

    kind: Literal["oracle_harness", "generated"]


class ManifestOracle(_Payload):
    """One oracle in the manifest: ids, kind and counts, no case and no symbol."""

    check_id: str
    criterion_key: str
    call_kind: CallKind
    param_count: int = Field(ge=0)
    target_named_in_criterion: bool
    case_count: int = Field(ge=1)
    held_out_count: int = Field(ge=1)
    """Every oracle declares at least one held-out case (``oracle.OracleSpec``)."""

    @model_validator(mode="after")
    def _counts(self) -> ManifestOracle:
        if self.held_out_count > self.case_count:
            raise ValueError("an oracle holds out more cases than it has")
        return self


class ManifestUncovered(_Payload):
    """A criterion the package left uncovered, by key only (its reason is not persisted)."""

    criterion_key: str


class ManifestRecord(_Payload):
    """The exact closed schema of ``CheckPackage.manifest_summary`` (the frozen manifest).

    ``binding_grammar`` and ``oracles`` are written together and only for a
    package with oracles. ``ledger.frozen_manifest`` checks how the fields
    relate (links, coverage, oracles).
    """

    schema_version: Literal["ouroboros.check_package.v1", "ouroboros.check_package.v2"]
    package_id: str
    seed_digest: str
    criterion_keys: tuple[str, ...]
    checks: tuple[ManifestCheck, ...]
    files: tuple[ManifestOracleData | ManifestFile, ...]
    base_file_count: int = Field(ge=0)
    scratch_path_count: int = Field(ge=0)
    uncovered: tuple[ManifestUncovered, ...]
    binding_grammar: str | None = None
    oracles: tuple[ManifestOracle, ...] | None = None

    _digests = field_validator("seed_digest")(_hex)
    _id = field_validator("package_id")(lambda value: _hex(value, 2 * PACKAGE_ID_BYTES))

    @model_validator(mode="after")
    def _oracles_together(self) -> ManifestRecord:
        """What a package with oracles carries follows from having them (``CheckPackage``).

        Oracles and the binding grammar are listed together, or neither; a
        package with oracles is schema v2 with the product grammar and one
        oracle harness and one oracle data file, one without them schema v1
        with neither file.
        """
        fields = self.model_fields_set
        if ("oracles" in fields) != ("binding_grammar" in fields) or self.oracles == ():
            raise ValueError("a manifest lists oracles and their grammar together, or neither")
        oracles = bool(self.oracles)
        kinds = [item.kind for item in self.files]
        if (
            self.schema_version
            != ("ouroboros.check_package.v2" if oracles else "ouroboros.check_package.v1")
            or (oracles and self.binding_grammar != BINDING_GRAMMAR)
            or kinds.count("oracle_harness") != int(oracles)
            or kinds.count("oracle_data") != int(oracles)
        ):
            raise ValueError("a manifest's schema and files disagree with its oracles")
        return self


class FrozenRecord(_Payload):
    """``boundary.check_package.frozen`` as journaled (``package_frozen_event``)."""

    package_id: str
    seed_digest: str
    record_sha256: str
    manifest: ManifestRecord

    _digests = field_validator("seed_digest", "record_sha256")(_hex)
    _id = field_validator("package_id")(lambda value: _hex(value, 2 * PACKAGE_ID_BYTES))

    def cited(self) -> Cited:
        return Cited(self.package_id, self.seed_digest)


class ConstructionFailedRecord(_Payload):
    """``boundary.check_package.construction_failed`` as journaled."""

    seed_digest: str
    input_digest: str
    reason: str = Field(min_length=1)

    _digests = field_validator("seed_digest", "input_digest")(_hex)

    def cited(self) -> Cited:
        return Cited(None, self.seed_digest)


class ReferenceCheckedRecord(ReferenceCheckPayload):
    """``boundary.oracle.reference_checked`` as journaled."""

    package_id: str

    def cited(self) -> Cited:
        return Cited(
            self.package_id,
            criterion_keys=tuple(item.criterion_key for item in self.uncovered),
            check_ids=tuple(item.check_id for item in self.excluded_cases),
        )


class AdmissionRecord(AdmissionJournal):
    """``boundary.check_package.admission_completed`` as journaled."""

    def cited(self) -> Cited:
        return Cited(self.package_id, self.seed_digest, check_ids=self.check_ids())


class ActorStartedRecord(_Payload):
    """``boundary.actor.started`` as journaled."""

    actor_id: str
    package_id: str | None
    runtime: str | None

    def cited(self) -> Cited:
        return Cited(self.package_id)


class SupersededRecord(_Payload):
    """``boundary.check_package.superseded`` as journaled."""

    superseded_by: str
    package_id: str | None
    successor_package_id: str | None
    reason: str

    def cited(self) -> Cited:
        return Cited(self.package_id)


class AbandonedRecord(_Payload):
    """``boundary.check_package.replacement_abandoned`` as journaled."""

    bound: str
    package_id: str | None

    def cited(self) -> Cited:
        return Cited(self.package_id)


class BindingsRecord(BindingsPayload):
    """``boundary.binding.recorded`` as journaled: one record per check, each once."""

    package_id: str

    @model_validator(mode="after")
    def _once(self) -> BindingsRecord:
        ids = [item.check_id for item in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("bindings name a check twice")
        return self

    def cited(self) -> Cited:
        declared = [item.declared for item in self.checks if item.declared is not None]
        bindings = [
            binding
            for binding in (
                *(item.binding for item in self.checks),
                *(item.binding for item in declared),
            )
            if binding is not None
        ]
        return Cited(
            self.package_id,
            criterion_keys=(
                *(item.criterion_key for item in self.checks),
                *(item.criterion_key for item in declared),
                *(binding.criterion_key for binding in bindings),
            ),
            check_ids=tuple(item.check_id for item in self.checks),
        )


class VerificationRecord(VerificationJournal):
    """``boundary.candidate.verified`` as journaled: one result per check, each once."""

    @model_validator(mode="after")
    def _once(self) -> VerificationRecord:
        ids = [check.check_id for check in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("a verification names a check twice")
        return self

    def cited(self) -> Cited:
        keys = tuple(binding.criterion_key for binding in (self.bindings or {}).values())
        return Cited(
            self.package_id, self.seed_digest, criterion_keys=keys, check_ids=self.check_ids()
        )


def _decision_cited(package_id: str | None, criteria: tuple[CriterionDecisionRecord, ...]) -> Cited:
    keys = tuple(item.criterion_key for item in criteria)
    bound = tuple(item.binding.criterion_key for item in criteria if item.binding is not None)
    return Cited(package_id, criterion_keys=(*keys, *bound), decided=keys)


class ReconciledRecord(ReconciliationPayload):
    """``boundary.acceptance.reconciled`` as journaled (under the product's rule)."""

    package_id: str | None

    @model_validator(mode="after")
    def _product_rule(self) -> ReconciledRecord:
        self.check_product_rule()
        return self

    def cited(self) -> Cited:
        return _decision_cited(self.package_id, self.criteria)


class ResumedRecord(ResumedPayload):
    """``boundary.acceptance.resumed`` as journaled (on a version or on the run)."""

    package_id: str | None

    @model_validator(mode="after")
    def _product_rule(self) -> ResumedRecord:
        self.check_product_rule()
        return self

    def cited(self) -> Cited:
        cited = _decision_cited(self.package_id, self.criteria)
        return replace(cited, check_ids=self.held_out_checks)


JOURNAL_RECORDS: Mapping[str, type[BaseModel]] = {
    CHECK_PACKAGE_ENABLED: EnabledRecord,
    PACKAGE_FROZEN: FrozenRecord,
    CONSTRUCTION_FAILED: ConstructionFailedRecord,
    REFERENCE_CHECKED: ReferenceCheckedRecord,
    ADMISSION_COMPLETED: AdmissionRecord,
    ACTOR_STARTED: ActorStartedRecord,
    SUPERSEDED: SupersededRecord,
    REPLACEMENT_ABANDONED: AbandonedRecord,
    BINDING_RECORDED: BindingsRecord,
    CANDIDATE_VERIFIED: VerificationRecord,
    ACCEPTANCE_RECONCILED: ReconciledRecord,
    ACCEPTANCE_RESUMED: ResumedRecord,
}
"""The one closed schema of every boundary record, by event type."""

_LABELS = {
    CHECK_PACKAGE_ENABLED: "the run's enabled record",
    PACKAGE_FROZEN: "the frozen record",
    CONSTRUCTION_FAILED: "the construction_failed record",
    REFERENCE_CHECKED: "a reference check",
    ADMISSION_COMPLETED: "admission receipt",
    ACTOR_STARTED: "actor start",
    SUPERSEDED: "a supersession",
    REPLACEMENT_ABANDONED: "an abandoned replacement",
    BINDING_RECORDED: "bindings",
    CANDIDATE_VERIFIED: "candidate verification",
    ACCEPTANCE_RECONCILED: "acceptance",
    ACCEPTANCE_RESUMED: "a resumed decision",
}
_DECISIONS = frozenset({ACCEPTANCE_RECONCILED, ACCEPTANCE_RESUMED})


def validate_record(event: BaseEvent, frozen: FrozenIdentity | None) -> Any:
    """The typed record ``event`` holds; ``JournalRecordError`` unless the product wrote it.

    Checks the envelope (a boundary aggregate, a known record type), the
    record's exact closed schema (``JOURNAL_RECORDS``), and, with ``frozen``
    (the identities of a sealed version, or ``RUN_IDENTITY``), that the record
    cites the sealed package and Seed and names only the frozen checks and
    criteria, a decision exactly the frozen criteria.
    """
    model = JOURNAL_RECORDS.get(event.type)
    if event.aggregate_type != BOUNDARY_AGGREGATE_TYPE or not event.aggregate_id or model is None:
        raise JournalRecordError(f"{event.type} is not a boundary record")
    try:
        record = model.model_validate(event.data)
    except ValidationError as exc:
        if event.type in _DECISIONS:
            raise JournalRecordError(
                "a recorded decision disagrees with the statuses that decided it"
            ) from exc
        raise JournalRecordError(
            f"{_LABELS[event.type]} is not a record the product writes"
        ) from exc
    if frozen is not None:
        _bind(_LABELS[event.type], cast(_Citing, record).cited(), frozen)
    return record


def _bind(label: str, cited: Cited, frozen: FrozenIdentity) -> None:
    if cited.package_id != frozen.package_id:
        raise JournalRecordError(f"{label} names a different package than the boundary's seal")
    if cited.seed_digest is not None and cited.seed_digest != frozen.seed_digest:
        raise JournalRecordError(f"{label} names a different Seed than the frozen package")
    if not set(cited.check_ids) <= frozen.check_ids:
        raise JournalRecordError(f"{label} names a check the frozen package does not hold")
    if frozen.criterion_keys is None:
        return
    if not set(cited.criterion_keys) <= frozen.criterion_keys:
        raise JournalRecordError(f"{label} names a criterion the frozen manifest does not hold")
    decided = cited.decided
    if decided is not None and (
        len(set(decided)) != len(decided) or set(decided) != frozen.criterion_keys
    ):
        raise JournalRecordError(f"{label} does not decide exactly the frozen manifest's criteria")


def validate_run_record(event: BaseEvent, execution_id: str) -> EnabledRecord | ResumedRecord:
    """A record of the run's own aggregate; ``JournalRecordError`` unless the product wrote it.

    The product writes there only the enabled record of this run
    (``check_package_enabled_event``) and a resumed decision made without a
    usable boundary (``BoundaryLedger.record_resumed_undecided``): it cites no
    package and no check, states why, and decides nothing (every criterion
    indeterminate or uncovered).
    """
    if event.aggregate_id != execution_id or event.type not in (
        CHECK_PACKAGE_ENABLED,
        ACCEPTANCE_RESUMED,
    ):
        raise JournalRecordError(
            "the run aggregate holds a record the product does not write there"
        )
    record = validate_record(event, RUN_IDENTITY)
    if isinstance(record, EnabledRecord) and record.execution_id != execution_id:
        raise JournalRecordError("the run's enabled record names another run")
    if isinstance(record, ResumedRecord) and (
        not record.reason
        or any(item.package_status not in _UNDECIDED_STATUSES for item in record.criteria)
    ):
        raise JournalRecordError("a run-level resumed decision must be undecided and say why")
    return cast(EnabledRecord | ResumedRecord, record)
