"""Bounded mappings from Seed QA diagnostics to repair contracts."""

from __future__ import annotations

import re

from ouroboros.auto.adapters import EvaluateResult
from ouroboros.core.seed import Seed
from ouroboros.core.task_type import (
    _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN,
    _SEED_ID_CONTINUATION_PATTERN,
    _SEED_ID_PATTERN,
    _candidate_segment,
    _governor_scope_start,
    _has_contract_local_ambiguity,
    _resolve_authoritative_matches,
    explicit_task_type_from_goal,
    has_affirmative_contract_prefix,
    has_ambiguous_contract_governor,
    has_comma_non_binding_governor,
    has_historical_governor,
    has_negative_governor,
    has_optional_contract_tail,
    has_post_match_rejection,
    is_non_binding_contract_segment,
    is_quoted_contract,
)


class SeedQaRepairMappingError(RuntimeError):
    def __init__(self, feedback: tuple[str, ...]) -> None:
        self.feedback = feedback
        super().__init__(
            "Seed QA feedback could not be mapped to a bounded repair; "
            "manual Seed revision is required"
        )


def normalized_seed_qa_feedback(
    qa_result: EvaluateResult,
    *,
    seed: Seed | None = None,
) -> tuple[str, ...]:
    """Translate QA diagnostics into short actionable Seed repair constraints."""
    feedback = tuple(
        item.strip()
        for item in (*qa_result.differences[:5], *qa_result.suggestions[:5])
        if item.strip()
    )
    lowered = "\n".join(feedback).casefold()
    task_type = requested_seed_qa_task_type(seed) if seed is not None else None
    if task_type is None and re.search(r"\bexit[_\s-]*conditions?\b", lowered):
        raise SeedQaRepairMappingError(feedback)
    repairs: list[str] = []
    if task_type is not None:
        repairs.append(f"Seed task_type must be {task_type}.")
    if requests_seed_qa_ambiguity_repair(qa_result):
        repairs.append("Seed metadata must satisfy the readiness gate: ambiguity_score <= 0.20.")
    if "non_goals" in lowered or "non-goals" in lowered or "runtime_context" in lowered:
        repairs.append(
            "Preserve ledger non-goals and runtime context in executable Seed surfaces; "
            "use constraints prefixed with `Non-goal:` and explicit runtime constraints or ontology fields."
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
    if "uncertainty" in lowered and "fallback" in lowered:
        repairs.append(
            "Document a stream-specific uncertainty and fallback rule for each "
            "high-risk business assumption."
        )
    if not repairs:
        raise SeedQaRepairMappingError(feedback)
    return tuple(dict.fromkeys(repairs))


def requested_seed_qa_task_type(seed: Seed) -> str | None:
    """Return a binding goal task type only when the Seed contradicts it."""
    task_type = explicit_task_type_from_goal(seed.goal)
    if task_type is None or task_type == seed.task_type.casefold():
        return None
    return task_type


def inherited_parent_seed_id(seed: Seed) -> str | None:
    """Return the inherited Seed ID explicitly named by the goal contract."""
    seed_id = rf"(?P<seed_id>{_SEED_ID_PATTERN})"
    patterns = (
        re.compile(
            rf"\b(?:inherit(?:ing)?(?:\s+from)?|derive(?:d)?\s+from|"
            rf"(?:set\s+)?parent(?:_seed_id|\s+seed)?\s*(?:is|=|:|to))\s+{seed_id}"
            rf"(?!{_SEED_ID_CONTINUATION_PATTERN})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\buse\s+{seed_id}(?!{_SEED_ID_CONTINUATION_PATTERN})"
            rf"\s+as\s+(?:the\s+)?parent(?:_seed|\s+seed)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<!\S)(?P<seed_id>[^\s,;!?]+?)(?=(?:을|를)?\s*"
            r"(?:계승|상속)(?!하지))"
            r"(?:을|를)?\s*"
            r"(?:계승|상속)(?!하지)",
            re.IGNORECASE,
        ),
    )
    matches: list[tuple[int, int, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(seed.goal):
            segment_start, segment_end = _candidate_segment(seed.goal, match.start(), match.end())
            governor_prefix = seed.goal[
                _governor_scope_start(seed.goal, match.start()) : match.start()
            ]
            contract_prefix = seed.goal[segment_start : match.start()]
            bare_parent_field = (
                re.match(r"\s*(?:the\s+)?parent(?:_seed_id|\s+seed)\b", match.group(), re.I)
                is not None
            )
            current_seed_binding = (
                re.search(
                    r"\b(?:this|the\s+current)\s+seed\s+"
                    r"(?:must|should|will|is\s+to)\s*$",
                    contract_prefix,
                    re.IGNORECASE,
                )
                is not None
            )
            prior_data_plane_governor = (
                bare_parent_field
                and re.search(
                    r"\b(?:route|map|read|treat|persist|store|expose)\b",
                    seed.goal[_governor_scope_start(seed.goal, match.start()) : segment_start],
                    re.IGNORECASE,
                )
                is not None
            )
            if (
                is_non_binding_contract_segment(contract_prefix)
                or is_non_binding_contract_segment(seed.goal[segment_start : match.end()])
                or not has_affirmative_contract_prefix(
                    contract_prefix, allow_task_linker=not bare_parent_field
                )
                and not current_seed_binding
                or prior_data_plane_governor
                or _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN.search(seed.goal[segment_start : match.end()])
                is not None
                or _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN.search(seed.goal[segment_start:segment_end])
                is not None
                or _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN.search(governor_prefix) is not None
                or is_quoted_contract(seed.goal, match.start(), match.end())
                or has_historical_governor(
                    seed.goal, match.start(), _governor_scope_start(seed.goal, match.start())
                )
                or has_negative_governor(
                    seed.goal, match.start(), _governor_scope_start(seed.goal, match.start())
                )
                or has_comma_non_binding_governor(seed.goal, match.start())
                or has_post_match_rejection(seed.goal, match.end())
                or _has_contract_local_ambiguity(seed.goal, match.end(), segment_end)
                or has_optional_contract_tail(seed.goal, match.end(), segment_end)
                or has_ambiguous_contract_governor(governor_prefix)
            ):
                continue
            matches.append((match.start(), match.end(), match.group("seed_id")))
    if not matches:
        return None
    return _resolve_authoritative_matches(seed.goal, matches)


def requests_seed_qa_ambiguity_repair(qa_result: EvaluateResult) -> bool:
    """Return whether QA explicitly requests the Seed ambiguity readiness gate."""
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
