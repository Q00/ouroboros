"""Selected-driver interview answering for ``ooo auto``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Protocol

from ouroboros.auto.answerer import (
    AutoAnswer,
    AutoAnswerContext,
    AutoAnswerer,
    AutoAnswerSource,
    AutoBlocker,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.state import AutoBrakeMode
from ouroboros.providers.base import CompletionConfig, LLMAdapter, Message, MessageRole
from ouroboros.providers.factory import create_llm_adapter, resolve_llm_backend


class AsyncAutoAnswerer(Protocol):
    """Protocol for answerers that can draft interview answers asynchronously."""

    async def answer(
        self, question: str, ledger: SeedDraftLedger, context: AutoAnswerContext | None = None
    ) -> AutoAnswer:
        """Draft an answer for one interview question."""

    def apply(self, answer: AutoAnswer, ledger: SeedDraftLedger, *, question: str) -> None:
        """Apply ledger updates associated with an answer."""


@dataclass(slots=True)
class DriverAutoAnswerer:
    """Ask the selected ``llm.backend`` driver to answer every interview question.

    The existing deterministic ``AutoAnswerer`` is still used as a ledger/risk
    scaffold, but the text sent back to the interview backend comes from the
    selected driver.  With brake=on, high-impact/risky drafts become approval
    blockers.  With brake=off, they are sent automatically with assumption and
    provenance tags so the later Seed-ready/A-grade gates remain the safety net.
    """

    backend: str | None = None
    brake: AutoBrakeMode = AutoBrakeMode.ON
    cwd: str | Path | None = None
    adapter: LLMAdapter | None = None
    baseline: AutoAnswerer = field(default_factory=AutoAnswerer)
    timeout_seconds: float | None = 60.0

    def __post_init__(self) -> None:
        self.backend = resolve_llm_backend(self.backend)

    async def answer(
        self, question: str, ledger: SeedDraftLedger, context: AutoAnswerContext | None = None
    ) -> AutoAnswer:
        """Return the selected driver's answer for ``question``."""
        scaffold = self.baseline.answer(question, ledger, context)
        risk = classify_interview_answer_risk(question, scaffold)
        if risk and self.brake == AutoBrakeMode.ON:
            reason = f"brake on: risky auto interview answer requires approval ({risk})"
            return AutoAnswer(
                text=f"Cannot send automatically without approval: {risk}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=AutoBlocker(reason=reason, question=question),
            )

        if self.adapter is None:
            allowed_tools: list[str] | None = None if self.backend == "hermes" else []
            self.adapter = create_llm_adapter(
                backend=self.backend,
                use_case="interview",
                cwd=self.cwd,
                allowed_tools=allowed_tools,
                max_turns=1,
                timeout=self.timeout_seconds,
            )
        assert self.adapter is not None
        prompt = _driver_prompt(
            question, ledger, scaffold, backend=self.backend or "driver", risk=risk
        )
        result = await self.adapter.complete(
            messages=[Message(role=MessageRole.USER, content=prompt)],
            config=CompletionConfig(
                model="default",
                temperature=0.2,
                max_tokens=700,
                role="auto_interview_answer",
                max_turns=1,
            ),
        )
        if not result.is_ok:
            return AutoAnswer(
                text=f"Cannot obtain driver answer: {result.error}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=AutoBlocker(
                    reason=f"selected driver {self.backend} failed to answer: {result.error}",
                    question=question,
                ),
            )
        text = _clean_driver_text(result.value.content)
        if not text:
            return AutoAnswer(
                text="Cannot obtain driver answer: empty response",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=AutoBlocker(
                    reason=f"selected driver {self.backend} returned an empty answer",
                    question=question,
                ),
            )

        assumptions = list(scaffold.assumptions)
        confidence = min(scaffold.confidence, 0.82)
        if risk:
            assumptions.append(f"brake off auto-sent risky driver answer: {risk}")
            confidence = min(confidence, 0.62)
        tagged_text = _tag_driver_text(
            text, backend=self.backend or "driver", brake=self.brake, risk=risk
        )
        return AutoAnswer(
            text=tagged_text,
            source=AutoAnswerSource.DRIVER,
            confidence=confidence,
            ledger_updates=_ledger_updates_for(
                scaffold,
                driver_text=tagged_text,
                risk=risk,
                backend=self.backend or "driver",
            ),
            assumptions=assumptions,
            non_goals=list(scaffold.non_goals),
        )

    def apply(self, answer: AutoAnswer, ledger: SeedDraftLedger, *, question: str) -> None:
        """Apply a selected-driver answer to the ledger."""
        self.baseline.apply(answer, ledger, question=question)


def classify_interview_answer_risk(question: str, scaffold: AutoAnswer | None = None) -> str | None:
    """Return a risk label when an interview answer should be approval-gated."""
    if scaffold is not None and scaffold.blocker is not None:
        return scaffold.blocker.reason
    lowered = question.lower()
    patterns: tuple[tuple[str, str], ...] = (
        (
            r"\b(legal|privacy|pii|gdpr|hipaa|compliance|security|credential|secret|token|api key|password)\b",
            "legal/privacy/security/compliance",
        ),
        (
            r"\b(delete|destroy|drop|wipe|remove|irreversible|production|prod|deploy|billing|charge|payment|financial)\b",
            "destructive or financial/production choice",
        ),
        (
            (
                r"\b(?:expand|new acceptance|scope|trade[- ]?off|pricing|business|product decision)\b"
                r"|\badd(?:ing)?\s+(?:(?:a|an|the)\s+)?"
                r"(?:features?|capabilit(?:y|ies)|support|requirements?"
                r"|epic|story(?:\s+points?)?|product\s+area|scope)\b"
            ),
            "scope or product/business tradeoff",
        ),
        (
            r"\b(prefer|preference|always|never)\b.*\b(user|customer|stakeholder)\b",
            "unknown user preference",
        ),
    )
    for pattern, label in patterns:
        if re.search(pattern, lowered):
            return label
    if scaffold is not None and scaffold.confidence < 0.65:
        return "low-confidence high-impact answer"
    return None


def _driver_prompt(
    question: str,
    ledger: SeedDraftLedger,
    scaffold: AutoAnswer,
    *,
    backend: str,
    risk: str | None,
) -> str:
    open_gaps = ", ".join(ledger.open_gaps()) or "none"
    risk_line = f"Risk label: {risk}." if risk else "Risk label: none."
    return f"""You are the selected ooo auto interview driver: {backend}.
Answer the Ouroboros Socratic interview question on behalf of the user.

Rules:
- Answer directly and concisely in 1-4 sentences.
- Preserve the user's goal and avoid inventing user preferences.
- If you make an assumption, state it explicitly.
- Do not ask a follow-up question; this auto mode must answer every interview question.
- Existing auto pipeline, Seed-ready checks, and A-grade review continue after your answer.

Current goal: {_ledger_goal(ledger)}
Open ledger gaps: {open_gaps}
Deterministic scaffold answer: {scaffold.text}
{risk_line}

Interview question:
{question}
""".strip()


def _ledger_goal(ledger: SeedDraftLedger) -> str:
    entries = ledger.sections.get("goal").entries if "goal" in ledger.sections else []
    for entry in reversed(entries):
        if entry.value.strip():
            return entry.value.strip()
    return ""


def _clean_driver_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
    return text


def _tag_driver_text(text: str, *, backend: str, brake: AutoBrakeMode, risk: str | None) -> str:
    tags = [f"driver={backend}", f"brake={brake.value}"]
    if risk:
        tags.append(f"risk={risk}")
    return f"[{' ; '.join(tags)}] {text}"


def _ledger_updates_for(
    scaffold: AutoAnswer, *, driver_text: str, risk: str | None, backend: str
) -> list[tuple[str, LedgerEntry]]:
    """Build ledger updates that reflect the driver's actual freeform answer.

    The deterministic scaffold provides the structural keys (so downstream
    seed-generation stays section-aware), but each entry's value is replaced
    with the driver's freeform answer and marked CONFLICTING with reduced
    confidence so the grading/Seed-ready gates treat the entry as needing
    verification rather than as a confirmed scaffold choice. This prevents
    the transcript (driver answer) and the persisted ledger (scaffold value)
    from diverging silently into two competing sources of truth.
    """
    updates = [
        (
            section,
            LedgerEntry(
                key=entry.key,
                value=f"driver:{backend} answer (verbatim): {driver_text}",
                source=entry.source,
                confidence=min(entry.confidence, 0.4),
                status=LedgerStatus.CONFLICTING,
                reversible=entry.reversible,
                rationale=(
                    "Selected-driver freeform answer is the canonical source; the "
                    "scaffold's structural key is kept so Seed generation stays "
                    "section-aware, but the value is the driver answer verbatim and "
                    "the entry is marked CONFLICTING so grading and the Seed-ready "
                    "gate flag it for verification before A-grade. Original "
                    f"scaffold value (kept only as evidence): {entry.value}"
                ),
                evidence=[*entry.evidence, f"driver:{backend}", "auto_interview_transcript"],
            ),
        )
        for section, entry in scaffold.ledger_updates
    ]
    if risk:
        updates.append(
            (
                "constraints",
                LedgerEntry(
                    key=f"risk.auto_driver.{_slug_key(risk)}",
                    value=f"Driver {backend} auto-sent a risky interview answer under brake=off: {risk}",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.6,
                    status=LedgerStatus.INFERRED,
                    rationale="Risk was preserved as provenance for Seed-ready and A-grade review gates.",
                ),
            )
        )
    return updates


def _slug_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "risk"
