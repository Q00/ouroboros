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
    BLOCKER = "blocker"


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
        if not isinstance(data, dict):
            msg = "ledger entry must be an object"
            raise ValueError(msg)
        required = {"key", "value", "source", "confidence", "status"}
        missing = sorted(required - data.keys())
        if missing:
            msg = f"ledger entry is missing required fields: {', '.join(missing)}"
            raise ValueError(msg)
        if not isinstance(data["key"], str) or not data["key"].strip():
            msg = "ledger entry key must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(data["value"], str):
            msg = "ledger entry value must be a string"
            raise ValueError(msg)
        if not isinstance(data.get("rationale", ""), str):
            msg = "ledger entry rationale must be a string"
            raise ValueError(msg)
        evidence = data.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            msg = "ledger entry evidence must be a list of strings"
            raise ValueError(msg)
        if "reversible" in data and not isinstance(data["reversible"], bool):
            msg = "ledger entry reversible must be a boolean"
            raise ValueError(msg)
        try:
            confidence = float(data["confidence"])
        except (TypeError, ValueError) as exc:
            msg = "ledger entry confidence must be numeric"
            raise ValueError(msg) from exc
        if confidence < 0.0 or confidence > 1.0:
            msg = "ledger entry confidence must be between 0 and 1"
            raise ValueError(msg)
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
        if not isinstance(data, dict):
            msg = "ledger section must be an object"
            raise ValueError(msg)
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            msg = "ledger section name must be a non-empty string"
            raise ValueError(msg)
        entries_raw = data.get("entries")
        if not isinstance(entries_raw, list):
            msg = "ledger section entries must be a list"
            raise ValueError(msg)
        entries = [LedgerEntry.from_dict(item) for item in entries_raw]
        return cls(name=data["name"], entries=entries)


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
        clean_goal = goal.strip()
        ledger.add_entry(
            "goal",
            LedgerEntry(
                key="goal.primary",
                value=clean_goal,
                source=LedgerSource.USER_GOAL,
                confidence=0.95 if clean_goal else 0.0,
                status=LedgerStatus.CONFIRMED if clean_goal else LedgerStatus.WEAK,
                reversible=False,
                rationale=(
                    "Initial user-provided auto task."
                    if clean_goal
                    else "Auto task goal is blank and must be clarified before Seed generation."
                ),
            ),
        )
        return ledger

    def add_entry(self, section_name: str, entry: LedgerEntry) -> None:
        """Append ``entry`` to a section, marking same-key contradictions explicit.

        A later same-key answer that returns to a previously seen value is treated
        as a correction: older contradictory entries become weak historical facts
        instead of keeping the section permanently conflicting.
        """
        section = self.sections.setdefault(section_name, LedgerSection(section_name))
        same_key_entries = [existing for existing in section.entries if existing.key == entry.key]
        entry_value = _normalize_conflict_value(entry.value)
        matching_prior = [
            existing
            for existing in same_key_entries
            if _normalize_conflict_value(existing.value) == entry_value
        ]
        if (
            same_key_entries
            and entry.source == LedgerSource.USER_GOAL
            and entry.status == LedgerStatus.CONFIRMED
        ):
            for existing in same_key_entries:
                existing.status = LedgerStatus.WEAK
                existing.rationale = (
                    existing.rationale or "Superseded by a later user-confirmed answer."
                )
        elif matching_prior:
            for existing in same_key_entries:
                if entry.status == LedgerStatus.BLOCKED:
                    continue
                if existing.status == LedgerStatus.BLOCKED:
                    existing.status = LedgerStatus.WEAK
                    existing.rationale = (
                        existing.rationale or "Superseded by a later same-key answer."
                    )
                    continue
                if _normalize_conflict_value(existing.value) == entry_value:
                    if existing.status == LedgerStatus.CONFLICTING:
                        existing.status = entry.status
                    continue
                existing.status = LedgerStatus.WEAK
                existing.rationale = (
                    existing.rationale or "Superseded by a later same-key correction."
                )
        else:
            for existing in same_key_entries:
                if entry.status == LedgerStatus.BLOCKED:
                    continue
                if existing.status == LedgerStatus.BLOCKED:
                    existing.status = LedgerStatus.WEAK
                    existing.rationale = (
                        existing.rationale or "Superseded by a later same-key answer."
                    )
                    continue
                existing.status = LedgerStatus.CONFLICTING
                entry.status = LedgerStatus.CONFLICTING
                existing.rationale = (
                    existing.rationale or "Conflicts with another auto ledger answer."
                )
                entry.rationale = entry.rationale or "Conflicts with another auto ledger answer."
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
        blocked = {
            LedgerStatus.MISSING,
            LedgerStatus.WEAK,
            LedgerStatus.CONFLICTING,
            LedgerStatus.BLOCKED,
        }
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
        if not isinstance(data, dict):
            msg = "ledger must be an object"
            raise ValueError(msg)
        sections_raw = data.get("sections")
        if not isinstance(sections_raw, dict):
            msg = "ledger sections must be an object"
            raise ValueError(msg)
        sections: dict[str, LedgerSection] = {}
        for name, section_raw in sections_raw.items():
            if not isinstance(name, str) or not name.strip():
                msg = "ledger section keys must be non-empty strings"
                raise ValueError(msg)
            section = LedgerSection.from_dict(section_raw)
            if section.name != name:
                msg = f"ledger section key/name mismatch: {name} != {section.name}"
                raise ValueError(msg)
            sections[name] = section

        question_history = data.get("question_history")
        if not isinstance(question_history, list):
            msg = "ledger question_history must be a list"
            raise ValueError(msg)
        for item in question_history:
            if not isinstance(item, dict):
                msg = "ledger question_history entries must be objects"
                raise ValueError(msg)
            if set(item) != {"question", "answer"}:
                msg = "ledger question_history entries must contain question and answer"
                raise ValueError(msg)
            if not isinstance(item["question"], str) or not isinstance(item["answer"], str):
                msg = "ledger question_history question and answer must be strings"
                raise ValueError(msg)

        ledger = cls(sections=sections, question_history=[dict(item) for item in question_history])
        for required in REQUIRED_SECTIONS:
            ledger.sections.setdefault(required, LedgerSection(required))
        return ledger

    def _values_for_sources(self, sources: set[LedgerSource]) -> list[str]:
        resolved: dict[tuple[str, str], str] = {}
        inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
        for section in self.sections.values():
            for entry in section.entries:
                if entry.source not in sources or entry.status in inactive:
                    continue
                resolved[(section.name, entry.key)] = entry.value

        values: list[str] = []
        seen: set[str] = set()
        for value in resolved.values():
            normalized = _normalize_conflict_value(value)
            if normalized in seen:
                continue
            seen.add(normalized)
            values.append(value)
        return values


def _normalize_conflict_value(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _truncate(value: str, *, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 15].rstrip() + " ... (truncated)"
