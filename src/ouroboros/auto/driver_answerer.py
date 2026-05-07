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
            try:
                self.adapter = create_llm_adapter(
                    backend=self.backend,
                    use_case="interview",
                    cwd=self.cwd,
                    allowed_tools=[],
                    max_turns=1,
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                return _driver_failure_answer(
                    backend=self.backend,
                    question=question,
                    failure=f"{type(exc).__name__}: {exc}",
                )
        assert self.adapter is not None
        prompt = _driver_prompt(
            question, ledger, scaffold, backend=self.backend or "driver", risk=risk
        )
        try:
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
        except Exception as exc:
            return _driver_failure_answer(
                backend=self.backend,
                question=question,
                failure=f"{type(exc).__name__}: {exc}",
            )
        if not result.is_ok:
            return _driver_failure_answer(
                backend=self.backend,
                question=question,
                failure=str(result.error),
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


def _driver_failure_answer(*, backend: str | None, question: str, failure: str) -> AutoAnswer:
    """Convert selected-driver backend failures into recoverable interview blockers."""
    return AutoAnswer(
        text=f"Cannot obtain driver answer: {failure}",
        source=AutoAnswerSource.BLOCKER,
        confidence=1.0,
        blocker=AutoBlocker(
            reason=f"selected driver {backend} failed to answer: {failure}",
            question=question,
        ),
    )


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
                r"\b(expand|increase|broaden|change)\s+(the\s+)?scope\b"
                r"|\bscope\s+(change|expansion|trade[- ]?off)\b"
                r"|\bnew acceptance\b"
                r"|\btrade[- ]?off\b"
                r"|\b(pricing|business|product decision)\b"
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
    updates = [
        _driver_backed_entry(section, entry, driver_text=driver_text, backend=backend)
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


def _driver_backed_entry(
    section: str, entry: LedgerEntry, *, driver_text: str, backend: str
) -> tuple[str, LedgerEntry]:
    supported = _driver_text_supports_entry(driver_text, entry.value)
    status = entry.status if supported else LedgerStatus.WEAK
    value = entry.value if supported else driver_text
    confidence = (
        entry.confidence
        if supported and entry.status == LedgerStatus.CONFIRMED
        else min(entry.confidence, 0.72)
    )
    rationale = (
        "Selected-driver answer was sent to the interview and supports this "
        f"structured scaffold entry. Driver answer was: {driver_text}"
        if supported
        else (
            "Selected-driver answer was sent to the interview, but it did not "
            "explicitly support this deterministic scaffold entry; keep the "
            "section open instead of resolving the ledger against a different "
            f"contract. Scaffold was: {entry.value}"
        )
    )
    return (
        section,
        LedgerEntry(
            key=entry.key,
            value=value,
            source=entry.source,
            confidence=confidence,
            status=status,
            reversible=entry.reversible,
            rationale=rationale,
            evidence=[*entry.evidence, f"driver:{backend}"],
        ),
    )


_SUPPORT_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "by",
        "for",
        "package",
        "managed",
        "the",
        "use",
        "with",
        "should",
        "existing",
    }
)


def _driver_text_supports_entry(driver_text: str, scaffold_value: str) -> bool:
    """Return True when the driver answer visibly supports a scaffold value."""
    scaffold_tokens = _support_tokens(scaffold_value)
    if not scaffold_tokens:
        return False
    driver_tokens = _support_tokens(driver_text)
    return scaffold_tokens <= driver_tokens


def _support_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in _SUPPORT_STOPWORDS and len(token) >= 2
    }


def _slug_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "risk"
