"""Verify evidence the worker cited by call number in the evidence turn.

After the worker finishes, the evidence turn shows it the controller's own
numbered record of its shell calls (``call_ledger``) and the worker answers,
per evidence field, with call numbers only. This module decides what those
numbers prove. It reads the controller's records only; the worker's command
text is never compared with the transcript.

For a cited number:

- no recorded call has it: ``FABRICATION_SUSPECTED``;
- under ``tests_passed``, the call's recorded result is a failure:
  ``FABRICATION_SUSPECTED`` (a failed call cited as passing);
- otherwise a claim that cannot be proven is withheld, with a closed reason
  (``WithheldReason``): the result is unknown, a later call recorded the same
  command and did not pass, the call ran outside the workspace or changed
  directory, none of the commands its success implies is a verification
  invocation, the controller's replay on the final artifact failed, or the
  relevance check vetoed it. A withheld claim proves nothing and is not
  fabrication;
- a ``tests_passed`` call whose every verification command runs a script no
  longer in the artifact is not replayed (``script_absent_from_artifact``, as
  #2513): it proves nothing.

A criterion passes when every required citable field has at least one proven
citation and the other evidence fields pass the existing verifier. When a
required field has none, the criterion has no evidence: the verdict is
``SCRIPT_ABSENT_FROM_ARTIFACT`` when every unproven ``tests_passed`` citation
is a not-replayed script, otherwise ``CITED_EVIDENCE_WITHHELD``. Both are
``UNAVAILABLE`` verdicts: the work is kept and reported unverified.

A model's judgement (the relevance check) enters only as a veto: it can
withhold a citation, never prove one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.call_ledger import (
    CallResult,
    RecordedCall,
    build_call_ledger,
    later_run_failed,
)
from ouroboros.orchestrator.evidence.call_ordering import (
    call_shape,
    executed_script,
    script_in_artifact,
    verification_commands,
)
from ouroboros.orchestrator.evidence.observed_runs import SCRIPT_ABSENT_FROM_ARTIFACT
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.verifier import EVIDENCE_PATH_CITATIONS, VerifierVerdict

TESTS_PASSED = "tests_passed"
COMMANDS_RUN = "commands_run"
CITABLE_FIELDS: tuple[str, ...] = (TESTS_PASSED, COMMANDS_RUN)


class WithheldReason(StrEnum):
    """Why a citation proves nothing although it is not fabrication."""

    REPLY_UNUSABLE = "reply_unusable"
    NO_CITATION = "no_citation"
    RESULT_UNKNOWN = "result_unknown"
    DEFEATED_BY_LATER_RUN = "defeated_by_later_run"
    OUTSIDE_WORKSPACE = "outside_workspace"
    CHANGES_DIRECTORY = "changes_directory"
    NOT_A_VERIFICATION_INVOCATION = "not_a_verification_invocation"
    REPLAY_FAILED = "replay_failed"
    RELEVANCE_VETO = "relevance_veto"


class FabricationReason(StrEnum):
    """Why a citation is a fabrication."""

    UNKNOWN_CALL = "unknown_call"
    FAILED_CALL_CITED_AS_PASSING = "failed_call_cited_as_passing"


@dataclass(frozen=True, slots=True)
class Citations:
    """The call numbers the worker cited, per evidence field."""

    tests_passed: tuple[int, ...] = ()
    commands_run: tuple[int, ...] = ()

    def for_field(self, field_name: str) -> tuple[int, ...]:
        return self.tests_passed if field_name == TESTS_PASSED else self.commands_run


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """The controller's replay of one cited call on the final artifact."""

    number: int
    succeeded: bool


@dataclass(frozen=True, slots=True)
class RelevanceDecision:
    """The relevance check's decision for one cited ``tests_passed`` call.

    ``relevant`` is None when the check did not decide (off, or unavailable);
    only ``False`` changes anything, and it can only withhold.
    """

    number: int
    relevant: bool | None
    status: str


@dataclass(frozen=True, slots=True)
class CitedEvidence:
    """What the evidence turn produced, attached to the record by the controller.

    Set only by the controller (``EvidenceRecord.cited``), never from the
    worker's JSON. ``citations`` is None when the reply could not be used.
    """

    citations: Citations | None
    is_error_is_exit_verdict: bool
    replays: tuple[ReplayOutcome, ...] = ()
    relevance: tuple[RelevanceDecision, ...] = ()
    reply_error: str | None = None

    def replay_for(self, number: int) -> ReplayOutcome | None:
        return next((replay for replay in self.replays if replay.number == number), None)

    def relevance_for(self, number: int) -> RelevanceDecision | None:
        return next((decision for decision in self.relevance if decision.number == number), None)


@dataclass(frozen=True, slots=True)
class CitationFindings:
    """Per-citation findings, each entry ``<field>: [<number>] <reason>``."""

    proven: dict[str, tuple[int, ...]]
    fabricated: tuple[str, ...]
    withheld: tuple[str, ...]
    not_replayed: tuple[str, ...]


def _tests_passed_finding(
    call: RecordedCall,
    *,
    ledger: Sequence[RecordedCall],
    cited: CitedEvidence,
    task_cwd: str | None,
) -> tuple[str, str]:
    """``("proven" | "fabricated" | "withheld" | "not_replayed", reason)``."""
    if call.result is CallResult.FAILED:
        return "fabricated", FabricationReason.FAILED_CALL_CITED_AS_PASSING.value
    if call.result is CallResult.UNKNOWN:
        return "withheld", WithheldReason.RESULT_UNKNOWN.value
    if later_run_failed(call, ledger):
        return "withheld", WithheldReason.DEFEATED_BY_LATER_RUN.value
    if not call.in_workspace:
        return "withheld", WithheldReason.OUTSIDE_WORKSPACE.value
    shape = call_shape(call.command)
    if shape.changes_directory:
        return "withheld", WithheldReason.CHANGES_DIRECTORY.value
    checks = verification_commands(shape)
    if not checks:
        return "withheld", WithheldReason.NOT_A_VERIFICATION_INVOCATION.value
    replay = cited.replay_for(call.number)
    if replay is not None and not replay.succeeded:
        return "withheld", WithheldReason.REPLAY_FAILED.value
    relevance = cited.relevance_for(call.number)
    if relevance is not None and relevance.relevant is False:
        return "withheld", WithheldReason.RELEVANCE_VETO.value
    if replay is None and all(
        (script := executed_script(argv)) is not None and not script_in_artifact(script, task_cwd)
        for argv in checks
    ):
        return "not_replayed", SCRIPT_ABSENT_FROM_ARTIFACT
    return "proven", ""


def evaluate_citations(
    cited: CitedEvidence,
    *,
    messages: Sequence[AgentMessage],
    fields: Sequence[str],
    task_cwd: str | None,
) -> CitationFindings:
    """Resolve every cited number against the controller's own call ledger."""
    ledger = build_call_ledger(
        messages, task_cwd=task_cwd, is_error_is_exit_verdict=cited.is_error_is_exit_verdict
    )
    by_number = {call.number: call for call in ledger}
    proven: dict[str, tuple[int, ...]] = {}
    fabricated: list[str] = []
    withheld: list[str] = []
    not_replayed: list[str] = []
    for field_name in fields:
        if cited.citations is None:
            withheld.append(f"{field_name}: {WithheldReason.REPLY_UNUSABLE.value}")
            proven[field_name] = ()
            continue
        numbers = cited.citations.for_field(field_name)
        if not numbers:
            withheld.append(f"{field_name}: {WithheldReason.NO_CITATION.value}")
        field_proven: list[int] = []
        for number in dict.fromkeys(numbers):
            call = by_number.get(number)
            if call is None:
                fabricated.append(f"{field_name}: [{number}] {FabricationReason.UNKNOWN_CALL}")
                continue
            if field_name == TESTS_PASSED:
                kind, reason = _tests_passed_finding(
                    call, ledger=ledger, cited=cited, task_cwd=task_cwd
                )
            elif call.result is CallResult.UNKNOWN:
                kind, reason = "withheld", WithheldReason.RESULT_UNKNOWN.value
            else:
                kind, reason = "proven", ""
            entry = f"{field_name}: [{number}] {reason}"
            if kind == "proven":
                field_proven.append(number)
            elif kind == "fabricated":
                fabricated.append(entry)
            elif kind == "withheld":
                withheld.append(entry)
            else:
                not_replayed.append(entry)
        proven[field_name] = tuple(field_proven)
    return CitationFindings(
        proven=proven,
        fabricated=tuple(fabricated),
        withheld=tuple(withheld),
        not_replayed=tuple(not_replayed),
    )


def verify_cited_record(
    cited: CitedEvidence,
    *,
    messages: Sequence[AgentMessage],
    required_fields: Sequence[str],
    task_cwd: str | None,
    verify_other_fields: Callable[[], VerifierVerdict],
) -> VerifierVerdict:
    """The verdict for a record whose command evidence was cited by number.

    ``verify_other_fields`` runs the existing verifier on every required
    field that is not citable (for example ``files_touched``).
    """
    fields = [field_name for field_name in CITABLE_FIELDS if field_name in required_fields]
    findings = evaluate_citations(cited, messages=messages, fields=fields, task_cwd=task_cwd)
    withheld = tuple(f"withheld: {entry}" for entry in findings.withheld)
    not_replayed = tuple(
        f"{SCRIPT_ABSENT_FROM_ARTIFACT}: {entry}" for entry in findings.not_replayed
    )
    evidence_used = tuple(
        f"call:{field_name}:{number}"
        for field_name, numbers in findings.proven.items()
        for number in numbers
    )
    if findings.fabricated:
        return VerifierVerdict(
            passed=False,
            reasons=("cited calls the transcript contradicts: " + "; ".join(findings.fabricated),),
            failure_class=FailureClass.FABRICATION_SUSPECTED.value,
            decided_by=EVIDENCE_PATH_CITATIONS,
            withheld=withheld,
        )
    other = verify_other_fields()
    if not other.passed:
        return VerifierVerdict(
            passed=False,
            reasons=other.reasons,
            failure_class=other.failure_class,
            status=other.status,
            retry_admission=other.retry_admission,
            not_replayed=(*other.not_replayed, *not_replayed),
            decided_by=EVIDENCE_PATH_CITATIONS,
            withheld=withheld,
        )
    unproven = [field_name for field_name in fields if not findings.proven.get(field_name)]
    if not unproven:
        return VerifierVerdict(
            passed=True,
            evidence_used=(*other.evidence_used, *evidence_used),
            not_replayed=(*other.not_replayed, *not_replayed),
            decided_by=EVIDENCE_PATH_CITATIONS,
            withheld=withheld,
        )
    only_absent_scripts = (
        unproven == [TESTS_PASSED]
        and bool(findings.not_replayed)
        and not any(entry.startswith(f"{TESTS_PASSED}: ") for entry in findings.withheld)
    )
    failure_class = (
        FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT
        if only_absent_scripts
        else FailureClass.CITED_EVIDENCE_WITHHELD
    )
    detail = "; ".join((*findings.withheld, *findings.not_replayed)) or "no proven citation"
    return VerifierVerdict(
        passed=False,
        reasons=(
            f"no_evidence: {failure_class.value.lower()}: no proven citation for "
            + ", ".join(unproven)
            + f" ({detail})",
        ),
        failure_class=failure_class.value,
        not_replayed=(*other.not_replayed, *not_replayed),
        decided_by=EVIDENCE_PATH_CITATIONS,
        withheld=withheld,
    )


__all__ = [
    "CITABLE_FIELDS",
    "COMMANDS_RUN",
    "TESTS_PASSED",
    "CitationFindings",
    "CitedEvidence",
    "Citations",
    "FabricationReason",
    "RelevanceDecision",
    "ReplayOutcome",
    "WithheldReason",
    "evaluate_citations",
    "verify_cited_record",
]
