"""Tests for ouroboros.orchestrator.evidence_schema (RFC v2 #830, PR 2)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.evidence_schema import (
    EvidenceError,
    EvidenceRecord,
    extract_evidence,
    validate_evidence,
)
from ouroboros.orchestrator.profile_loader import load_profile


@pytest.fixture
def code_profile():
    return load_profile("code")


@pytest.fixture
def research_profile():
    return load_profile("research")


@pytest.fixture
def analysis_profile():
    return load_profile("analysis")


class TestExtractEvidence:
    def test_bare_json_object(self) -> None:
        record = extract_evidence('{"files_touched": ["a.py"]}')
        assert record.data == {"files_touched": ["a.py"]}

    def test_fenced_json_block(self) -> None:
        text = 'summary line\n```json\n{"x": 1}\n```\ntrailing\n'
        record = extract_evidence(text)
        assert record.data == {"x": 1}

    def test_fenced_block_without_lang_tag(self) -> None:
        record = extract_evidence('prelude\n```\n{"y": 2}\n```\n')
        assert record.data == {"y": 2}

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(EvidenceError, match="empty"):
            extract_evidence("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(EvidenceError, match="empty"):
            extract_evidence("   \n\t  ")

    def test_malformed_json(self) -> None:
        with pytest.raises(EvidenceError, match="not valid JSON"):
            extract_evidence("{not: json}")

    def test_non_object_payload(self) -> None:
        with pytest.raises(EvidenceError, match="must be a JSON object"):
            extract_evidence("[1, 2, 3]")


class TestValidateCodeProfile:
    def test_accepts_complete_record(self, code_profile) -> None:
        record = EvidenceRecord(
            data={
                "files_touched": ["src/a.py"],
                "commands_run": ["pytest"],
                "tests_passed": ["test_a"],
            }
        )
        result = validate_evidence(code_profile, record)
        assert result.ok is True
        assert result.missing_fields == ()
        assert result.rejected_by == ()

    def test_rejects_empty_tests_passed(self, code_profile) -> None:
        record = EvidenceRecord(
            data={
                "files_touched": ["src/a.py"],
                "commands_run": ["pytest"],
                "tests_passed": [],
            }
        )
        result = validate_evidence(code_profile, record)
        assert result.ok is False
        assert result.rejected_by == ("tests_passed == []",)
        assert result.missing_fields == ()

    def test_reports_missing_fields(self, code_profile) -> None:
        record = EvidenceRecord(data={"files_touched": ["a.py"]})
        result = validate_evidence(code_profile, record)
        assert result.ok is False
        assert "commands_run" in result.missing_fields
        assert "tests_passed" in result.missing_fields

    def test_reasons_summarize_failures(self, code_profile) -> None:
        record = EvidenceRecord(data={"tests_passed": []})
        result = validate_evidence(code_profile, record)
        reasons = result.reasons()
        assert any("missing required fields" in r for r in reasons)
        assert any("tests_passed == []" in r for r in reasons)


class TestValidateResearchProfile:
    def test_accepts_triangulated(self, research_profile) -> None:
        record = EvidenceRecord(
            data={
                "external_sources": ["https://example.com/a"],
                "claims": [{"text": "x", "source": 0}],
                "triangulated_sources": ["https://example.com/a", "https://example.com/b"],
            }
        )
        result = validate_evidence(research_profile, record)
        assert result.ok is True

    def test_rejects_no_external_sources(self, research_profile) -> None:
        record = EvidenceRecord(
            data={
                "external_sources": [],
                "claims": [],
                "triangulated_sources": [],
            }
        )
        result = validate_evidence(research_profile, record)
        assert result.ok is False
        assert "external_sources == []" in result.rejected_by
        assert "triangulated_sources == []" in result.rejected_by


class TestValidateAnalysisProfile:
    def test_accepts_perspectives(self, analysis_profile) -> None:
        record = EvidenceRecord(
            data={
                "claims": [{"text": "x"}],
                "perspectives_compared": ["pro", "con"],
            }
        )
        result = validate_evidence(analysis_profile, record)
        assert result.ok is True

    def test_rejects_one_sided(self, analysis_profile) -> None:
        record = EvidenceRecord(
            data={
                "claims": [{"text": "x"}],
                "perspectives_compared": [],
            }
        )
        result = validate_evidence(analysis_profile, record)
        assert result.ok is False
        assert result.rejected_by == ("perspectives_compared == []",)


class TestRejectionGrammar:
    """rejected_if grammar is intentionally narrow; bad expressions must surface."""

    def test_unsupported_expression_raises(
        self, code_profile, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        broken = code_profile.model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("len(tests_passed) < 1",),
                )
            }
        )
        record = EvidenceRecord(data={"tests_passed": [1]})
        with pytest.raises(EvidenceError, match="Unsupported rejected_if"):
            validate_evidence(broken, record)

    def test_unsupported_literal_raises(self, code_profile) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        broken = code_profile.model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("tests_passed == os.system",),
                )
            }
        )
        record = EvidenceRecord(data={"tests_passed": []})
        with pytest.raises(EvidenceError, match="Unsupported literal"):
            validate_evidence(broken, record)

    def test_missing_field_compared_to_none_triggers(self) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        profile = load_profile("code").model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=(),
                    rejected_if=("never_emitted == None",),
                )
            }
        )
        record = EvidenceRecord(data={})
        result = validate_evidence(profile, record)
        assert result.ok is False
        assert result.rejected_by == ("never_emitted == None",)
