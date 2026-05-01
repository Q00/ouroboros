"""Seed Draft Ledger for bounded auto-mode convergence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class LedgerSource(StrEnum):
    """Source categories for ledger entries."""

    USER_GOAL = "user_goal"
    REPO_FACT = "repo_fact"
    EXISTING_CONVENTION = "existing_convention"
    CONSERVATIVE_DEFAULT = "conservative_default"
    ASSUMPTION = "assumption"
    NON_GOAL = "non_goal"
    INFERENCE = "inference"


class LedgerStatus(StrEnum):
    """Status of a ledger entry or section."""

    MISSING = "missing"
    WEAK = "weak"
    DEFAULTED = "defaulted"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    CONFLICTING = "conflicting"
    BLOCKED = "blocked"


REQUIRED_SECTIONS = (
    "goal",
    "actors",
    "inputs",
    "outputs",
    "constraints",
    "non_goals",
    "acceptance_criteria",
    "verification_plan",
    "failure_modes",
    "runtime_context",
)


@dataclass(slots=True)
class LedgerEntry:
    """A single machine-readable fact in the Seed Draft Ledger."""

    key: str
    value: str
    source: LedgerSource | str
    confidence: float
    status: LedgerStatus | str
    reversible: bool = True
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.source = LedgerSource(str(self.source))
        self.status = LedgerStatus(str(self.status))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible data."""
        data = asdict(self)
        data["source"] = self.source.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        """Deserialize from JSON-compatible data."""
        return cls(**data)


@dataclass(slots=True)
class LedgerSection:
    """A Seed section containing one or more ledger entries."""

    name: str
    entries: list[LedgerEntry] = field(default_factory=list)

    def status(self) -> LedgerStatus:
        """Return the aggregate status for this section."""
        if not self.entries:
            return LedgerStatus.MISSING
        statuses = {entry.status for entry in self.entries}
        if LedgerStatus.BLOCKED in statuses:
            return LedgerStatus.BLOCKED
        if LedgerStatus.CONFLICTING in statuses:
            return LedgerStatus.CONFLICTING
        if LedgerStatus.CONFIRMED in statuses:
            return LedgerStatus.CONFIRMED
        if LedgerStatus.DEFAULTED in statuses:
            return LedgerStatus.DEFAULTED
        if LedgerStatus.INFERRED in statuses:
            return LedgerStatus.INFERRED
        return LedgerStatus.WEAK

    def to_dict(self) -> dict[str, Any]:
        """Serialize section data."""
        return {"name": self.name, "entries": [entry.to_dict() for entry in self.entries]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerSection:
        """Deserialize section data."""
        entries = [LedgerEntry.from_dict(item) for item in data.get("entries", [])]
        return cls(name=str(data["name"]), entries=entries)


@dataclass(slots=True)
class SeedDraftLedger:
    """Structured auto-mode Seed draft.

    The ledger performs no external IO or model calls.  It is safe to mutate in
    tight loops without risking hangs.
    """

    sections: dict[str, LedgerSection] = field(default_factory=dict)
    question_history: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_goal(cls, goal: str) -> SeedDraftLedger:
        """Create a ledger initialized with a user goal."""
        ledger = cls(sections={name: LedgerSection(name) for name in REQUIRED_SECTIONS})
        ledger.add_entry(
            "goal",
            LedgerEntry(
                key="goal.primary",
                value=goal,
                source=LedgerSource.USER_GOAL,
                confidence=0.95,
                status=LedgerStatus.CONFIRMED,
                reversible=False,
                rationale="Initial user-provided auto task.",
            ),
        )
        return ledger

    def add_entry(self, section_name: str, entry: LedgerEntry) -> None:
        """Append ``entry`` to a section, creating the section when needed."""
        section = self.sections.setdefault(section_name, LedgerSection(section_name))
        section.entries.append(entry)

    def record_qa(self, question: str, answer: str) -> None:
        """Record an interview Q/A pair in bounded form."""
        self.question_history.append(
            {
                "question": _truncate(question),
                "answer": _truncate(answer),
            }
        )

    def section_statuses(self) -> dict[str, LedgerStatus]:
        """Return aggregate statuses for required sections."""
        return {
            name: self.sections.get(name, LedgerSection(name)).status()
            for name in REQUIRED_SECTIONS
        }

    def open_gaps(self) -> list[str]:
        """Return required sections that are not Seed-ready."""
        blocked = {LedgerStatus.MISSING, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
        return [name for name, status in self.section_statuses().items() if status in blocked]

    def is_seed_ready(self) -> bool:
        """Return True when no required section is missing/conflicting/blocked."""
        return not self.open_gaps()

    def assumptions(self) -> list[str]:
        """Return assumption entry values."""
        return self._values_for_sources({LedgerSource.ASSUMPTION})

    def non_goals(self) -> list[str]:
        """Return non-goal entry values."""
        return self._values_for_sources({LedgerSource.NON_GOAL})

    def summary(self) -> dict[str, Any]:
        """Return a bounded summary suitable for CLI/MCP output."""
        statuses = self.section_statuses()
        return {
            "complete_sections": [
                name
                for name, status in statuses.items()
                if status in {LedgerStatus.CONFIRMED, LedgerStatus.DEFAULTED, LedgerStatus.INFERRED}
            ],
            "weak_sections": [
                name for name, status in statuses.items() if status == LedgerStatus.WEAK
            ],
            "defaulted_sections": [
                name for name, status in statuses.items() if status == LedgerStatus.DEFAULTED
            ],
            "assumptions": [_truncate(value) for value in self.assumptions()],
            "non_goals": [_truncate(value) for value in self.non_goals()],
            "open_gaps": self.open_gaps(),
            "risks": [
                _truncate(entry.value)
                for section in self.sections.values()
                for entry in section.entries
                if entry.key.startswith("risk.")
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the ledger."""
        return {
            "sections": {name: section.to_dict() for name, section in self.sections.items()},
            "question_history": list(self.question_history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeedDraftLedger:
        """Deserialize the ledger."""
        sections_raw = data.get("sections", {})
        sections = {
            name: LedgerSection.from_dict(section)
            for name, section in sections_raw.items()
            if isinstance(section, dict)
        }
        ledger = cls(sections=sections, question_history=list(data.get("question_history", [])))
        for required in REQUIRED_SECTIONS:
            ledger.sections.setdefault(required, LedgerSection(required))
        return ledger

    def _values_for_sources(self, sources: set[LedgerSource]) -> list[str]:
        return [
            entry.value
            for section in self.sections.values()
            for entry in section.entries
            if entry.source in sources
        ]


def _truncate(value: str, *, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 15].rstrip() + " ... (truncated)"
