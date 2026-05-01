"""Vanilla Hermes baselines for RLM benchmark comparison.

The baseline path intentionally performs a single Hermes RPC call. It does not
construct an Ouroboros outer scaffold, schedule child RLM nodes, or call the
recursive RLM loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal

from ouroboros.core.errors import ProviderError
from ouroboros.rlm.loop import RLM_BENCHMARK_OUTPUT_SCHEMA_VERSION
from ouroboros.rlm.trace import hash_trace_text

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentRuntime

RLM_VANILLA_TRUNCATION_BASELINE_ID = "rlm-vanilla-truncation-baseline-v1"
RLM_VANILLA_BASELINE_INPUT_SCHEMA_VERSION = "rlm.vanilla_baseline.input.v1"
RLM_VANILLA_BASELINE_MODE = "vanilla_single_pass"
RLM_VANILLA_BASELINE_CALL_ID = "rlm_call_vanilla_truncation_baseline"
RLM_VANILLA_BASELINE_RESULT_ARTIFACT_TYPE = "rlm_vanilla_baseline_result"
RLM_VANILLA_BASELINE_RESULT_DIR = Path(".ouroboros") / "rlm" / "baselines"
RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION = "rlm.vanilla_baseline.quality.v1"

HERMES_VANILLA_BASELINE_SYSTEM_PROMPT = """You are the vanilla Hermes baseline for an RLM comparison.

Perform exactly one bounded single-pass analysis of the supplied JSON envelope.
Do not invoke Ouroboros, do not run any ooo command, and do not request recursive sub-calls.
Use only the retained context in the envelope; lines beyond the truncation boundary are unavailable.
Return a single JSON object with mode, verdict, confidence, result, evidence_references, and residual_gaps."""


@dataclass(frozen=True, slots=True)
class RLMVanillaTruncationBaselineConfig:
    """Configuration for one single-pass Hermes truncation baseline."""

    fixture: Mapping[str, Any]
    cwd: Path
    hermes_runtime: AgentRuntime | None = field(default=None, compare=False, repr=False)
    result_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RLMVanillaBaselineQualityScore:
    """Deterministic quality score for a vanilla truncation baseline output."""

    schema_version: str
    scoring_method: str
    score: float
    required_field_score: float
    confidence_score: float
    retained_fact_citation_score: float
    truncation_boundary_score: float
    omitted_fact_safety_score: float
    required_fields_present: tuple[str, ...]
    required_fields_missing: tuple[str, ...]
    cited_retained_fact_ids: tuple[str, ...]
    missing_retained_fact_ids: tuple[str, ...]
    cited_selected_chunk_ids: tuple[str, ...]
    cited_omitted_chunk_ids: tuple[str, ...]
    claimed_omitted_fact_ids: tuple[str, ...]
    reports_truncation_boundary: bool
    confidence: float | None = None
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the quality score for result artifacts."""
        return {
            "schema_version": self.schema_version,
            "scoring_method": self.scoring_method,
            "score": self.score,
            "required_field_score": self.required_field_score,
            "confidence_score": self.confidence_score,
            "retained_fact_citation_score": self.retained_fact_citation_score,
            "truncation_boundary_score": self.truncation_boundary_score,
            "omitted_fact_safety_score": self.omitted_fact_safety_score,
            "required_fields_present": list(self.required_fields_present),
            "required_fields_missing": list(self.required_fields_missing),
            "cited_retained_fact_ids": list(self.cited_retained_fact_ids),
            "missing_retained_fact_ids": list(self.missing_retained_fact_ids),
            "cited_selected_chunk_ids": list(self.cited_selected_chunk_ids),
            "cited_omitted_chunk_ids": list(self.cited_omitted_chunk_ids),
            "claimed_omitted_fact_ids": list(self.claimed_omitted_fact_ids),
            "reports_truncation_boundary": self.reports_truncation_boundary,
            "confidence": self.confidence,
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True, slots=True)
class RLMVanillaTruncationBaselineResult:
    """Result of the single-pass Hermes truncation baseline."""

    baseline_id: str
    fixture_id: str
    target_path: str
    status: Literal["completed", "failed"]
    success: bool
    call_id: str
    prompt: str
    completion: str
    selected_chunk_ids: tuple[str, ...]
    omitted_chunk_ids: tuple[str, ...]
    retained_line_count: int
    omitted_line_count: int
    target_line_count: int
    elapsed_ms: int
    output_quality: RLMVanillaBaselineQualityScore
    hermes_subcall_count: int = 1
    error_message: str | None = None
    result_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the baseline result for tests and benchmark artifacts."""
        return {
            "baseline_id": self.baseline_id,
            "fixture_id": self.fixture_id,
            "target_path": self.target_path,
            "status": self.status,
            "success": self.success,
            "call_id": self.call_id,
            "selected_chunk_ids": list(self.selected_chunk_ids),
            "omitted_chunk_ids": list(self.omitted_chunk_ids),
            "retained_line_count": self.retained_line_count,
            "omitted_line_count": self.omitted_line_count,
            "target_line_count": self.target_line_count,
            "elapsed_ms": self.elapsed_ms,
            "hermes_subcall_count": self.hermes_subcall_count,
            "output_quality_score": self.output_quality.score,
            "output_quality": self.output_quality.to_dict(),
            "prompt_hash": hash_trace_text(self.prompt),
            "completion_hash": hash_trace_text(self.completion),
            "error_message": self.error_message,
            "result_path": str(self.result_path) if self.result_path is not None else None,
        }

    def to_rlm_result_dict(self) -> dict[str, Any]:
        """Serialize the baseline in the RLM benchmark-result comparison shape."""
        completion_json: Any | None
        try:
            parsed_completion = json.loads(self.completion)
        except json.JSONDecodeError:
            completion_json = None
        else:
            completion_json = parsed_completion if isinstance(parsed_completion, dict) else None

        return {
            "schema_version": RLM_BENCHMARK_OUTPUT_SCHEMA_VERSION,
            "artifact_type": RLM_VANILLA_BASELINE_RESULT_ARTIFACT_TYPE,
            "benchmark_id": self.baseline_id,
            "generated_rlm_tree_depth": 0,
            "source_evidence": [],
            "cited_source_file_count": 0,
            "report_markdown": _render_vanilla_baseline_report(self),
            "baseline": self.to_dict(),
            "runner_output": {
                "mode": RLM_VANILLA_BASELINE_MODE,
                "status": self.status,
                "success": self.success,
                "call_id": self.call_id,
                "hermes_subcall_count": self.hermes_subcall_count,
                "output_quality_score": self.output_quality.score,
                "output_quality": self.output_quality.to_dict(),
                "completion": self.completion,
                "completion_json": completion_json,
                "completion_hash": hash_trace_text(self.completion),
                "elapsed_ms": self.elapsed_ms,
                "error_message": self.error_message,
            },
        }


def load_truncation_fixture(path: Path) -> dict[str, Any]:
    """Load an RLM truncation fixture JSON document."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"Unable to read RLM truncation fixture: {path}"
        raise ValueError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Invalid RLM truncation fixture JSON: {path}"
        raise ValueError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"RLM truncation fixture must be a JSON object: {path}"
        raise ValueError(msg)
    return payload


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f"RLM truncation fixture field {field_name!r} must be an object"
        raise ValueError(msg)
    return value


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"RLM truncation fixture field {field_name!r} must be a non-empty string"
        raise ValueError(msg)
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"RLM truncation fixture field {field_name!r} must be a positive integer"
        raise ValueError(msg)
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _clamp_unit_score(value: float) -> float:
    """Clamp and round a normalized quality score."""
    return round(max(0.0, min(1.0, value)), 4)


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    """Return values in first-seen order without duplicates."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _required_completion_fields(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    """Return completion fields required by the truncation fixture."""
    required_fields: list[str] = []
    requirements = fixture.get("completion_requirements")
    if isinstance(requirements, Sequence) and not isinstance(requirements, str):
        for requirement in requirements:
            requirement_mapping = requirement if isinstance(requirement, Mapping) else {}
            for field_name in _string_tuple(requirement_mapping.get("required_output_fields")):
                required_fields.append(field_name)

    if not required_fields:
        required_fields.extend(
            [
                "mode",
                "verdict",
                "confidence",
                "result",
                "evidence_references",
                "residual_gaps",
            ]
        )
    return _ordered_unique(required_fields)


def _required_fact_ids(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    """Return retained fact IDs that the fixture says a valid completion must cite."""
    required_fact_ids: list[str] = []
    requirements = fixture.get("completion_requirements")
    if isinstance(requirements, Sequence) and not isinstance(requirements, str):
        for requirement in requirements:
            requirement_mapping = requirement if isinstance(requirement, Mapping) else {}
            required_fact_ids.extend(_string_tuple(requirement_mapping.get("required_fact_ids")))

    if not required_fact_ids:
        retained_facts = fixture.get("expected_retained_facts")
        if isinstance(retained_facts, Sequence) and not isinstance(retained_facts, str):
            for fact in retained_facts:
                fact_mapping = fact if isinstance(fact, Mapping) else {}
                fact_id = fact_mapping.get("fact_id")
                if isinstance(fact_id, str) and fact_id:
                    required_fact_ids.append(fact_id)
    return _ordered_unique(required_fact_ids)


def _minimum_completion_confidence(fixture: Mapping[str, Any]) -> float:
    """Return the strongest minimum confidence requirement in the fixture."""
    minimums: list[float] = []
    requirements = fixture.get("completion_requirements")
    if isinstance(requirements, Sequence) and not isinstance(requirements, str):
        for requirement in requirements:
            requirement_mapping = requirement if isinstance(requirement, Mapping) else {}
            value = requirement_mapping.get("minimum_confidence")
            if isinstance(value, int | float) and not isinstance(value, bool):
                minimums.append(float(value))
    return max(minimums) if minimums else 0.0


def _completion_evidence_chunk_ids(completion_json: Mapping[str, Any]) -> tuple[str, ...]:
    """Return chunk IDs cited through the completion evidence list."""
    evidence_references = _completion_evidence_references(completion_json)
    chunk_ids: list[str] = []
    for reference in evidence_references:
        chunk_id = _fact_entry_chunk_id(reference)
        if chunk_id is not None:
            chunk_ids.append(chunk_id)
    return _ordered_unique(chunk_ids)


def _completion_evidence_references(
    completion_json: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Return structured evidence references from a completion."""
    evidence_references = completion_json.get("evidence_references")
    if not isinstance(evidence_references, Sequence) or isinstance(evidence_references, str):
        return ()

    references: list[Mapping[str, Any]] = []
    for reference in evidence_references:
        if isinstance(reference, Mapping):
            references.append(reference)
    return tuple(references)


def _fact_entry_chunk_id(entry: Mapping[str, Any]) -> str | None:
    """Return the source chunk ID from a structured fact/evidence entry."""
    for key in ("chunk_id", "evidence_chunk_id", "source_chunk_id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _fact_entry_text(entry: Mapping[str, Any]) -> str:
    """Return claim-bearing text from a fact or evidence entry."""
    values: list[str] = []
    for key in ("quoted_evidence", "text", "statement", "claim", "summary"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    return "\n".join(values)


def _completion_fact_entries(completion_json: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Return structured fact entries that assert observed or retained facts."""
    result = completion_json.get("result")
    if not isinstance(result, Mapping):
        return ()

    entries: list[Mapping[str, Any]] = []
    for key in (
        "retained_facts",
        "observed_facts",
        "facts",
        "retained_evidence",
        "observed_evidence",
    ):
        value = result.get(key)
        if isinstance(value, Mapping):
            entries.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, str):
            entries.extend(item for item in value if isinstance(item, Mapping))

    if isinstance(result.get("fact_id"), str):
        entries.append(result)
    return tuple(entries)


def _completion_result_claim_texts(completion_json: Mapping[str, Any]) -> tuple[str, ...]:
    """Return top-level result text that can make an assertive claim."""
    result = completion_json.get("result")
    if not isinstance(result, Mapping):
        return ()

    texts: list[str] = []
    for key in ("summary", "answer", "final_answer"):
        value = result.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    return tuple(texts)


def _supported_fact_ids(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Return fact IDs explicitly supported by a fact or evidence entry."""
    fact_ids: list[str] = []
    for key in ("fact_id", "supports_fact_id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            fact_ids.append(value)
    for key in ("fact_ids", "supports_fact_ids", "supported_fact_ids"):
        fact_ids.extend(_string_tuple(entry.get(key)))
    return _ordered_unique(fact_ids)


def _normalized_claim_text(text: str) -> str:
    return " ".join(text.lower().split())


def _text_negates_or_qualifies_fact(text: str) -> bool:
    """Return whether text mentions a fact only as unavailable or unclaimed."""
    normalized = _normalized_claim_text(text)
    guarded_phrases = (
        "cannot be claimed",
        "cannot claim",
        "not be claimed",
        "not claimed",
        "no facts from omitted",
        "not available",
        "not asserted",
        "not supplied",
        "outside the retained context",
        "outside retained context",
        "outside the supplied context",
        "outside supplied context",
        "omitted chunks were outside",
        "if present",
        "if any",
        "not observed",
        "not cited",
        "do not claim",
        "does not claim",
    )
    return any(phrase in normalized for phrase in guarded_phrases)


def _text_mentions_fact(text: str, fact_id: str, fact_text: object) -> bool:
    if fact_id in text:
        return True
    if isinstance(fact_text, str) and fact_text:
        return _normalized_claim_text(fact_text) in _normalized_claim_text(text)
    return False


def _text_asserts_fact(text: str, fact_id: str, fact_text: object) -> bool:
    """Return whether text positively asserts a fixture fact."""
    return _text_mentions_fact(text, fact_id, fact_text) and not _text_negates_or_qualifies_fact(
        text
    )


def _entry_supports_fact(
    entry: Mapping[str, Any],
    *,
    fact_id: str,
    fact: Mapping[str, Any],
    require_chunk_match: bool,
) -> bool:
    """Return whether a structured entry supports a fixture fact claim."""
    fact_chunk_id = fact.get("chunk_id")
    entry_chunk_id = _fact_entry_chunk_id(entry)
    if require_chunk_match and isinstance(fact_chunk_id, str) and fact_chunk_id:
        if entry_chunk_id != fact_chunk_id:
            return False

    if fact_id in _supported_fact_ids(entry):
        return True
    return _text_asserts_fact(_fact_entry_text(entry), fact_id, fact.get("text"))


def _fact_lookup_by_id(
    facts: object,
) -> dict[str, Mapping[str, Any]]:
    """Return fixture fact mappings keyed by fact_id."""
    if not isinstance(facts, Sequence) or isinstance(facts, str):
        return {}

    by_id: dict[str, Mapping[str, Any]] = {}
    for fact in facts:
        fact_mapping = fact if isinstance(fact, Mapping) else {}
        fact_id = fact_mapping.get("fact_id")
        if isinstance(fact_id, str) and fact_id:
            by_id[fact_id] = fact_mapping
    return by_id


def _completion_reports_truncation_boundary(
    completion_text: str,
    fixture: Mapping[str, Any],
) -> bool:
    """Return whether the completion reports the fixture truncation boundary."""
    truncation_config = _mapping(fixture.get("truncation_config"), "truncation_config")
    boundary = _mapping(
        truncation_config.get("truncation_boundary"),
        "truncation_config.truncation_boundary",
    )
    last_retained_line = boundary.get("last_retained_line")
    omitted_line_count = boundary.get("omitted_line_count")
    if not isinstance(last_retained_line, int) or not isinstance(omitted_line_count, int):
        return False

    normalized = completion_text.lower()
    references_boundary = any(
        marker in normalized
        for marker in (
            "truncation boundary",
            "truncation_boundary",
            "last retained line",
            "last_retained_line",
            "omitted line",
            "omitted_line_count",
        )
    )
    return (
        references_boundary
        and str(last_retained_line) in completion_text
        and str(omitted_line_count) in completion_text
    )


def score_vanilla_truncation_baseline_completion(
    fixture: Mapping[str, Any],
    completion: str,
) -> RLMVanillaBaselineQualityScore:
    """Compute a deterministic quality score for the truncation baseline output.

    The score is intentionally local and fixture-driven, so the vanilla baseline
    can be compared against recursive RLM runs without invoking another model.
    """
    required_fields = _required_completion_fields(fixture)
    required_fact_ids = _required_fact_ids(fixture)
    retained_facts = _fact_lookup_by_id(fixture.get("expected_retained_facts"))
    omitted_facts = _fact_lookup_by_id(fixture.get("expected_omitted_facts"))
    selected_chunks, omitted_chunks = _fixture_chunks(fixture)
    selected_chunk_ids = tuple(str(chunk["chunk_id"]) for chunk in selected_chunks)
    omitted_chunk_ids = tuple(str(chunk["chunk_id"]) for chunk in omitted_chunks)

    try:
        parsed_completion = json.loads(completion)
    except json.JSONDecodeError as exc:
        return RLMVanillaBaselineQualityScore(
            schema_version=RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
            scoring_method="truncation_fixture_requirements_v1",
            score=0.0,
            required_field_score=0.0,
            confidence_score=0.0,
            retained_fact_citation_score=0.0,
            truncation_boundary_score=0.0,
            omitted_fact_safety_score=0.0,
            required_fields_present=(),
            required_fields_missing=required_fields,
            cited_retained_fact_ids=(),
            missing_retained_fact_ids=required_fact_ids,
            cited_selected_chunk_ids=(),
            cited_omitted_chunk_ids=(),
            claimed_omitted_fact_ids=(),
            reports_truncation_boundary=False,
            parse_error=str(exc),
        )

    if not isinstance(parsed_completion, Mapping):
        return RLMVanillaBaselineQualityScore(
            schema_version=RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
            scoring_method="truncation_fixture_requirements_v1",
            score=0.0,
            required_field_score=0.0,
            confidence_score=0.0,
            retained_fact_citation_score=0.0,
            truncation_boundary_score=0.0,
            omitted_fact_safety_score=0.0,
            required_fields_present=(),
            required_fields_missing=required_fields,
            cited_retained_fact_ids=(),
            missing_retained_fact_ids=required_fact_ids,
            cited_selected_chunk_ids=(),
            cited_omitted_chunk_ids=(),
            claimed_omitted_fact_ids=(),
            reports_truncation_boundary=False,
            parse_error="Completion JSON must be an object",
        )

    required_fields_present = tuple(
        field_name for field_name in required_fields if field_name in parsed_completion
    )
    required_fields_missing = tuple(
        field_name for field_name in required_fields if field_name not in parsed_completion
    )
    required_field_score = (
        len(required_fields_present) / len(required_fields) if required_fields else 1.0
    )

    confidence_value = parsed_completion.get("confidence")
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, int | float) and not isinstance(confidence_value, bool)
        else None
    )
    minimum_confidence = _minimum_completion_confidence(fixture)
    if confidence is None:
        confidence_score = 0.0
    elif minimum_confidence <= 0:
        confidence_score = 1.0
    else:
        confidence_score = confidence / minimum_confidence

    completion_text = json.dumps(parsed_completion, sort_keys=True)
    evidence_references = _completion_evidence_references(parsed_completion)
    fact_entries = _completion_fact_entries(parsed_completion)
    result_claim_texts = _completion_result_claim_texts(parsed_completion)
    cited_chunk_ids = _completion_evidence_chunk_ids(parsed_completion)
    cited_selected_chunk_ids = tuple(
        chunk_id for chunk_id in selected_chunk_ids if chunk_id in cited_chunk_ids
    )
    cited_omitted_chunk_ids = tuple(
        chunk_id for chunk_id in omitted_chunk_ids if chunk_id in cited_chunk_ids
    )
    cited_retained_fact_ids: list[str] = []
    missing_retained_fact_ids: list[str] = []
    for fact_id in required_fact_ids:
        fact = retained_facts.get(fact_id, {})
        cites_fact = any(
            _entry_supports_fact(
                entry,
                fact_id=fact_id,
                fact=fact,
                require_chunk_match=True,
            )
            for entry in (*fact_entries, *evidence_references)
        )
        if cites_fact:
            cited_retained_fact_ids.append(fact_id)
        else:
            missing_retained_fact_ids.append(fact_id)

    retained_fact_citation_score = (
        len(cited_retained_fact_ids) / len(required_fact_ids) if required_fact_ids else 1.0
    )

    claimed_omitted_fact_ids: list[str] = []
    for fact_id, fact in omitted_facts.items():
        claims_fact = any(
            _entry_supports_fact(
                entry,
                fact_id=fact_id,
                fact=fact,
                require_chunk_match=False,
            )
            for entry in (*fact_entries, *evidence_references)
        )
        claims_fact = claims_fact or any(
            _text_asserts_fact(text, fact_id, fact.get("text")) for text in result_claim_texts
        )
        if claims_fact:
            claimed_omitted_fact_ids.append(fact_id)

    reports_truncation_boundary = _completion_reports_truncation_boundary(
        completion_text,
        fixture,
    )
    truncation_boundary_score = 1.0 if reports_truncation_boundary else 0.0
    omitted_fact_safety_score = (
        1.0 if not cited_omitted_chunk_ids and not claimed_omitted_fact_ids else 0.0
    )

    score = (
        0.20 * _clamp_unit_score(required_field_score)
        + 0.15 * _clamp_unit_score(confidence_score)
        + 0.35 * _clamp_unit_score(retained_fact_citation_score)
        + 0.10 * truncation_boundary_score
        + 0.20 * omitted_fact_safety_score
    )

    return RLMVanillaBaselineQualityScore(
        schema_version=RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
        scoring_method="truncation_fixture_requirements_v1",
        score=_clamp_unit_score(score),
        required_field_score=_clamp_unit_score(required_field_score),
        confidence_score=_clamp_unit_score(confidence_score),
        retained_fact_citation_score=_clamp_unit_score(retained_fact_citation_score),
        truncation_boundary_score=truncation_boundary_score,
        omitted_fact_safety_score=omitted_fact_safety_score,
        required_fields_present=required_fields_present,
        required_fields_missing=required_fields_missing,
        cited_retained_fact_ids=tuple(cited_retained_fact_ids),
        missing_retained_fact_ids=tuple(missing_retained_fact_ids),
        cited_selected_chunk_ids=cited_selected_chunk_ids,
        cited_omitted_chunk_ids=cited_omitted_chunk_ids,
        claimed_omitted_fact_ids=tuple(claimed_omitted_fact_ids),
        reports_truncation_boundary=reports_truncation_boundary,
        confidence=confidence,
    )


def _fixture_target_lines(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    target = _mapping(fixture.get("target"), "target")
    raw_lines = target.get("lines")
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, str):
        msg = "RLM truncation fixture target.lines must be a list of strings"
        raise ValueError(msg)

    lines = tuple(line for line in raw_lines if isinstance(line, str))
    if len(lines) != len(raw_lines):
        msg = "RLM truncation fixture target.lines must contain only strings"
        raise ValueError(msg)

    declared_count = target.get("line_count")
    if isinstance(declared_count, int) and not isinstance(declared_count, bool):
        if declared_count != len(lines):
            msg = "RLM truncation fixture target.line_count does not match target.lines"
            raise ValueError(msg)
    return lines


def _fixture_chunks(
    fixture: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = _mapping(fixture.get("target"), "target")
    truncation_config = _mapping(fixture.get("truncation_config"), "truncation_config")
    source_path = _string(target.get("path"), "target.path")
    lines = _fixture_target_lines(fixture)
    chunk_line_limit = _positive_int(
        truncation_config.get("chunk_line_limit"),
        "truncation_config.chunk_line_limit",
    )
    max_atomic_chunks = _positive_int(
        truncation_config.get("max_atomic_chunks"),
        "truncation_config.max_atomic_chunks",
    )

    chunks: list[dict[str, Any]] = []
    for start_index in range(0, len(lines), chunk_line_limit):
        selected_lines = lines[start_index : start_index + chunk_line_limit]
        start_line = start_index + 1
        end_line = start_index + len(selected_lines)
        content = "\n".join(selected_lines)
        chunks.append(
            {
                "chunk_id": f"{source_path}:{start_line}-{end_line}",
                "source_path": source_path,
                "start_line": start_line,
                "end_line": end_line,
                "content": content,
                "token_estimate": max(1, len(content.split())),
                "truncated": start_index + chunk_line_limit < len(lines),
            }
        )

    selected_chunks = chunks[:max_atomic_chunks]
    omitted_chunks = chunks[max_atomic_chunks:]
    _validate_fixture_chunk_expectations(fixture, selected_chunks, omitted_chunks)
    return selected_chunks, omitted_chunks


def _validate_fixture_chunk_expectations(
    fixture: Mapping[str, Any],
    selected_chunks: Sequence[Mapping[str, Any]],
    omitted_chunks: Sequence[Mapping[str, Any]],
) -> None:
    truncation_config = _mapping(fixture.get("truncation_config"), "truncation_config")
    selected_ids = tuple(str(chunk["chunk_id"]) for chunk in selected_chunks)
    omitted_ids = tuple(str(chunk["chunk_id"]) for chunk in omitted_chunks)
    expected_selected = _string_tuple(truncation_config.get("expected_selected_chunk_ids"))
    expected_omitted = _string_tuple(truncation_config.get("expected_omitted_chunk_ids"))

    if expected_selected and expected_selected != selected_ids:
        msg = "RLM truncation fixture selected chunks do not match truncation_config"
        raise ValueError(msg)
    if expected_omitted and expected_omitted != omitted_ids:
        msg = "RLM truncation fixture omitted chunks do not match truncation_config"
        raise ValueError(msg)


def _public_fixture_metadata(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Return fixture metadata that does not leak omitted target lines."""
    target = _mapping(fixture.get("target"), "target")
    metadata = {
        "schema_version": fixture.get("schema_version"),
        "fixture_id": fixture.get("fixture_id"),
        "description": fixture.get("description"),
        "target": {
            "path": target.get("path"),
            "encoding": target.get("encoding"),
            "line_count": target.get("line_count"),
        },
        "truncation_config": fixture.get("truncation_config"),
        "completion_requirements": fixture.get("completion_requirements", []),
    }
    return metadata


def _build_vanilla_truncation_baseline_prompt(
    fixture: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], tuple[str, ...], int, int, int, str]:
    """Build the single-pass baseline prompt and summary metrics."""
    target = _mapping(fixture.get("target"), "target")
    fixture_id = _string(fixture.get("fixture_id"), "fixture_id")
    target_path = _string(target.get("path"), "target.path")
    selected_chunks, omitted_chunks = _fixture_chunks(fixture)
    selected_chunk_ids = tuple(str(chunk["chunk_id"]) for chunk in selected_chunks)
    omitted_chunk_ids = tuple(str(chunk["chunk_id"]) for chunk in omitted_chunks)
    retained_line_count = sum(
        int(chunk["end_line"]) - int(chunk["start_line"]) + 1 for chunk in selected_chunks
    )
    omitted_line_count = sum(
        int(chunk["end_line"]) - int(chunk["start_line"]) + 1 for chunk in omitted_chunks
    )
    target_line_count = len(_fixture_target_lines(fixture))

    envelope = {
        "schema_version": RLM_VANILLA_BASELINE_INPUT_SCHEMA_VERSION,
        "mode": RLM_VANILLA_BASELINE_MODE,
        "baseline": {
            "baseline_id": RLM_VANILLA_TRUNCATION_BASELINE_ID,
            "fixture_id": fixture_id,
            "single_pass": True,
            "uses_recursive_outer_loop": False,
            "hermes_subcall_budget": 1,
        },
        "call_context": {
            "call_id": RLM_VANILLA_BASELINE_CALL_ID,
            "parent_call_id": None,
            "depth": 0,
        },
        "run": {
            "seed_id": RLM_VANILLA_TRUNCATION_BASELINE_ID,
            "fixture_id": fixture_id,
        },
        "objective": {
            "instruction": (
                "Execute the long-context truncation fixture as a vanilla "
                "single-pass Hermes baseline. Report only facts supported by "
                "retained_chunks and explicitly note that omitted chunks were "
                "outside the retained context."
            ),
            "success_criteria": [
                "Exactly one Hermes RPC call is used",
                "Every retained chunk can be cited by chunk_id",
                "No omitted fact text is claimed as observed evidence",
            ],
        },
        "constraints": {
            "single_pass": True,
            "must_not_call_ouroboros": True,
            "must_not_run_ooo_commands": True,
            "uses_recursive_outer_loop": False,
            "must_use_supplied_context_only": True,
        },
        "context": {
            "fixture": _public_fixture_metadata(fixture),
            "retained_chunks": selected_chunks,
            "retained_facts": fixture.get("expected_retained_facts", []),
            "omitted_chunk_ids": list(omitted_chunk_ids),
            "truncation_boundary": _mapping(
                _mapping(fixture.get("truncation_config"), "truncation_config").get(
                    "truncation_boundary"
                ),
                "truncation_config.truncation_boundary",
            ),
        },
        "trace": {
            "call_id": RLM_VANILLA_BASELINE_CALL_ID,
            "parent_call_id": None,
            "depth": 0,
            "selected_chunk_ids": list(selected_chunk_ids),
            "omitted_chunk_ids": list(omitted_chunk_ids),
            "generated_child_ac_node_ids": [],
        },
        "output_contract": {
            "format": "json",
            "required_fields": [
                "mode",
                "verdict",
                "confidence",
                "result",
                "evidence_references",
                "residual_gaps",
            ],
        },
    }
    return (
        json.dumps(envelope, indent=2, sort_keys=True),
        selected_chunk_ids,
        omitted_chunk_ids,
        retained_line_count,
        omitted_line_count,
        target_line_count,
        target_path,
    )


def _default_vanilla_baseline_result_path(cwd: Path, fixture_id: str) -> Path:
    """Return the default persisted baseline artifact path for later comparison."""
    return cwd / RLM_VANILLA_BASELINE_RESULT_DIR / f"{fixture_id}.json"


def _resolve_vanilla_baseline_result_path(
    config: RLMVanillaTruncationBaselineConfig,
    fixture_id: str,
) -> Path:
    """Resolve an explicit or default result artifact path."""
    if config.result_path is None:
        return _default_vanilla_baseline_result_path(config.cwd, fixture_id)
    if config.result_path.is_absolute():
        return config.result_path
    return config.cwd / config.result_path


def _render_vanilla_baseline_report(result: RLMVanillaTruncationBaselineResult) -> str:
    """Render a compact human-readable baseline report inside the result artifact."""
    return "\n".join(
        [
            "# RLM Vanilla Hermes Baseline",
            "",
            "## Baseline",
            f"- Baseline ID: `{result.baseline_id}`",
            f"- Fixture ID: `{result.fixture_id}`",
            f"- Target: `{result.target_path}`",
            f"- Mode: `{RLM_VANILLA_BASELINE_MODE}`",
            "",
            "## Runner Output",
            f"- Status: `{result.status}`",
            f"- Success: `{result.success}`",
            f"- Hermes sub-calls observed: `{result.hermes_subcall_count}`",
            f"- Output quality score: `{result.output_quality.score:.2f}`",
            f"- Completion hash: `{hash_trace_text(result.completion)}`",
            "",
            "## Context Boundary",
            f"- Retained lines: `{result.retained_line_count}`",
            f"- Omitted lines: `{result.omitted_line_count}`",
            f"- Target lines: `{result.target_line_count}`",
            f"- Selected chunks: `{len(result.selected_chunk_ids)}`",
            f"- Omitted chunks: `{len(result.omitted_chunk_ids)}`",
        ]
    )


def persist_vanilla_truncation_baseline_result(
    result: RLMVanillaTruncationBaselineResult,
    path: Path,
) -> Path:
    """Persist a baseline run in the machine-readable RLM result format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_rlm_result_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _default_vanilla_baseline_runtime(cwd: Path) -> AgentRuntime:
    """Build the default Hermes runtime for the vanilla baseline."""
    from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime

    return HermesCliRuntime(cwd=cwd)


async def run_vanilla_truncation_baseline(
    config: RLMVanillaTruncationBaselineConfig,
) -> RLMVanillaTruncationBaselineResult:
    """Execute the truncation fixture with one vanilla Hermes RPC call."""
    (
        prompt,
        selected_chunk_ids,
        omitted_chunk_ids,
        retained_line_count,
        omitted_line_count,
        target_line_count,
        target_path,
    ) = _build_vanilla_truncation_baseline_prompt(config.fixture)
    fixture_id = _string(config.fixture.get("fixture_id"), "fixture_id")
    hermes_runtime = config.hermes_runtime or _default_vanilla_baseline_runtime(config.cwd)
    started_at = perf_counter()
    hermes_result = await hermes_runtime.execute_task_to_result(
        prompt=prompt,
        tools=[],
        system_prompt=HERMES_VANILLA_BASELINE_SYSTEM_PROMPT,
    )
    elapsed_ms = int((perf_counter() - started_at) * 1000)

    if hermes_result.is_err:
        error = hermes_result.error
        message = error.message if isinstance(error, ProviderError) else str(error)
        raise ValueError(f"Vanilla Hermes truncation baseline failed: {message}") from None

    task_result = hermes_result.value
    result_path = _resolve_vanilla_baseline_result_path(config, fixture_id)
    output_quality = score_vanilla_truncation_baseline_completion(
        config.fixture,
        task_result.final_message,
    )
    result = RLMVanillaTruncationBaselineResult(
        baseline_id=RLM_VANILLA_TRUNCATION_BASELINE_ID,
        fixture_id=fixture_id,
        target_path=target_path,
        status="completed" if task_result.success else "failed",
        success=task_result.success,
        call_id=RLM_VANILLA_BASELINE_CALL_ID,
        prompt=prompt,
        completion=task_result.final_message,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
        retained_line_count=retained_line_count,
        omitted_line_count=omitted_line_count,
        target_line_count=target_line_count,
        elapsed_ms=elapsed_ms,
        output_quality=output_quality,
        error_message=None if task_result.success else task_result.final_message,
        result_path=result_path,
    )
    persist_vanilla_truncation_baseline_result(result, result_path)
    return result


__all__ = [
    "HERMES_VANILLA_BASELINE_SYSTEM_PROMPT",
    "RLM_VANILLA_BASELINE_CALL_ID",
    "RLM_VANILLA_BASELINE_INPUT_SCHEMA_VERSION",
    "RLM_VANILLA_BASELINE_MODE",
    "RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION",
    "RLM_VANILLA_BASELINE_RESULT_ARTIFACT_TYPE",
    "RLM_VANILLA_BASELINE_RESULT_DIR",
    "RLM_VANILLA_TRUNCATION_BASELINE_ID",
    "RLMVanillaBaselineQualityScore",
    "RLMVanillaTruncationBaselineConfig",
    "RLMVanillaTruncationBaselineResult",
    "load_truncation_fixture",
    "persist_vanilla_truncation_baseline_result",
    "run_vanilla_truncation_baseline",
    "score_vanilla_truncation_baseline_completion",
]
