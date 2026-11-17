"""Map Seed QA feedback onto a bounded repair plan.

The Seed is the user-owned contract: its ``constraints`` reach the executor and
render in the Seed card and YAML as requirements the user asked for. Seed QA
feedback is reviewer prose about the Seed document, so the repair keeps two
kinds of mapped line apart:

* **constraints** state product or runtime behaviour the executor can honor
  (``Failure paths must leave no partial output artifacts.``). The repair may
  append them to ``Seed.constraints``.
* **instructions** address the Seed document or the repair itself (keep ledger
  non-goals in Seed surfaces, keep diagnostic prose out of constraints, write
  direct acceptance criteria, meet the readiness gate). They steer the repair
  step, as the lateral persona's problem context and as directives for the
  deterministic rebuild, and are never written to the Seed (#3033).

Each mapped line is classified where :func:`seed_qa_repair_plan` defines it, so
the split is a reviewed table, not a text heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from ouroboros.auto.adapters import EvaluateResult


class SeedQaRepairMappingError(RuntimeError):
    def __init__(self, feedback: tuple[str, ...]) -> None:
        self.feedback: tuple[str, ...] = feedback
        super().__init__(
            "Seed QA feedback could not be mapped to a bounded repair; "
            "manual Seed revision is required"
        )


AMBIGUITY_INSTRUCTION = "Seed metadata must satisfy the readiness gate: ambiguity_score <= 0.20."
LEDGER_PRESERVATION_INSTRUCTION = (
    "Preserve ledger non-goals and runtime context in executable Seed surfaces; "
    "use constraints prefixed with `Non-goal:` and explicit runtime constraints or ontology fields."
)
DIAGNOSTIC_PROSE_INSTRUCTION = (
    "Constraints must contain only actionable product/runtime constraints; "
    "omit QA or lateral diagnostic prose."
)
DIRECT_CRITERIA_INSTRUCTION = (
    "Acceptance criteria must be direct executable checks, not generic templates."
)
# Earlier releases appended this placeholder to ``Seed.constraints`` when the
# lateral persona returned no usable decision. It is no longer produced; it is
# listed so a repair round drops it from a Seed those releases persisted.
LEGACY_LATERAL_PLACEHOLDER = (
    "Resolve Seed QA feedback before execution without copying recovery persona "
    "prompts, failed-run transcripts, or diagnostic prose."
)
_INSTRUCTIONS = tuple(
    text.casefold()
    for text in (
        AMBIGUITY_INSTRUCTION,
        LEDGER_PRESERVATION_INSTRUCTION,
        DIAGNOSTIC_PROSE_INSTRUCTION,
        DIRECT_CRITERIA_INSTRUCTION,
        LEGACY_LATERAL_PLACEHOLDER,
    )
)


def contains_seed_qa_repair_instruction(text: str) -> bool:
    """Whether ``text`` carries one of the repair's own instruction sentences."""
    lowered = text.casefold()
    return any(instruction in lowered for instruction in _INSTRUCTIONS)


@dataclass(frozen=True, slots=True)
class SeedQaRepairPlan:
    """Mapped Seed QA feedback, split by where each line may go.

    ``guidance`` is every mapped line in mapping order; it routes and briefs
    the lateral persona. ``constraints`` is the subset that may be appended to
    ``Seed.constraints``. ``restore_ledger_non_goals`` asks the deterministic
    rebuild to write the ledger's own binding non-goals as ``Non-goal:``
    constraints, which is what :data:`LEDGER_PRESERVATION_INSTRUCTION` asks for.
    """

    feedback: tuple[str, ...]
    guidance: tuple[str, ...]
    constraints: tuple[str, ...]
    restore_ledger_non_goals: bool


def seed_qa_repair_plan(qa_result: EvaluateResult) -> SeedQaRepairPlan:
    """Translate QA diagnostics into a bounded repair plan.

    The Seed direction must not absorb raw reviewer/lateral transcripts. Keep
    the feedback as bounded repair intent, not as pasted diagnostic evidence.
    Raises :class:`SeedQaRepairMappingError` when no line maps.
    """
    feedback = tuple(
        item.strip()
        for item in (*qa_result.differences[:5], *qa_result.suggestions[:5])
        if item.strip()
    )
    lowered = "\n".join(feedback).casefold()
    if re.search(r"\bexit[_\s-]*conditions?\b", lowered):
        raise SeedQaRepairMappingError(feedback)
    guidance: list[str] = []
    constraints: list[str] = []

    def instruct(text: str) -> None:
        guidance.append(text)

    def require(text: str) -> None:
        guidance.append(text)
        constraints.append(text)

    if requests_seed_qa_ambiguity_repair(qa_result):
        instruct(AMBIGUITY_INSTRUCTION)
    restore_non_goals = "non_goals" in lowered or "non-goals" in lowered
    if restore_non_goals or "runtime_context" in lowered:
        instruct(LEDGER_PRESERVATION_INSTRUCTION)
    if "polluted" in lowered or "diagnostic" in lowered or "lateral repair" in lowered:
        instruct(DIAGNOSTIC_PROSE_INSTRUCTION)
    if "transcript schema" in lowered or "schema_version" in lowered:
        require("Use one transcript JSON schema consistently across acceptance criteria.")
    if "no-op" in lowered or "noop" in lowered:
        require("Define explicit no-op scope for supported command behavior.")
    if "review-blocking" in lowered:
        require("Introduce the review-blocking post-QA constraint before execution.")
    if "binding" in lowered and "contract" in lowered:
        require("Define one explicit binding contract before execution.")
    if "templated" in lowered or "indirect" in lowered:
        instruct(DIRECT_CRITERIA_INSTRUCTION)
    if "partial" in lowered and (
        "output" in lowered or "mp4" in lowered or "transcript" in lowered
    ):
        require("Failure paths must leave no partial output artifacts.")
    if not guidance:
        raise SeedQaRepairMappingError(feedback)
    return SeedQaRepairPlan(
        feedback=feedback,
        guidance=tuple(dict.fromkeys(guidance)),
        constraints=tuple(dict.fromkeys(constraints)),
        restore_ledger_non_goals=restore_non_goals,
    )


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
    for item in (*qa_result.differences, *qa_result.suggestions):
        lowered = item.strip().casefold()
        if any(re.fullmatch(rf"{pattern}[.!]?", lowered) for pattern in patterns):
            return True
    return False
