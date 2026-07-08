"""Acceptance-criterion evidence schema classification helpers."""

from __future__ import annotations

import re
from typing import Any

from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.profile_loader import EvidenceSchema, ExecutionProfile

_DOC_ONLY_TARGET_RE = re.compile(
    r"\b(readme(?:\.md)?|docs?/|docs?\.[a-z0-9_-]+|documentation|guide|manual|changelog)\b",
    re.IGNORECASE,
)
_DOC_ONLY_ACTION_RE = re.compile(
    r"\b(document|describe|explain|add|update|create|fix|write|improve)\b",
    re.IGNORECASE,
)
_CODE_IMPLEMENTATION_ACTION_RE = re.compile(
    r"\b(implement|build|develop|ship)\b",
    re.IGNORECASE,
)
_CODE_MUTATION_ACTION_RE = re.compile(
    r"\b(add(?:ing)?|fix(?:ing)?|create|creating|update|updating|change|changing|modify|modifying|refactor(?:ing)?|repair(?:ing)?)\b",
    re.IGNORECASE,
)
_CODE_WORK_SIGNAL_RE = re.compile(
    r"("
    r"\b[a-zA-Z_][a-zA-Z0-9_]*\s*\("
    r"|"
    r"\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|c|cc|cpp|h|hpp|swift|rb|php|sh|zsh|fish)\b"
    r"|"
    r"\b(parser|function|module|api|endpoint|class|method|cli\s+flag|flag|command|"
    r"bug|runtime|workflow|validator|validation|implementation)\b"
    r")",
    re.IGNORECASE,
)
_TEST_WORK_RE = re.compile(
    r"("
    r"\b(?:run|execute|pass|validate|verify)\b.{0,40}\b(?:pytest|tests?|unit\s+tests?|integration\s+tests?)\b"
    r"|"
    r"\b(?:add|write|create|implement|fix|update)\b.{0,40}\b"
    r"(?:tests?|unit\s+tests?|integration\s+tests?)\b"
    r"(?!\s+(?:guide|docs?|documentation|setup))"
    r"|"
    r"\b(?:pytest|tests_passed|test\s+command)\b"
    r")",
    re.IGNORECASE,
)
_TEST_MUTATION_WORK_RE = re.compile(
    r"("
    r"\b(?:add|write|create|implement|fix|update|extend|expand)\b.{0,60}\b"
    r"(?:tests?|unit\s+tests?|integration\s+tests?|coverage|test_[\w.-]+\.py)\b"
    r"|"
    r"\b(?:tests?|unit\s+tests?|integration\s+tests?|test_[\w.-]+\.py)\b.{0,60}\b"
    r"(?:cover|coverage)\b"
    r"|"
    r"\bcheck(?:ed)?\s+(?:(?:the|existing|current|new|added|updated)\s+){0,3}"
    r"(?:tests?|unit\s+tests?|integration\s+tests?|test_[\w.-]+\.py)"
    r"\s+into\b"
    r"|"
    r"\bcheck\s+in\b.{0,60}\b"
    r"(?:tests?|unit\s+tests?|integration\s+tests?|test_[\w.-]+\.py)\b"
    r")",
    re.IGNORECASE,
)
_DOCS_TEST_REFERENCE_RE = re.compile(
    r"("
    r"\b(?:document(?:ing)?|guide|manual|instructions?|usage|setup|how\s+to|verification)\b"
    r".{0,100}\b(?:test\s+command|python\s+-m\s+unittest|pytest|tests?|unit\s+tests?|test\s+setup)\b"
    r"|"
    r"\b(?:test\s+command|python\s+-m\s+unittest|pytest|tests?|unit\s+tests?|test\s+setup)\b"
    r".{0,100}\b(?:document(?:ing)?|guide|manual|instructions?|usage|setup|how\s+to|verification)\b"
    r")",
    re.IGNORECASE,
)
_NO_MUTATION_VALIDATION_RE = re.compile(
    r"("
    r"\bwithout\s+(?:modifying|changing|editing|writing|updating|touching)\b"
    r"|"
    r"\bwith\s+no\s+(?:file|code)?\s*(?:modifications?|changes?|edits?|updates?)\b"
    r"|"
    r"\bno\s+(?:file|code)?\s*(?:modifications?|changes?|edits?|updates?)\b"
    r"|"
    r"\bdo\s+not\s+(?:modify|change|edit|write|update|touch)\b"
    r")",
    re.IGNORECASE,
)
_EXISTING_VALIDATION_RE = re.compile(
    r"\b(?:existing|current|already(?:-|\s+)?satisfied|already(?:-|\s+)?implemented)\b",
    re.IGNORECASE,
)
_VALIDATION_ONLY_ACTION_RE = re.compile(
    r"\b(?:run|execute|pass|validate|verify|ensure|confirm|check)\b",
    re.IGNORECASE,
)
_VALIDATION_ONLY_TEST_SIGNAL_RE = re.compile(
    r"\b(?:pytest|unit\s+tests?|integration\s+tests?|tests?|test suite|"
    r"test_[\w.-]+\.py|python\s+-m\s+unittest)\b",
    re.IGNORECASE,
)


def _has_mixed_code_and_documentation_work(ac_content: str) -> bool:
    """Return True when one AC appears to combine code mutation and docs work."""
    for connector in re.finditer(
        r"\b(?:and|then|while|plus)\b|[,;:]",
        ac_content,
        re.IGNORECASE,
    ):
        before = ac_content[: connector.start()]
        after = ac_content[connector.end() :]
        before_has_docs = bool(_DOC_ONLY_TARGET_RE.search(before))
        after_has_docs = bool(_DOC_ONLY_TARGET_RE.search(after))
        before_has_code_work = bool(
            _CODE_MUTATION_ACTION_RE.search(before) and _CODE_WORK_SIGNAL_RE.search(before)
        )
        after_has_code_work = bool(
            _CODE_MUTATION_ACTION_RE.search(after) and _CODE_WORK_SIGNAL_RE.search(after)
        )
        if after_has_docs and not before_has_docs and before_has_code_work:
            return True
        if before_has_docs and not after_has_docs and after_has_code_work:
            return True
    return False


def _has_mixed_test_and_documentation_work(ac_content: str) -> bool:
    """Return True when one AC appears to combine test mutation and docs work."""
    for connector in re.finditer(
        r"\b(?:and|then|while|plus)\b|[,;:]",
        ac_content,
        re.IGNORECASE,
    ):
        before = ac_content[: connector.start()]
        after = ac_content[connector.end() :]
        before_has_docs = bool(_DOC_ONLY_TARGET_RE.search(before))
        after_has_docs = bool(_DOC_ONLY_TARGET_RE.search(after))
        before_has_test_work = bool(_TEST_MUTATION_WORK_RE.search(before))
        after_has_test_work = bool(_TEST_MUTATION_WORK_RE.search(after))
        if after_has_docs and not before_has_docs and before_has_test_work:
            return True
        if before_has_docs and not after_has_docs and after_has_test_work:
            return True
    return False


def _has_mixed_validation_and_documentation_work(ac_content: str) -> bool:
    """Return True when one AC appears to combine docs work and test execution."""
    for connector in re.finditer(
        r"\b(?:and|then|while|plus)\b|[,;:]",
        ac_content,
        re.IGNORECASE,
    ):
        before = ac_content[: connector.start()]
        after = ac_content[connector.end() :]
        before_has_docs = bool(_DOC_ONLY_TARGET_RE.search(before))
        after_has_docs = bool(_DOC_ONLY_TARGET_RE.search(after))
        before_has_validation = bool(
            _VALIDATION_ONLY_ACTION_RE.search(before)
            and _VALIDATION_ONLY_TEST_SIGNAL_RE.search(before)
        )
        after_has_validation = bool(
            _VALIDATION_ONLY_ACTION_RE.search(after)
            and _VALIDATION_ONLY_TEST_SIGNAL_RE.search(after)
        )
        if after_has_docs and not before_has_docs and before_has_validation:
            return True
        if before_has_docs and not after_has_docs and after_has_validation:
            return True
    return False


def _is_documentation_only_ac(ac_content: str) -> bool:
    """Return True when an AC asks only for documentation/README work.

    The code profile normally requires runnable test evidence. Normal usage can
    still include a final docs-only AC (for example README usage examples after
    code ACs already passed). Such an AC should be verified by docs evidence
    from the current runtime session, not by re-claiming prior code test IDs.
    """
    normalized = " ".join(ac_content.split())
    if not normalized:
        return False
    has_docs_target = bool(_DOC_ONLY_TARGET_RE.search(normalized))
    has_docs_action = bool(_DOC_ONLY_ACTION_RE.search(normalized))
    documents_test_reference = (
        has_docs_target and has_docs_action and bool(_DOCS_TEST_REFERENCE_RE.search(normalized))
    )
    if documents_test_reference and _has_mixed_validation_and_documentation_work(normalized):
        return False
    if _TEST_MUTATION_WORK_RE.search(normalized) and (
        not documents_test_reference or _has_mixed_test_and_documentation_work(normalized)
    ):
        return False
    if _TEST_WORK_RE.search(normalized) and not documents_test_reference:
        return False
    if _CODE_IMPLEMENTATION_ACTION_RE.search(normalized):
        return False
    if _has_mixed_code_and_documentation_work(normalized):
        return False
    if (
        re.search(r"\bdocumentation\b", normalized, re.IGNORECASE)
        and _CODE_MUTATION_ACTION_RE.search(normalized)
        and _CODE_WORK_SIGNAL_RE.search(normalized)
        and not re.search(
            r"\b(readme(?:\.md)?|docs?/|docs?\.[a-z0-9_-]+|guide|manual|changelog)\b",
            normalized,
            re.IGNORECASE,
        )
    ):
        return False
    return has_docs_target and has_docs_action


def _is_validation_only_ac(ac_content: str) -> bool:
    """Return True when an AC asks only to run or verify tests.

    Test-writing ACs still require ``files_touched``; validation-only ACs are
    allowed to prove completion with command/test evidence and no file mutation.
    """
    normalized = " ".join(ac_content.split())
    if not normalized:
        return False
    if _is_documentation_only_ac(normalized):
        return False
    if _DOC_ONLY_TARGET_RE.search(normalized) and _DOC_ONLY_ACTION_RE.search(normalized):
        return False
    stripped_no_mutation_terms = _NO_MUTATION_VALIDATION_RE.sub("", normalized)
    if _TEST_MUTATION_WORK_RE.search(normalized):
        return False
    if _CODE_MUTATION_ACTION_RE.search(stripped_no_mutation_terms) and _CODE_WORK_SIGNAL_RE.search(
        stripped_no_mutation_terms
    ):
        return False
    if _CODE_IMPLEMENTATION_ACTION_RE.search(stripped_no_mutation_terms):
        return False
    if _has_mixed_code_and_documentation_work(stripped_no_mutation_terms):
        return False
    if (
        (
            _NO_MUTATION_VALIDATION_RE.search(normalized)
            or _EXISTING_VALIDATION_RE.search(normalized)
        )
        and _VALIDATION_ONLY_ACTION_RE.search(normalized)
        and _VALIDATION_ONLY_TEST_SIGNAL_RE.search(normalized)
    ):
        return True
    return bool(_VALIDATION_ONLY_ACTION_RE.search(normalized)) and bool(
        _VALIDATION_ONLY_TEST_SIGNAL_RE.search(normalized)
    )


def _drop_required_evidence_field(schema: EvidenceSchema, field: str) -> EvidenceSchema:
    """Return a schema copy with ``field`` removed from required and rejected_if."""
    required = tuple(name for name in schema.required if name != field)
    rejected_if = tuple(expr for expr in schema.rejected_if if not expr.strip().startswith(field))
    return EvidenceSchema(required=required, rejected_if=rejected_if)


def _effective_evidence_schema_for_ac(
    profile: ExecutionProfile,
    ac_content: str,
    *,
    has_success_contract: bool = False,
    has_expected_artifacts: bool = False,
    verify_gate_active: bool = False,
) -> EvidenceSchema:
    """Return the active evidence schema for one atomic AC dispatch.

    ``has_success_contract`` is True when the AC declares a ``verify_command``.
    For such ACs ``tests_passed`` is dropped from the required evidence set: the
    orchestrator itself runs the declared command via ``_run_ac_verify_gate``
    (real subprocess, real exit code + output_assertion), which is an
    authoritative, non-fabricatable check — strictly stronger than reconstructing
    "did a test pass" from the worker's transcript. Legacy ACs (no
    ``verify_command``) keep ``tests_passed`` required and verified as before.

    ``has_expected_artifacts`` is True when the AC declares filesystem artifacts.
    For contract ACs that also declare artifacts, ``files_touched`` is likewise
    delegated to the verify gate's real filesystem existence check. Contract ACs
    without artifacts keep transcript-backed ``files_touched`` evidence because
    there is no filesystem oracle to replace it.

    ``verify_gate_active`` is True only when the orchestrator verify gate is
    actually enabled (``run_verify_commands``). Delegation of ``tests_passed`` /
    ``files_touched`` to the gate is *only* valid when the gate will run. When an
    operator disables verify commands, the gate that would replace the transcript
    check never fires (``_apply_verify_gate`` returns early), so we retain the
    transcript-backed evidence — the pre-delegation behavior — as a fail-safe.
    The default is ``False`` so we never delegate unless explicitly told the gate
    is active. The content-based ``_is_validation_only_ac`` /
    ``_is_documentation_only_ac`` drops are unaffected — those are not gate
    delegations.
    """
    schema = profile.evidence_schema
    if _is_validation_only_ac(ac_content) and "files_touched" in schema.required:
        schema = _drop_required_evidence_field(schema, "files_touched")
    elif _is_documentation_only_ac(ac_content) and "tests_passed" in schema.required:
        schema = _drop_required_evidence_field(schema, "tests_passed")
    if has_success_contract and verify_gate_active and "tests_passed" in schema.required:
        schema = _drop_required_evidence_field(schema, "tests_passed")
    if (
        has_success_contract
        and has_expected_artifacts
        and verify_gate_active
        and "files_touched" in schema.required
    ):
        schema = _drop_required_evidence_field(schema, "files_touched")
    return schema


def _out_of_scope_evidence_fields_for_ac(
    profile: ExecutionProfile,
    ac_content: str,
    record: EvidenceRecord | None,
    *,
    has_success_contract: bool = False,
    has_expected_artifacts: bool = False,
    verify_gate_active: bool = False,
) -> tuple[str, ...]:
    """Return non-empty evidence fields excluded by the AC-specific schema."""
    if record is None:
        return ()
    effective_schema = _effective_evidence_schema_for_ac(
        profile,
        ac_content,
        has_success_contract=has_success_contract,
        has_expected_artifacts=has_expected_artifacts,
        verify_gate_active=verify_gate_active,
    )
    required_fields = set(effective_schema.required)
    return tuple(
        field
        for field in profile.evidence_schema.required
        if field not in required_fields and _flatten_evidence_values(record.get(field))
    )


def _out_of_scope_evidence_values_for_ac(
    profile: ExecutionProfile,
    ac_content: str,
    record: EvidenceRecord | None,
    *,
    has_success_contract: bool = False,
    has_expected_artifacts: bool = False,
    verify_gate_active: bool = False,
) -> dict[str, Any]:
    """Return out-of-scope evidence values retained for audit metadata only."""
    if record is None:
        return {}
    fields = _out_of_scope_evidence_fields_for_ac(
        profile,
        ac_content,
        record,
        has_success_contract=has_success_contract,
        has_expected_artifacts=has_expected_artifacts,
        verify_gate_active=verify_gate_active,
    )
    return {field: record.data[field] for field in fields if field in record.data}


def _scoped_evidence_record_for_ac(
    profile: ExecutionProfile,
    ac_content: str,
    record: EvidenceRecord,
    *,
    has_success_contract: bool = False,
    has_expected_artifacts: bool = False,
    verify_gate_active: bool = False,
) -> EvidenceRecord:
    """Return only evidence fields inside the AC-specific schema."""
    effective_schema = _effective_evidence_schema_for_ac(
        profile,
        ac_content,
        has_success_contract=has_success_contract,
        has_expected_artifacts=has_expected_artifacts,
        verify_gate_active=verify_gate_active,
    )
    allowed_fields = set(effective_schema.required)
    return EvidenceRecord(
        data={field: value for field, value in record.data.items() if field in allowed_fields},
        source=record.source,
    )


def _profile_with_evidence_schema(
    profile: ExecutionProfile,
    schema: EvidenceSchema,
) -> ExecutionProfile:
    """Return a shallow profile copy using an AC-specific evidence schema."""
    required_fields = set(schema.required)
    must_produce = tuple(field for field in profile.must_produce if field in required_fields)
    return profile.model_copy(update={"evidence_schema": schema, "must_produce": must_produce})
