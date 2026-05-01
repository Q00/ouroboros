"""Conservative source-tagged auto answers for Socratic interview prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re

from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger


class AutoAnswerSource(StrEnum):
    """Source categories for generated auto answers."""

    USER_GOAL = "user_goal"
    REPO_FACT = "repo_fact"
    EXISTING_CONVENTION = "existing_convention"
    CONSERVATIVE_DEFAULT = "conservative_default"
    ASSUMPTION = "assumption"
    NON_GOAL = "non_goal"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class AutoBlocker:
    """A hard blocker that should stop auto convergence."""

    reason: str
    question: str


@dataclass(frozen=True, slots=True)
class AutoAnswer:
    """Answer plus structured ledger updates."""

    text: str
    source: AutoAnswerSource
    confidence: float
    ledger_updates: list[tuple[str, LedgerEntry]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    blocker: AutoBlocker | None = None

    @property
    def prefixed_text(self) -> str:
        """Return the text sent back to the interview handler."""
        return f"[from-auto][{self.source.value}] {self.text}"


class AutoAnswerer:
    """Policy engine for bounded auto interview answers.

    This class is deterministic and performs no unbounded repository or network
    exploration.  Later integrations may pass bounded repo facts into it.
    """

    def answer(self, question: str, ledger: SeedDraftLedger) -> AutoAnswer:
        """Answer ``question`` using a conservative policy."""
        lowered = question.lower()
        blocker = _blocker_for(question)
        if blocker is not None:
            return AutoAnswer(
                text=f"Cannot safely decide automatically: {blocker.reason}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=blocker,
            )

        if _matches_any(
            lowered, (r"\bnon-goals?\b", r"\bout of scope\b", r"\bexclude\b", r"\bnot do\b")
        ):
            return self._non_goal_answer(question, ledger)
        if _is_feature_acceptance_question(lowered):
            return self._feature_acceptance_answer(question)
        if _matches_any(
            lowered,
            (
                r"\btests?\b",
                r"\bverify\b",
                r"\bvalidation\b",
                r"\bacceptance\b",
                r"\bdefinition of done\b",
            ),
        ):
            return self._verification_answer(question)
        if _matches_any(
            lowered,
            (
                r"\bruntime\b",
                r"\bstack\b",
                r"\brepo\b",
                r"\bframework\b",
                r"\bproject structure\b",
                r"\bproject runtime\b",
            ),
        ):
            return self._runtime_answer(question)
        if _is_actor_or_io_question(lowered):
            return self._io_actor_answer(question)
        if _is_product_behavior_question(lowered):
            return self._product_behavior_answer(question)

        return self._default_answer(question, ledger)

    def apply(self, answer: AutoAnswer, ledger: SeedDraftLedger, *, question: str) -> None:
        """Apply answer updates to ``ledger``."""
        ledger.record_qa(question, answer.prefixed_text)
        if answer.blocker is not None:
            ledger.add_entry(
                "constraints",
                LedgerEntry(
                    key="blocker.auto_answer",
                    value=answer.blocker.reason,
                    source=LedgerSource.BLOCKER,
                    confidence=1.0,
                    status=LedgerStatus.BLOCKED,
                    reversible=False,
                    rationale=f"Auto mode cannot safely answer: {answer.blocker.question}",
                ),
            )
        for section, entry in answer.ledger_updates:
            ledger.add_entry(section, entry)

    def _non_goal_answer(self, question: str, ledger: SeedDraftLedger) -> AutoAnswer:  # noqa: ARG002
        goal_text = _latest_resolved_goal(ledger).lower()
        excluded = ["cloud sync", "paid services"]
        if not re.search(r"\b(auth|authentication|login|sign[- ]?in|signup|password)\b", goal_text):
            excluded.append("authentication")
        if not re.search(r"\b(production|prod|deploy|deployment|release|publish)\b", goal_text):
            excluded.append("production deployment")
        value = (
            f"For auto MVP scope, {', '.join(excluded)} are non-goals unless explicitly requested."
        )
        entry = LedgerEntry(
            key="non_goals.mvp_scope",
            value=value,
            source=LedgerSource.NON_GOAL,
            confidence=0.86,
            status=LedgerStatus.DEFAULTED,
            rationale="Conservative auto policy bounds MVP scope.",
        )
        return AutoAnswer(
            value, AutoAnswerSource.NON_GOAL, 0.86, [("non_goals", entry)], non_goals=[value]
        )

    def _verification_answer(self, question: str) -> AutoAnswer:  # noqa: ARG002
        value = "Success must be verified with observable behavior: commands or tests should produce stable output, non-zero failures for invalid input, and reproducible artifacts where applicable."
        updates = [
            (
                "verification_plan",
                LedgerEntry(
                    key="verification.observable",
                    value=value,
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.84,
                    status=LedgerStatus.DEFAULTED,
                    rationale="A-grade Seeds require testable acceptance criteria.",
                ),
            ),
            (
                "acceptance_criteria",
                LedgerEntry(
                    key="acceptance.observable_behavior",
                    value="A command-level check returns exit code 0 and stdout contains stable output or writes a reproducible artifact for each acceptance criterion.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Converts vague completion into testable behavior.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.84, updates)

    def _feature_acceptance_answer(self, question: str) -> AutoAnswer:
        subject = _acceptance_subject(question)
        value = (
            f"Acceptance for {subject} must cover the requested behavior directly: "
            "a successful operation returns an observable status/output, invalid input fails "
            "with a non-zero/error status, and any persisted artifact or state change can be verified."
        )
        updates = [
            (
                "acceptance_criteria",
                LedgerEntry(
                    key=f"acceptance.{_slug_key(subject)}",
                    value=value,
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Preserves feature-specific acceptance semantics from the interview question.",
                ),
            ),
            (
                "verification_plan",
                LedgerEntry(
                    key=f"verification.{_slug_key(subject)}",
                    value=f"Verify {subject} with command/API checks for success, failure, and persisted state or output.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Feature-specific acceptance requires observable verification.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.82, updates)

    def _runtime_answer(self, question: str) -> AutoAnswer:  # noqa: ARG002
        value = "Use the existing repository runtime, package manager, and architectural patterns; avoid new dependencies unless required by acceptance criteria."
        updates = [
            (
                "runtime_context",
                LedgerEntry(
                    key="runtime.existing_project",
                    value=value,
                    source=LedgerSource.EXISTING_CONVENTION,
                    confidence=0.78,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Auto mode should avoid unnecessary stack choices.",
                ),
            ),
            (
                "constraints",
                LedgerEntry(
                    key="constraints.no_unnecessary_dependencies",
                    value="Do not add new dependencies unless they are necessary to satisfy explicit acceptance criteria.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.86,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Reduces execution risk and review scope.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.EXISTING_CONVENTION, 0.78, updates)

    def _io_actor_answer(self, question: str) -> AutoAnswer:  # noqa: ARG002
        value = "Assume a single local user operating through the requested interface; inputs and outputs should be explicit command/API arguments and stable returned text or artifacts."
        updates = [
            (
                "actors",
                LedgerEntry(
                    key="actors.single_local_user",
                    value="Single local user",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.76,
                    status=LedgerStatus.DEFAULTED,
                    rationale="No multi-user requirement was provided.",
                ),
            ),
            (
                "inputs",
                LedgerEntry(
                    key="inputs.explicit_arguments",
                    value="Explicit command/API arguments derived from the task goal",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.74,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Auto mode needs concrete IO to generate testable Seeds.",
                ),
            ),
            (
                "outputs",
                LedgerEntry(
                    key="outputs.stable_text_or_artifacts",
                    value="Stable text output or generated artifacts suitable for verification",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.74,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Outputs must be observable for A-grade testability.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.ASSUMPTION, 0.76, updates, assumptions=[value])

    def _product_behavior_answer(self, question: str) -> AutoAnswer:
        subject = _acceptance_subject(question)
        value = (
            f"Treat this requested product behavior as in scope for the MVP: {subject}. "
            "Implement it directly and make the resulting state, output, or API response observable."
        )
        key = _slug_key(subject)
        updates = [
            (
                "constraints",
                LedgerEntry(
                    key=f"constraints.behavior.{key}",
                    value=f"Preserve the product behavior requested by the interview question: {subject}",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Safe product-semantics questions should not be collapsed into a generic MVP policy.",
                ),
            ),
            (
                "acceptance_criteria",
                LedgerEntry(
                    key=f"acceptance.behavior.{key}",
                    value=f"The requested behavior is observable: {subject} produces a verifiable output, status, or persisted state change.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.78,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Feature semantics from the interview question must remain visible in the Seed contract.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.8, updates)

    def _default_answer(self, question: str, ledger: SeedDraftLedger) -> AutoAnswer:  # noqa: ARG002
        value = "Proceed with a conservative MVP: keep scope small, prefer existing project patterns, document assumptions, and make completion verifiable with observable acceptance criteria."
        updates = [
            (
                "constraints",
                LedgerEntry(
                    key="constraints.conservative_mvp",
                    value="Keep the implementation to the smallest safe MVP that satisfies the task goal.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Default auto policy favors safe convergence.",
                ),
            ),
            (
                "failure_modes",
                LedgerEntry(
                    key="failure_modes.unverified_or_scope_creep",
                    value="Failure includes unverified behavior, non-reproducible output, or scope expansion beyond the MVP.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                    rationale="A-grade Seeds need explicit failure boundaries.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.82, updates)


def _is_feature_acceptance_question(lowered: str) -> bool:
    if not re.search(r"\b(acceptance|criteria)\b", lowered):
        return False
    if re.search(
        r"\b(general|overall|test strategy|verification plan|definition of done)\b", lowered
    ):
        return False
    return bool(
        re.search(
            r"\b(for|when|where|should|must|feature|flow|integration|endpoint|api|command|report|webhook|billing|search|generator|users?|user)\b",
            lowered,
        )
    )


def _acceptance_subject(question: str) -> str:
    cleaned = re.sub(r"\s+", " ", question.strip().rstrip("?"))
    patterns = (
        r"acceptance criteria should (?P<subject>.+?) satisfy$",
        r"criteria should (?P<subject>.+?) satisfy$",
        r"should (?P<subject>.+?) do$",
        r"for (?P<subject>.+)$",
    )
    lowered = cleaned.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return match.group("subject").strip() or "the requested behavior"
    return cleaned or "the requested behavior"


def _slug_key(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:64] or "requested_behavior"


def _is_product_behavior_question(lowered: str) -> bool:
    return bool(
        re.search(
            r"\b(should|must|can|will|do|does|is|are)\b.+\b(mark|marked|show|display|write|return|create|update|edit|delete|store|save|send|generate|filter|sort|search|export|import|notify|report|use)\b",
            lowered,
        )
        or re.search(r"\bwhat\s+(output|input)\b.+\b(should|does|do|format|write|use)\b", lowered)
        or re.search(
            r"\bwhat\s+should\b.+\b(write|return|display|show|create|store|generate|edit|delete)\b",
            lowered,
        )
        or re.search(
            r"\bhow\s+should\b.+\b(behave|work|display|return|write|store|mark)\b", lowered
        )
        or re.search(
            r"\b(which|what)\b.+\b(can|should)\b.+\b(edit|delete|update|create|view|access)\b",
            lowered,
        )
    )


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def _is_actor_or_io_question(lowered: str) -> bool:
    if re.search(
        r"\b(what|which)\s+(are|inputs? are|outputs? are)\s+.+\b(inputs|outputs)\b", lowered
    ):
        return True
    if re.search(r"\b(what|which)\s+(inputs|outputs)\s+(are|should be)\b", lowered):
        return True
    if re.search(
        r"\b(who|which|what)\s+(is|are)\s+.+\b(actors?|personas?|stakeholders?)\b", lowered
    ):
        return True
    return bool(re.search(r"\b(who|which)\s+(is|are)\s+the\s+users?\b", lowered))


def _latest_resolved_goal(ledger: SeedDraftLedger) -> str:
    section = ledger.sections.get("goal")
    if section is None:
        return ""
    inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    for entry in reversed(section.entries):
        if entry.status not in inactive and entry.value.strip():
            return entry.value
    return ""


def _blocker_for(question: str) -> AutoBlocker | None:
    lowered = question.lower()

    external_action_patterns = (
        (
            r"\b(access token|auth token|private key|credential value|credential secret)\b",
            "credential or secret value required",
        ),
        (
            r"\b(provide|enter|use|choose|select|configure|set)\b.+\b(credential|credentials)\b",
            "credential or secret value required",
        ),
        (
            r"\b(provide|enter|use|configure|set)\b.+\b(api keys?|passwords?)\b.+\b(value|secret|token|credential|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(which|what)\b.+\b(api keys?|passwords?)\b.+\b(value|secret|token|credential|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(credential|credentials)\b.+\b(value|secret|token|key|password|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(charge|purchase|subscribe|provide|enter|use|configure|set)\b.+\b(payment|billing|paid service|credit card|bank account|invoice)\b.+\b(account|provider|key|secret|production|live)\b",
            "paid service or financial decision required",
        ),
        (
            r"\b(payment|billing|paid service|credit card|bank account|invoice)\b.+\b(account|provider|key|secret|production|live)\b.+\b(charge|purchase|subscribe|pay)\b",
            "paid service or financial decision required",
        ),
        (
            r"\b(which|what|provide|obtain|get|use|choose|select)\b.+\b(legal|compliance|license|contract)\b.+\b(advice|judgment|review|approval|liability|risk|interpretation)\b",
            "legal judgment required",
        ),
        (
            r"\b(which|what|provide|use|choose|select)\b.+\b(medical|clinical|diagnosis|treatment|health)\b.+\b(advice|judgment|diagnose|prescribe|triage|recommendation)\b",
            "medical judgment required",
        ),
        (
            r"\b(should|can|may|will|do we|should we)\b.+\b(deploy|release|publish)\b.+\b(to|against|on)\s+\b(production|prod|live|external)\b",
            "deployment target requires human authority",
        ),
        (
            r"\b(which|what|choose|select|use|configure|set)\b.+\b(production|prod|live|external)\b.+\b(environment|target|account|project|cluster|region)\b",
            "deployment target requires human authority",
        ),
        (
            r"\b(which|what|choose|select|use|configure|set)\b.+\b(environment|target|account|project|cluster|region)\b.+\b(deploy|release|publish)\b.+\b(production|prod|live|external)\b",
            "deployment target requires human authority",
        ),
        (
            r"\b(production|prod|live|external)\b.+\b(credential|secret|api key)\b",
            "production deployment or irreversible external action required",
        ),
        (
            r"\b(delete|drop|erase|wipe|remove)\b.+\b(database|db|repo|branch|production|prod|secret|api key|credential)\b",
            "destructive external operation requires human authority",
        ),
        (
            r"\b(provide|enter|paste|supply|use|configure|set)\b.+\bsecret\b.+\b(value|key|token|credential|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(which|what)\s+secret\b.+\b(use|configure|set|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
    )
    for pattern, reason in external_action_patterns:
        if re.search(pattern, lowered):
            return AutoBlocker(reason=reason, question=question)
    return None
