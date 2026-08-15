"""Bounded translation and sanitization of Seed QA repair evidence."""

from __future__ import annotations

import re

from ouroboros.auto.adapters import EvaluateResult, LateralResult


class SeedQaRepairMappingError(RuntimeError):
    def __init__(
        self,
        feedback: tuple[str, ...],
        *,
        code: str = "seed_qa_feedback_unmapped",
        message: str | None = None,
    ) -> None:
        self.feedback = feedback
        self.code = code
        super().__init__(
            message
            or (
                "Seed QA feedback could not be mapped to a bounded repair; "
                "manual Seed revision is required"
            )
        )


_SEED_QA_DIAGNOSTIC_PREFIX_RE = re.compile(
    r"\[seed qa(?: lateral)? repair attempt [^\]]+\]\s*",
    re.IGNORECASE,
)
_SEED_QA_SENSITIVE_RE = re.compile(
    r"(?i)\braw prompt\b|\bignore (?:all )?previous\b|[\w.+-]+@[\w-]+\.[\w.-]+|\w+://\S+"
)


def normalized_seed_qa_lateral_feedback(lateral_result: LateralResult) -> tuple[str, ...]:
    """Translate lateral output into durable implementation constraints."""
    summary = clean_seed_qa_repair_text(lateral_result.approach_summary or "", limit=320)
    decision = clean_seed_qa_repair_text(lateral_result.text, limit=1600)
    persona_prefix = (lateral_result.persona or "").casefold()
    repairs: list[str] = []
    if (
        summary
        and not _is_seed_qa_recovery_transcript(summary)
        and not (persona_prefix and summary.casefold().startswith(f"{persona_prefix}:"))
    ):
        repairs.append(f"Use this bounded implementation approach to resolve Seed QA: {summary}")
    if _is_clean_seed_qa_lateral_decision(decision, raw_text=lateral_result.text):
        repairs.append(f"Use this bounded implementation approach to resolve Seed QA: {decision}")
    if not repairs:
        repairs.append(
            "Resolve Seed QA feedback before execution without copying recovery persona "
            "prompts, failed-run transcripts, or diagnostic prose."
        )
    return tuple(dict.fromkeys(repairs))


def clean_seed_qa_repair_text(text: str, *, limit: int) -> str:
    text = _SEED_QA_DIAGNOSTIC_PREFIX_RE.sub("", text.strip())
    cleaned_lines: list[str] = []
    skipping_diagnostic_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line:
            continue
        if _starts_seed_qa_recovery_context_block(line):
            break
        if _is_seed_qa_recovery_transcript_line(line):
            skipping_diagnostic_block = True
            continue
        if lowered.startswith("# lateral thinking:"):
            continue
        if lowered.startswith("qa differences:") or lowered.startswith("qa suggestions:"):
            skipping_diagnostic_block = True
            continue
        if skipping_diagnostic_block:
            if line.startswith(("-", "*")) or re.match(r"^\d+[\.)]\s+", line):
                continue
            skipping_diagnostic_block = False
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines)
    cleaned = cleaned.replace("# Lateral Thinking:", "").replace("# lateral thinking:", "")
    return cleaned[:limit].strip()


def _is_clean_seed_qa_lateral_decision(decision: str, *, raw_text: str) -> bool:
    if not decision or _is_seed_qa_recovery_transcript(decision):
        return False
    if decision.casefold().startswith("decision:"):
        return True
    if _is_seed_qa_recovery_transcript(raw_text) or _starts_seed_qa_recovery_context_block(
        raw_text.strip()
    ):
        return False
    lowered = raw_text.casefold()
    if (
        "# lateral thinking:" in lowered
        or "qa differences:" in lowered
        or "qa suggestions:" in lowered
    ):
        return False
    return len(decision) <= 420


def _is_seed_qa_recovery_transcript(text: str) -> bool:
    return any(_is_seed_qa_recovery_transcript_line(line) for line in text.splitlines())


def _is_seed_qa_recovery_transcript_line(text: str) -> bool:
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in (
            "## persona:",
            "# persona:",
            "persona:",
            "current approach (not working)",
            "most recent run artifact",
            "problem context",
            "concrete constraints for the generated ouroboros seed",
            "evaluate failed",
            "failed-run transcript",
            "repair transcript",
            "diagnostic recovery text",
        )
    )


def _starts_seed_qa_recovery_context_block(text: str) -> bool:
    lowered = text.casefold()
    return lowered.startswith("## problem context") or lowered.startswith("# problem context")


def normalized_seed_qa_feedback(qa_result: EvaluateResult) -> tuple[str, ...]:
    """Translate QA diagnostics into short actionable Seed repair constraints."""
    feedback = tuple(
        item.strip()
        for item in (*qa_result.differences[:5], *qa_result.suggestions[:5])
        if item.strip()
    )
    lowered = "\n".join(feedback).casefold()
    if re.search(r"\bexit[_\s-]*conditions?\b", lowered):
        raise SeedQaRepairMappingError(feedback)
    if requests_seed_qa_ambiguity_repair(qa_result):
        raise SeedQaRepairMappingError(
            feedback,
            code="seed_qa_ambiguity_unrepairable",
            message=(
                "Seed QA requires ambiguity_score <= 0.20, which a constraint patch "
                "cannot deliver; resume the interview to resolve the ambiguity or "
                "revise the Seed manually"
            ),
        )
    repairs: list[str] = []
    if "non_goals" in lowered or "non-goals" in lowered or "runtime_context" in lowered:
        repairs.append(
            "Preserve ledger non-goals and runtime context in executable Seed surfaces; "
            "use constraints prefixed with `Non-goal:` and explicit runtime constraints "
            "or ontology fields."
        )
    if "polluted" in lowered or "diagnostic" in lowered or "lateral repair" in lowered:
        repairs.append(
            "Constraints must contain only actionable product/runtime constraints; "
            "omit QA or lateral diagnostic prose."
        )
    if "transcript schema" in lowered or "schema_version" in lowered:
        repairs.append("Use one transcript JSON schema consistently across acceptance criteria.")
    if "no-op" in lowered or "noop" in lowered:
        repairs.append("Define explicit no-op scope for supported command behavior.")
    if "review-blocking" in lowered:
        repairs.append("Introduce the review-blocking post-QA constraint before execution.")
    if "binding" in lowered and "contract" in lowered:
        repairs.append("Define one explicit binding contract before execution.")
    if "templated" in lowered or "indirect" in lowered:
        repairs.append(
            "Acceptance criteria must be direct executable checks, not generic templates."
        )
    if "partial" in lowered and (
        "output" in lowered or "mp4" in lowered or "transcript" in lowered
    ):
        repairs.append("Failure paths must leave no partial output artifacts.")
    if not repairs:
        raise SeedQaRepairMappingError(feedback)
    return tuple(dict.fromkeys(repairs))


def requests_seed_qa_ambiguity_repair(qa_result: EvaluateResult) -> bool:
    score = r"(?:metadata\.)?ambiguity_score"
    target = r"0\.2(?:0)?"
    patterns = (
        rf"{score}\s*(?:<=|<)\s*{target}",
        rf"{score}\s+(?:must|should|needs? to)\s+(?:be|remain)\s*(?:<=|<)\s*{target}",
        rf"{score}\s+(?:must|should|needs? to)\s+be\s+reduced\s+to\s+{target}",
        rf"{score}\s+(?:must|should|needs? to)\s+be\s+(?:at most|no greater than)\s+{target}",
        rf"{score}\s+must\s+not\s+exceed\s+{target}",
        rf"{score}\s+(?:must|should)\s+not\s+be\s+greater\s+than\s+{target}",
        rf"{score}\s+(?:is|remains)\s+above\s+{target}\s+and\s+exceeds\s+(?:the\s+)?(?:required\s+)?(?:readiness\s+)?gate",
        rf"{score}\s+(?:is|=)\s*(?:0\.\d+|1\.0+)\s*,?\s*(?:which\s+)?(?:exceeds?|exceeding|is above|is greater than)\s+(?:the\s+)?(?:required\s+)?(?:readiness\s+gate(?:\s+of)?\s*)?(?:<=?\s*)?{target}",
    )
    return any(
        re.fullmatch(rf"{pattern}[.!]?", item.strip().casefold())
        for item in (*qa_result.differences, *qa_result.suggestions)
        for pattern in patterns
    )


def safe_seed_qa_evidence(feedback: tuple[str, ...]) -> list[str]:
    """Sanitize QA feedback for durable state without discarding its substance."""
    items: list[str] = []
    for raw in feedback[:5]:
        cleaned = clean_seed_qa_repair_text(raw, limit=240)
        if not cleaned or _SEED_QA_SENSITIVE_RE.search(cleaned):
            continue
        items.append(cleaned)
    return items


def safe_seed_qa_verdict(verdict: str) -> str:
    normalized = verdict.strip().casefold()
    if normalized in {"fail", "pass", "revise"}:
        return normalized
    return "unknown"


def safe_seed_qa_error_detail(error: object) -> str:
    """Classify an evaluator error without persisting provider output."""
    lowered = str(error).casefold()
    categories = (
        (("rate limit", "too many requests", "429"), "provider rate limit"),
        (
            ("unauthorized", "authentication", "api key", "credential"),
            "provider authentication failure",
        ),
        (("connection", "network", "unavailable", "timed out"), "provider connectivity failure"),
    )
    for markers, summary in categories:
        if any(marker in lowered for marker in markers):
            return summary
    return "provider evaluator failure"
