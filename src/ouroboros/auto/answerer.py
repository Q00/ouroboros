"""Conservative source-tagged auto answers for Socratic interview prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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

        if any(word in lowered for word in ("non-goal", "out of scope", "exclude", "not do")):
            return self._non_goal_answer(question)
        if any(word in lowered for word in ("test", "verify", "validation", "acceptance", "done")):
            return self._verification_answer(question)
        if any(word in lowered for word in ("runtime", "stack", "repo", "project", "framework")):
            return self._runtime_answer(question)
        if any(word in lowered for word in ("input", "output", "user", "actor")):
            return self._io_actor_answer(question)

        return self._default_answer(question, ledger)

    def apply(self, answer: AutoAnswer, ledger: SeedDraftLedger, *, question: str) -> None:
        """Apply answer updates to ``ledger``."""
        ledger.record_qa(question, answer.prefixed_text)
        for section, entry in answer.ledger_updates:
            ledger.add_entry(section, entry)

    def _non_goal_answer(self, question: str) -> AutoAnswer:  # noqa: ARG002
        value = "For auto MVP scope, cloud sync, authentication, paid services, and production deployment are non-goals unless explicitly requested."
        entry = LedgerEntry(
            key="non_goals.mvp_scope",
            value=value,
            source=LedgerSource.NON_GOAL,
            confidence=0.86,
            status=LedgerStatus.DEFAULTED,
            rationale="Conservative auto policy bounds MVP scope.",
        )
        return AutoAnswer(value, AutoAnswerSource.NON_GOAL, 0.86, [("non_goals", entry)], non_goals=[value])

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
                    value="At least one automated or command-level check proves each acceptance criterion with observable output or artifacts.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Converts vague completion into testable behavior.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.84, updates)

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


def _blocker_for(question: str) -> AutoBlocker | None:
    lowered = question.lower()
    blockers = {
        "credential": "credential or secret value required",
        "api key": "credential or API key required",
        "production": "production deployment or irreversible external action required",
        "deploy": "deployment target requires human authority",
        "delete": "destructive operation requires human authority",
        "payment": "paid service or financial decision required",
        "legal": "legal judgment required",
        "medical": "medical judgment required",
    }
    for token, reason in blockers.items():
        if token in lowered:
            return AutoBlocker(reason=reason, question=question)
    return None
