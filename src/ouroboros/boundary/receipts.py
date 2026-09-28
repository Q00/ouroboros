"""Receipts of a check package run: per-check executions, admission, candidate verification.

These are data only. ``boundary/admission.py`` produces them; the ledger
(``boundary/ledger.py``) records their journal-safe form and ``write_receipt``
stores the complete form outside every checkout.

Every receipt is closed all the way down: each model forbids fields it does
not define, and nested values are models too (``Binding``, ``OracleResult``,
``CheckTier``), never a free-form mapping, so nothing a caller adds reaches a
stored receipt or the journal.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.boundary.binding import Binding, CheckTier
from ouroboros.boundary.oracle import OracleResult, journal_safe_oracle_result, redact_held_out
from ouroboros.boundary.package import (
    PACKAGE_ID_BYTES,
    CheckRole,
    canonical_json_bytes,
    publish_exact,
    sha256_bytes,
)

_HEX = frozenset("0123456789abcdef")


def _hex_digest(value: str, length: int, what: str) -> str:
    if not isinstance(value, str) or len(value) != length or not set(value) <= _HEX:
        raise ValueError(f"{what} must be {length} lowercase hex characters")
    return value


class _Receipt(BaseModel):
    """A receipt model: immutable and closed (a field it does not define is an error)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _CitesPackage(_Receipt):
    """The package a receipt names: its Seed digest and id, both validated."""

    package_id: str | None
    seed_digest: str

    @field_validator("seed_digest")
    @classmethod
    def _seed(cls, value: str) -> str:
        return _hex_digest(value, 64, "seed_digest")

    @field_validator("package_id")
    @classmethod
    def _package(cls, value: str | None) -> str | None:
        return None if value is None else _hex_digest(value, 2 * PACKAGE_ID_BYTES, "package_id")


class PackageReceipt(_CitesPackage):
    """A receipt about one package: it names the package's Seed digest and id.

    ``seed_digest`` is required and validated; ``package_id`` is required and
    is ``None`` only for a package that was never sealed (``seal_package``),
    which the ledger never records. The ledger compares both with the frozen
    event of the boundary version before it appends a receipt.
    """

    # In-memory identity of the package that ran (``CheckPackage.sha256``);
    # never dumped, so no stored receipt or journal event carries it.
    package_sha256: str = Field(exclude=True)


ExclusionReason = Literal[
    "repro_passes_on_base", "held_out_not_discriminating", "preservation_fails_on_base"
]
"""Why per-check admission excluded a check (``per_check.EXCLUSION_REASONS``)."""


class CheckStatus(StrEnum):
    """Outcome of one check against its role contract."""

    EXPECTED = "expected"
    VIOLATED = "violated"
    INDETERMINATE = "indeterminate"


class PackageVerdict(StrEnum):
    """Admission verdict on the base checkout (after per-check exclusions)."""

    ADMITTED = "admitted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class CandidateVerdict(StrEnum):
    """Verdict of the admitted checks on a candidate checkout."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class CheckExecution(_Receipt):
    """Receipt for one check command on one isolated copy."""

    check_id: str
    role: CheckRole
    argv: tuple[str, ...]
    cwd: str
    status: CheckStatus
    reason: str
    return_code: int | None
    timed_out: bool
    duration_seconds: float
    signature_seen: bool
    stdout_sha256: str
    stderr_sha256: str
    output_tail: str
    protected_digest_before: str
    protected_digest_after: str
    mutated_paths: tuple[str, ...]
    scratch_outputs: tuple[str, ...]
    undeclared_outputs: tuple[str, ...]
    # Oracle split (optional; omitted from receipts and events while unset).
    tier: CheckTier | None = None
    binding: Binding | None = None
    oracle_result: OracleResult | None = None


class AdmissionResult(PackageReceipt):
    """Admission receipt on the pinned base checkout."""

    schema_version: Literal["ouroboros.check_admission.v2"] = "ouroboros.check_admission.v2"
    base_tree_digest: str
    base_tree_digest_after: str
    verdict: PackageVerdict
    reasons: tuple[str, ...]
    protected_bytes_mutated: bool
    timeout_seconds: int
    checks: tuple[CheckExecution, ...]
    started_at: datetime
    completed_at: datetime
    interpreter: str | None = None
    interpreter_source: str | None = None
    # SHA-256 of the pinned interpreter binary (``check_env.CheckInterpreter``):
    # a resumed run checks the interpreter it resolves against it.
    interpreter_sha256: str | None = None
    # SHA-256 of the pinned binary's real path (``CheckInterpreter.realpath_sha256``).
    interpreter_realpath_sha256: str | None = None
    check_tiers: dict[str, CheckTier] | None = None
    # Per-check admission (``boundary/per_check.py``): check id to the reason
    # it was excluded while the rest of the package was admitted.
    excluded_checks: dict[str, ExclusionReason] | None = None

    def event_summary(self) -> dict[str, Any]:
        """Return the journal payload: statuses and digests, no argv or output."""
        return _journal_safe(self, AdmissionJournal)


class CandidateVerification(PackageReceipt):
    """Receipt for running the unchanged frozen package on a candidate."""

    schema_version: Literal["ouroboros.candidate_verification.v3"] = (
        "ouroboros.candidate_verification.v3"
    )
    artifact_tree_digest: str
    artifact_tree_digest_after: str
    verdict: CandidateVerdict
    reasons: tuple[str, ...]
    protected_bytes_mutated: bool
    timeout_seconds: int
    checks: tuple[CheckExecution, ...]
    started_at: datetime
    completed_at: datetime
    interpreter: str | None = None
    interpreter_source: str | None = None
    check_tiers: dict[str, CheckTier] | None = None
    bindings: dict[str, Binding] | None = None

    def event_summary(self) -> dict[str, Any]:
        """Return the journal payload: statuses and digests, no argv or output."""
        return _journal_safe(self, VerificationJournal)


# --------------------------------------------------------------------------
# The journal form of a receipt (``event_summary``): the same closed schema
# with argv, output text, the interpreter path and held-out values removed.
# ``event_summary`` validates what it writes against these models, and the
# journal gateway (``events.validate_record``) validates what replay reads, so
# a record the product could not write is refused on both paths.


class JournalCase(_Receipt):
    """One oracle case in the journal: its id, whether it was held out, pass/fail."""

    case_id: str
    held_out: bool
    passed: bool


class JournalOracleResult(_Receipt):
    """An oracle result in the journal (``oracle.journal_safe_oracle_result``)."""

    binding_source: Literal["default", "declared"]
    resolve: str
    cases: tuple[JournalCase, ...]


class JournalCheckExecution(_Receipt):
    """A ``CheckExecution`` in the journal: no argv, no output tail."""

    check_id: str
    role: CheckRole
    cwd: str
    status: CheckStatus
    reason: str
    return_code: int | None
    timed_out: bool
    duration_seconds: float
    signature_seen: bool
    stdout_sha256: str
    stderr_sha256: str
    protected_digest_before: str
    protected_digest_after: str
    mutated_paths: tuple[str, ...]
    scratch_outputs: tuple[str, ...]
    undeclared_outputs: tuple[str, ...]
    tier: CheckTier | None = None
    binding: Binding | None = None
    oracle_result: JournalOracleResult | None = None


class AdmissionJournal(_CitesPackage):
    """An ``AdmissionResult`` in the journal (``AdmissionResult.event_summary``)."""

    schema_version: Literal["ouroboros.check_admission.v2"]
    base_tree_digest: str
    base_tree_digest_after: str
    verdict: PackageVerdict
    reasons: tuple[str, ...]
    protected_bytes_mutated: bool
    timeout_seconds: int
    checks: tuple[JournalCheckExecution, ...]
    started_at: datetime
    completed_at: datetime
    interpreter_source: str | None = None
    interpreter_sha256: str | None = None
    interpreter_realpath_sha256: str | None = None
    check_tiers: dict[str, CheckTier] | None = None
    excluded_checks: dict[str, ExclusionReason] | None = None

    @model_validator(mode="after")
    def _ordered(self) -> AdmissionJournal:
        if self.completed_at < self.started_at:
            raise ValueError("a run completes after it starts")
        return self

    def check_ids(self) -> tuple[str, ...]:
        """Every check id the record names."""
        return (
            *(check.check_id for check in self.checks),
            *(self.check_tiers or {}),
            *(self.excluded_checks or {}),
        )


class VerificationJournal(_CitesPackage):
    """A ``CandidateVerification`` in the journal (``CandidateVerification.event_summary``)."""

    schema_version: Literal["ouroboros.candidate_verification.v3"]
    artifact_tree_digest: str
    artifact_tree_digest_after: str
    verdict: CandidateVerdict
    reasons: tuple[str, ...]
    protected_bytes_mutated: bool
    timeout_seconds: int
    checks: tuple[JournalCheckExecution, ...]
    started_at: datetime
    completed_at: datetime
    interpreter_source: str | None = None
    check_tiers: dict[str, CheckTier] | None = None
    bindings: dict[str, Binding] | None = None

    @model_validator(mode="after")
    def _ordered(self) -> VerificationJournal:
        if self.completed_at < self.started_at:
            raise ValueError("a run completes after it starts")
        return self

    def check_ids(self) -> tuple[str, ...]:
        """Every check id the record names."""
        return (
            *(check.check_id for check in self.checks),
            *(self.check_tiers or {}),
            *(self.bindings or {}),
        )


_JOURNAL_EXCLUDED_CHECK_FIELDS = frozenset({"argv", "output_tail"})
# Optional receipt fields: omitted when unset (callers that pass no
# interpreter keep byte-identical receipts), and the interpreter's absolute
# path stays in the stored receipt only, never in the journal.
_OPTIONAL_RECEIPT_FIELDS = (
    "interpreter",
    "interpreter_source",
    "interpreter_sha256",
    "interpreter_realpath_sha256",
    "check_tiers",
    "bindings",
    "excluded_checks",
)
_JOURNAL_EXCLUDED_RECEIPT_FIELDS = frozenset({"interpreter"})
# Fields added with the oracle split: omitted while unset, so receipts and
# events of a package without oracles keep their earlier bytes.
_OPTIONAL_CHECK_KEYS = ("tier", "binding", "oracle_result")


def _receipt_dump(receipt: BaseModel) -> dict[str, Any]:
    # Validate again before anything is persisted: a ``model_copy(update=...)``
    # skips validation, and the stored and journaled forms must be the closed
    # schema, whatever path built the receipt.
    fields = receipt.model_dump()
    fields.update(
        {
            name: getattr(receipt, name)
            for name, info in type(receipt).model_fields.items()
            if info.exclude
        }
    )
    receipt = type(receipt).model_validate(fields)
    data = receipt.model_dump(mode="json")
    for key in _OPTIONAL_RECEIPT_FIELDS:
        if data.get(key) is None:
            data.pop(key, None)
    for check in data.get("checks", ()):
        for key in _OPTIONAL_CHECK_KEYS:
            if check.get(key) is None:
                check.pop(key, None)
        if "oracle_result" in check:
            # A held-out case keeps its id and pass/fail only.
            check["oracle_result"] = redact_held_out(check["oracle_result"])
    return data


def _journal_safe(receipt: BaseModel, journal: type[_CitesPackage]) -> dict[str, Any]:
    """Dump a receipt without check argv or output text, in its closed ``journal`` form.

    The event journal is readable by other tools, so it carries statuses,
    reasons, and digests only. The complete receipt (argv, output tails) is a
    separately stored artifact, see ``write_receipt``.
    """
    data = _receipt_dump(receipt)
    for key in _JOURNAL_EXCLUDED_RECEIPT_FIELDS:
        data.pop(key, None)
    data["checks"] = [
        {key: value for key, value in check.items() if key not in _JOURNAL_EXCLUDED_CHECK_FIELDS}
        for check in data["checks"]
    ]
    for check in data["checks"]:
        if "oracle_result" in check:
            # Case pass/fail and held-out flags only: no inputs, no observations.
            check["oracle_result"] = journal_safe_oracle_result(check["oracle_result"])
    journal.model_validate(data)
    return data


def write_receipt(receipt: AdmissionResult | CandidateVerification, directory: Path) -> Path:
    """Publish a complete receipt at ``<directory>/<sha256>.json`` (``publish_exact``)."""
    data = canonical_json_bytes(_receipt_dump(receipt))
    return publish_exact(directory / f"{sha256_bytes(data)}.json", data)
