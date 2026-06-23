"""Pre-Seed intent provenance checks for ``ooo auto`` interviews.

TraceGuard verifies runtime acceptance evidence. IntentGuard runs earlier: it
checks whether generated interview framing is still subordinate to the original
user contract before the answer is applied to the Seed draft ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any

from ouroboros.auto.ledger import (
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)


class IntentGuardStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class IntentGuardCheck:
    code: str
    status: IntentGuardStatus
    message: str
    action: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {
            "code": self.code,
            "status": self.status.value,
            "message": self.message,
        }
        if self.action:
            data["action"] = self.action
        return data


@dataclass(frozen=True, slots=True)
class IntentGuardReport:
    checks: tuple[IntentGuardCheck, ...]

    @property
    def status(self) -> IntentGuardStatus:
        if any(check.status is IntentGuardStatus.FAIL for check in self.checks):
            return IntentGuardStatus.FAIL
        if any(check.status is IntentGuardStatus.WARN for check in self.checks):
            return IntentGuardStatus.WARN
        return IntentGuardStatus.PASS

    @property
    def failed(self) -> bool:
        return self.status is IntentGuardStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checks": [check.to_dict() for check in self.checks],
        }

    def render_lines(self) -> list[str]:
        lines = [f"IntentGuard: {self.status.value}"]
        for check in self.checks:
            line = f"  {check.status.value.upper()} {check.code}: {check.message}"
            if check.action:
                line = f"{line} Action: {check.action}"
            lines.append(line)
        return lines


_OUTPUT_SECTIONS = ("outputs", "acceptance_criteria")
_EVIDENCE_SOURCES = {
    LedgerSource.USER_GOAL,
    LedgerSource.USER_PREFERENCE,
    LedgerSource.REPO_FACT,
    LedgerSource.EXISTING_CONVENTION,
}
_ACTIVE_STATUSES = {
    LedgerStatus.CONFIRMED,
    LedgerStatus.DEFAULTED,
    LedgerStatus.INFERRED,
}

_ARTIFACT_OUTPUT_TERMS = (
    "artifact",
    "artifacts",
    "file",
    "files",
    "output",
    "outputs",
    "export",
    "exports",
    "video",
    "videos",
    "mp4",
    "clip",
    "clips",
    "short",
    "shorts",
    "long-form",
    "long form",
    "transcript",
    "transcripts",
    "json",
    "csv",
    "pdf",
    "html",
    "report",
    "reports",
    "cli",
    "command",
    "commands",
    "web",
    "webapp",
    "web app",
    "app",
    "application",
    "tool",
    "tools",
)

_ARTIFACT_GENERATION_TERMS = (
    "make",
    "makes",
    "create",
    "creates",
    "generate",
    "generates",
    "produce",
    "produces",
    "export",
    "exports",
    "write",
    "writes",
)

_REVIEW_ONLY_TERMS = (
    "review-only",
    "review only",
    "review package",
    "--mode review",
    "reject automated export",
    "rejects automated export",
    "without automated export",
    "no automated export",
    "no mp4",
    "no export",
)

_SCOPE_REDUCTION_TERMS = (
    "docs-only",
    "docs only",
    "documentation-only",
    "documentation only",
    "document-only",
    "document only",
    "handoff-only",
    "handoff only",
    "handoff package",
    "checkpack",
    "check pack",
    "checklist-only",
    "checklist only",
    "checklist package",
)

_DIAGNOSTIC_MARKERS = (
    "[seed qa repair attempt",
    "[seed qa lateral repair attempt",
    "# lateral thinking:",
    "qa differences:",
    "qa suggestions:",
)

_HUMAN_ANSWER_SOURCES = {
    "human",
    "user",
    "user_answer",
    "user_response",
}

_EVIDENCE_ANSWER_SOURCES = {
    "user_goal",
    "user_preference",
    "repo_fact",
    "existing_convention",
    *_HUMAN_ANSWER_SOURCES,
}


def guard_auto_answer(
    *,
    goal: str,
    user_preferences: Mapping[str, str],
    ledger: SeedDraftLedger,
    question: str,
    answer_text: str,
    answer_source: str,
) -> IntentGuardReport:
    """Check a proposed auto-interview answer before it mutates the ledger."""
    checks: list[IntentGuardCheck] = []
    contract = _artifact_contract(goal, user_preferences, ledger)
    if contract is None:
        if _looks_like_artifact_generation(goal):
            checks.append(
                IntentGuardCheck(
                    code="intent_lock_missing",
                    status=IntentGuardStatus.WARN,
                    message="goal looks artifact-producing, but no evidence-backed output contract is locked",
                    action="derive outputs/acceptance_criteria from the user goal or ask one more question",
                )
            )
        return IntentGuardReport(tuple(checks))

    checks.append(
        IntentGuardCheck(
            code="intent_lock_present",
            status=IntentGuardStatus.PASS,
            message="evidence-backed output contract is locked from user intent",
        )
    )
    conflict = _generated_review_option_conflicts(
        contract_text=contract,
        question=question,
        answer_text=answer_text,
        answer_source=answer_source,
    )
    if conflict:
        checks.append(
            IntentGuardCheck(
                code="generated_option_conflict",
                status=IntentGuardStatus.FAIL,
                message="generated narrowed-output framing conflicts with the user output contract",
                action="discard the generated option and answer from USER_GOAL/USER_PREFERENCE; block if retry still conflicts",
            )
        )
    return IntentGuardReport(tuple(checks))


def guard_interview_turn(
    *,
    goal: str,
    question: str,
    answer_text: str,
    answer_source: str,
) -> IntentGuardReport:
    """Check one interactive interview turn using the shared intent contract.

    Human answers may intentionally change the contract, so conflicting human
    responses produce WARN instead of FAIL. Generated or assumption-class
    answers still fail closed, matching ``ooo auto``.
    """
    ledger = SeedDraftLedger.from_goal(goal)
    checks: list[IntentGuardCheck] = []
    contract = _artifact_contract(goal, {}, ledger)
    if contract is None:
        if _looks_like_artifact_generation(goal):
            checks.append(
                IntentGuardCheck(
                    code="intent_lock_missing",
                    status=IntentGuardStatus.WARN,
                    message="goal looks artifact-producing, but no output contract is locked",
                    action="ask one clarifying question before accepting generated product-behavior framing",
                )
            )
        return IntentGuardReport(tuple(checks))

    checks.append(
        IntentGuardCheck(
            code="intent_lock_present",
            status=IntentGuardStatus.PASS,
            message="initial interview context contains an artifact-output contract",
        )
    )
    question_has_generated_choice = _contains_scope_reduction_option(question) or (
        "--mode auto" in question.casefold() and "--mode review" in question.casefold()
    )
    answer_has_review_only = _contains_scope_reduction_option(answer_text) or (
        "review" in answer_text.casefold() and not _has_artifact_output_terms(answer_text)
    )

    if not question_has_generated_choice or not answer_has_review_only:
        return IntentGuardReport(tuple(checks))

    source = answer_source.strip().casefold()
    if source in _HUMAN_ANSWER_SOURCES:
        checks.append(
            IntentGuardCheck(
                code="user_contract_change",
                status=IntentGuardStatus.WARN,
                message="human answer appears to change the initial output contract toward a narrower output class",
                action="record it, but ask/confirm whether this is an intentional scope change before Seed generation",
            )
        )
    elif source not in _EVIDENCE_ANSWER_SOURCES:
        checks.append(
            IntentGuardCheck(
                code="generated_option_conflict",
                status=IntentGuardStatus.FAIL,
                message="generated interview answer conflicts with the initial user output contract",
                action="do not record this answer; ask the human to confirm the product behavior",
            )
        )
    return IntentGuardReport(tuple(checks))


def diagnose_auto_state(
    *,
    goal: str,
    user_preferences: Mapping[str, str],
    ledger: SeedDraftLedger | None,
    pending_question: str | None,
    auto_answer_log: Sequence[Mapping[str, Any]],
    seed_artifact: Mapping[str, Any] | None = None,
) -> IntentGuardReport:
    """Return a status/doctor projection for a persisted auto session."""
    checks: list[IntentGuardCheck] = []
    ledger = ledger or SeedDraftLedger.from_goal(goal)
    contract = _artifact_contract(goal, user_preferences, ledger)
    if contract is None:
        if _looks_like_artifact_generation(goal):
            checks.append(
                IntentGuardCheck(
                    code="intent_lock_missing",
                    status=IntentGuardStatus.WARN,
                    message="artifact-producing goal has no locked outputs/acceptance_criteria preference",
                    action="re-derive the output contract from the original user goal before answering product-behavior questions",
                )
            )
        else:
            checks.append(
                IntentGuardCheck(
                    code="intent_contract_not_applicable",
                    status=IntentGuardStatus.PASS,
                    message="goal does not look like an artifact-output request",
                )
            )
    else:
        checks.append(
            IntentGuardCheck(
                code="intent_lock_present",
                status=IntentGuardStatus.PASS,
                message="output contract is anchored in user/repo evidence",
            )
        )
        if pending_question and _contains_scope_reduction_option(pending_question):
            checks.append(
                IntentGuardCheck(
                    code="generated_option_present",
                    status=IntentGuardStatus.WARN,
                    message="pending question contains a generated narrowed-output option next to a user output contract",
                    action="prefer USER_GOAL/USER_PREFERENCE when answering; do not let the generated option win",
                )
            )
        for entry in auto_answer_log[-5:]:
            question = str(entry.get("question", ""))
            answer = str(entry.get("answer", ""))
            source = str(entry.get("source", ""))
            if _generated_review_option_conflicts(
                contract_text=contract,
                question=question,
                answer_text=answer,
                answer_source=source,
            ):
                checks.append(
                    IntentGuardCheck(
                        code="generated_option_conflict",
                        status=IntentGuardStatus.FAIL,
                        message="recent auto answer appears to have accepted review-only framing over user output intent",
                        action="discard that answer, reopen the round, and re-answer from USER_GOAL/USER_PREFERENCE",
                    )
                )
                break

    if _seed_artifact_has_diagnostic_constraints(seed_artifact or {}):
        checks.append(
            IntentGuardCheck(
                code="spec_pollution",
                status=IntentGuardStatus.FAIL,
                message="Seed constraints contain pasted QA/lateral diagnostic prose",
                action="normalize diagnostics into short repair constraints before Seed review",
            )
        )
    else:
        checks.append(
            IntentGuardCheck(
                code="spec_pollution",
                status=IntentGuardStatus.PASS,
                message="Seed constraints do not contain known diagnostic markers",
            )
        )
    return IntentGuardReport(tuple(checks))


def diagnose_auto_pipeline_state(state: Any) -> IntentGuardReport:
    """Build an IntentGuard report from a persisted ``AutoPipelineState``-like object."""
    ledger = None
    ledger_data = getattr(state, "ledger", None)
    if ledger_data:
        try:
            ledger = SeedDraftLedger.from_dict(ledger_data)
        except ValueError:
            ledger = None
    return diagnose_auto_state(
        goal=str(getattr(state, "goal", "")),
        user_preferences=getattr(state, "user_preferences", {}) or {},
        ledger=ledger,
        pending_question=getattr(state, "pending_question", None),
        auto_answer_log=getattr(state, "auto_answer_log", ()) or (),
        seed_artifact=getattr(state, "seed_artifact", {}) or {},
    )


def _artifact_contract(
    goal: str,
    user_preferences: Mapping[str, str],
    ledger: SeedDraftLedger,
) -> str | None:
    parts: list[str] = []
    for section in _OUTPUT_SECTIONS:
        value = user_preferences.get(section)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for section_name in _OUTPUT_SECTIONS:
        section = ledger.sections.get(section_name)
        if section is None:
            continue
        for entry in section.entries:
            if entry.source in _EVIDENCE_SOURCES and entry.status in _ACTIVE_STATUSES:
                parts.append(entry.value.strip())
    if not parts and _looks_like_artifact_generation(goal):
        return goal.strip()
    text = "\n".join(part for part in parts if part)
    if not _has_artifact_output_terms(text):
        return None
    return text


def _generated_review_option_conflicts(
    *,
    contract_text: str,
    question: str,
    answer_text: str,
    answer_source: str,
) -> bool:
    if not _has_artifact_output_terms(contract_text):
        return False
    source = answer_source.strip().casefold()
    if source in {"user_goal", "user_preference", "repo_fact", "existing_convention"}:
        return False
    question_has_generated_choice = _contains_scope_reduction_option(question) or (
        "--mode auto" in question.casefold() and "--mode review" in question.casefold()
    )
    if not question_has_generated_choice:
        return False
    answer_lower = answer_text.casefold()
    if _contains_scope_reduction_option(answer_lower):
        return True
    return "review" in answer_lower and not _has_artifact_output_terms(answer_lower)


def _contains_review_only_option(text: str) -> bool:
    return _contains_any_phrase(text, _REVIEW_ONLY_TERMS)


def _contains_scope_reduction_option(text: str) -> bool:
    return _contains_any_phrase(text, (*_REVIEW_ONLY_TERMS, *_SCOPE_REDUCTION_TERMS))


def _looks_like_artifact_generation(text: str) -> bool:
    return (
        bool(text.strip())
        and _contains_any_phrase(text, _ARTIFACT_GENERATION_TERMS)
        and _has_artifact_output_terms(text)
    )


def _has_artifact_output_terms(text: str) -> bool:
    return _contains_any_phrase(text, _ARTIFACT_OUTPUT_TERMS)


def _contains_any_phrase(text: str, phrases: Sequence[str]) -> bool:
    lowered = text.casefold()
    return any(_contains_phrase(lowered, phrase.casefold()) for phrase in phrases)


def _contains_phrase(lowered_text: str, phrase: str) -> bool:
    """Return true when ``phrase`` appears as a token/phrase, not a substring.

    IntentGuard uses short artifact terms such as ``app`` and ``web``. Raw
    substring matching would treat prose like ``approach`` or ``webbing`` as an
    artifact contract, so alphanumeric phrase edges must align to non-word
    boundaries. Phrases that start or end with punctuation keep substring
    semantics for CLI flags such as ``--mode review``.
    """
    escaped = re.escape(phrase)
    prefix = r"(?<!\w)" if phrase[:1].isalnum() else ""
    suffix = r"(?!\w)" if phrase[-1:].isalnum() else ""
    return re.search(f"{prefix}{escaped}{suffix}", lowered_text) is not None


def _seed_artifact_has_diagnostic_constraints(seed_artifact: Mapping[str, Any]) -> bool:
    constraints = seed_artifact.get("constraints")
    if constraints is None:
        return False
    if isinstance(constraints, str):
        text = constraints
    elif isinstance(constraints, Sequence):
        text = "\n".join(str(item) for item in constraints)
    else:
        text = str(constraints)
    lowered = text.casefold()
    return any(marker in lowered for marker in _DIAGNOSTIC_MARKERS)
