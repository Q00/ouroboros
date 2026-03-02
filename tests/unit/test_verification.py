"""Unit tests for spec verification — models, extractor, verifier."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse
from ouroboros.verification.extractor import AssertionExtractor
from ouroboros.verification.models import (
    ACVerificationReport,
    SpecAssertion,
    SpecVerificationResult,
    SpecVerificationSummary,
    VerificationTier,
)
from ouroboros.verification.verifier import SpecVerifier

# -- Model Tests --


class TestVerificationModels:
    """Tests for verification data models."""

    def test_spec_assertion_frozen(self) -> None:
        a = SpecAssertion(
            ac_index=0,
            ac_text="WARMUP_FRAMES=10",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"WARMUP_FRAMES\s*=\s*",
            expected_value="10",
        )
        assert a.ac_index == 0
        assert a.tier == VerificationTier.T1_CONSTANT
        with pytest.raises(Exception):
            a.ac_index = 1  # type: ignore[misc]

    def test_verification_result_discrepancy(self) -> None:
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="test",
            tier=VerificationTier.T1_CONSTANT,
        )
        r = SpecVerificationResult(
            assertion=assertion,
            verified=False,
            actual_value="30",
            discrepancy=True,
        )
        assert r.discrepancy
        assert not r.verified

    def test_ac_report_verified_pass_all_pass(self) -> None:
        assertion = SpecAssertion(ac_index=0, ac_text="test", tier=VerificationTier.T1_CONSTANT)
        report = ACVerificationReport(
            ac_index=0,
            ac_text="test",
            results=(
                SpecVerificationResult(assertion=assertion, verified=True),
                SpecVerificationResult(assertion=assertion, verified=True),
            ),
            agent_reported_pass=True,
        )
        assert report.verified_pass
        assert not report.has_discrepancy

    def test_ac_report_has_discrepancy(self) -> None:
        assertion = SpecAssertion(ac_index=0, ac_text="test", tier=VerificationTier.T1_CONSTANT)
        report = ACVerificationReport(
            ac_index=0,
            ac_text="test",
            results=(
                SpecVerificationResult(assertion=assertion, verified=False, discrepancy=True),
            ),
            agent_reported_pass=True,
        )
        assert not report.verified_pass
        assert report.has_discrepancy

    def test_ac_report_no_results_trusts_agent(self) -> None:
        """No assertions extracted → trust agent's self-report."""
        report = ACVerificationReport(
            ac_index=0,
            ac_text="UX feels natural",
            results=(),
            agent_reported_pass=True,
        )
        assert report.verified_pass
        assert not report.has_discrepancy

    def test_summary_from_reports(self) -> None:
        assertion = SpecAssertion(ac_index=0, ac_text="test", tier=VerificationTier.T1_CONSTANT)
        reports = (
            ACVerificationReport(
                ac_index=0,
                ac_text="test1",
                results=(SpecVerificationResult(assertion=assertion, verified=True),),
                agent_reported_pass=True,
            ),
            ACVerificationReport(
                ac_index=1,
                ac_text="test2",
                results=(
                    SpecVerificationResult(assertion=assertion, verified=False, discrepancy=True),
                ),
                agent_reported_pass=True,
            ),
            ACVerificationReport(
                ac_index=2,
                ac_text="subjective",
                results=(),
                agent_reported_pass=True,
            ),
        )
        summary = SpecVerificationSummary.from_reports(reports)
        assert summary.total_assertions == 2
        assert summary.verified_count == 1
        assert summary.failed_count == 1
        assert summary.skipped_count == 1
        assert summary.discrepancy_count == 1
        assert summary.has_discrepancies
        assert summary.override_approval is False

    def test_summary_no_discrepancies(self) -> None:
        assertion = SpecAssertion(ac_index=0, ac_text="test", tier=VerificationTier.T1_CONSTANT)
        reports = (
            ACVerificationReport(
                ac_index=0,
                ac_text="test",
                results=(SpecVerificationResult(assertion=assertion, verified=True),),
                agent_reported_pass=True,
            ),
        )
        summary = SpecVerificationSummary.from_reports(reports)
        assert not summary.has_discrepancies
        assert summary.override_approval is None


# -- Verifier Tests --


class TestSpecVerifier:
    """Tests for SpecVerifier file-based verification."""

    def _create_project(self, files: dict[str, str]) -> str:
        """Create a temp project directory with given files."""
        tmpdir = tempfile.mkdtemp()
        for name, content in files.items():
            path = os.path.join(tmpdir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        # Create pyproject.toml so project root is found
        with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "test"\n')
        return tmpdir

    def test_t1_constant_found_correct(self) -> None:
        """T1: expected value matches actual → verified."""
        project = self._create_project(
            {
                "config.py": "WARMUP_FRAMES = 10\nFPS = 60\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Warmup frames should be 10",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"WARMUP_FRAMES\s*=\s*",
            expected_value="10",
            file_hint="*.py",
        )
        summary = verifier.verify_all((assertion,))
        assert summary.verified_count == 1
        assert summary.failed_count == 0

    def test_t1_constant_found_wrong_value(self) -> None:
        """T1: expected 10 but found 30 → discrepancy."""
        project = self._create_project(
            {
                "config.py": "WARMUP_FRAMES = 30\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Warmup frames should be 10",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"WARMUP_FRAMES\s*=\s*",
            expected_value="10",
            file_hint="*.py",
        )
        summary = verifier.verify_all((assertion,), agent_results={0: True})
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1
        assert summary.reports[0].has_discrepancy

    def test_t1_pattern_not_found(self) -> None:
        """T1: pattern not in any file → verification fails."""
        project = self._create_project(
            {
                "main.py": "print('hello')\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MAX_RETRIES=5",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"MAX_RETRIES\s*=\s*",
            expected_value="5",
            file_hint="*.py",
        )
        summary = verifier.verify_all((assertion,), agent_results={0: True})
        assert summary.failed_count == 1

    def test_t2_structural_class_found(self) -> None:
        """T2: class exists in source → verified."""
        project = self._create_project(
            {
                "provider.py": "class CameraProvider:\n    pass\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="CameraProvider interface",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=r"class CameraProvider",
            file_hint="*.py",
        )
        summary = verifier.verify_all((assertion,))
        assert summary.verified_count == 1

    def test_t2_structural_missing(self) -> None:
        """T2: required class not found → fails."""
        project = self._create_project(
            {
                "main.py": "class SomethingElse:\n    pass\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="CameraProvider interface",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=r"class CameraProvider",
            file_hint="*.py",
        )
        summary = verifier.verify_all((assertion,), agent_results={0: True})
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1

    def test_t3_t4_skipped(self) -> None:
        """T3 and T4 assertions are skipped (no results)."""
        project = self._create_project({"main.py": ""})
        verifier = SpecVerifier(project_dir=project)
        assertions = (
            SpecAssertion(ac_index=0, ac_text="behavioral", tier=VerificationTier.T3_BEHAVIORAL),
            SpecAssertion(ac_index=1, ac_text="subjective", tier=VerificationTier.T4_UNVERIFIABLE),
        )
        summary = verifier.verify_all(assertions)
        assert summary.total_assertions == 0
        assert summary.skipped_count == 2

    def test_no_files_match_hint(self) -> None:
        """File hint matches nothing → trust agent (verified=True)."""
        project = self._create_project({"main.py": ""})
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="test",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"FOO",
            expected_value="bar",
            file_hint="*.rs",
        )
        summary = verifier.verify_all((assertion,))
        assert summary.verified_count == 1  # Trust agent when can't verify

    def test_multiple_assertions_per_ac(self) -> None:
        """Multiple assertions for one AC — all must pass."""
        project = self._create_project(
            {
                "config.py": "WARMUP = 10\nFPS = 60\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertions = (
            SpecAssertion(
                ac_index=0,
                ac_text="Config values",
                tier=VerificationTier.T1_CONSTANT,
                pattern=r"WARMUP\s*=\s*",
                expected_value="10",
                file_hint="*.py",
            ),
            SpecAssertion(
                ac_index=0,
                ac_text="Config values",
                tier=VerificationTier.T1_CONSTANT,
                pattern=r"FPS\s*=\s*",
                expected_value="30",  # Wrong!
                file_hint="*.py",
            ),
        )
        summary = verifier.verify_all(assertions, agent_results={0: True})
        assert summary.reports[0].has_discrepancy  # One of two failed

    def test_pycache_excluded(self) -> None:
        """__pycache__ directories are excluded from search."""
        project = self._create_project(
            {
                "__pycache__/cached.py": "WARMUP = 999\n",
                "config.py": "WARMUP = 10\n",
            }
        )
        verifier = SpecVerifier(project_dir=project)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="test",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"WARMUP\s*=\s*",
            expected_value="10",
            file_hint="**/*.py",
        )
        summary = verifier.verify_all((assertion,))
        assert summary.verified_count == 1


# -- Extractor Tests --


class TestAssertionExtractor:
    """Tests for LLM-based assertion extraction."""

    def _make_extractor(self, response_json: list[dict]) -> AssertionExtractor:
        """Create extractor with mocked LLM that returns given JSON."""
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                CompletionResponse(
                    content=json.dumps(response_json),
                    model="test",
                    usage={"input": 0, "output": 0},
                )
            )
        )
        return AssertionExtractor(llm_adapter=mock_adapter)

    @pytest.mark.asyncio
    async def test_extracts_t1_assertion(self) -> None:
        """Extractor produces T1 assertion from LLM response."""
        extractor = self._make_extractor(
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": r"WARMUP_FRAMES\s*=\s*",
                    "expected_value": "10",
                    "file_hint": "*.py",
                    "description": "Warmup frames check",
                }
            ]
        )
        result = await extractor.extract("seed_1", ("WARMUP_FRAMES should be 10",))
        assert result.is_ok
        assertions = result.value
        assert len(assertions) == 1
        assert assertions[0].tier == VerificationTier.T1_CONSTANT
        assert assertions[0].expected_value == "10"

    @pytest.mark.asyncio
    async def test_caches_by_seed_id(self) -> None:
        """Second call with same seed_id returns cached results."""
        extractor = self._make_extractor(
            [
                {
                    "ac_index": 0,
                    "tier": "t2_structural",
                    "pattern": "class Foo",
                    "expected_value": "",
                    "file_hint": "*.py",
                    "description": "",
                }
            ]
        )
        r1 = await extractor.extract("seed_cache", ("Has class Foo",))
        r2 = await extractor.extract("seed_cache", ("Has class Foo",))
        assert r1.is_ok and r2.is_ok
        assert r1.value is r2.value
        # LLM called only once
        extractor.llm_adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_error(self) -> None:
        """LLM failure → Result.err."""
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(return_value=Result.err("timeout"))
        extractor = AssertionExtractor(llm_adapter=mock_adapter)
        result = await extractor.extract("seed_fail", ("test",))
        assert result.is_err

    @pytest.mark.asyncio
    async def test_empty_acs_returns_empty(self) -> None:
        """No ACs → empty tuple, no LLM call."""
        mock_adapter = AsyncMock()
        extractor = AssertionExtractor(llm_adapter=mock_adapter)
        result = await extractor.extract("seed_empty", ())
        assert result.is_ok
        assert result.value == ()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_empty(self) -> None:
        """Malformed LLM response → empty assertions, no crash."""
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                CompletionResponse(
                    content="this is not json",
                    model="test",
                    usage={"input": 0, "output": 0},
                )
            )
        )
        extractor = AssertionExtractor(llm_adapter=mock_adapter)
        result = await extractor.extract("seed_bad", ("test",))
        assert result.is_ok
        assert result.value == ()

    @pytest.mark.asyncio
    async def test_invalid_tier_defaults_to_t4(self) -> None:
        """Unknown tier string → defaults to T4_UNVERIFIABLE."""
        extractor = self._make_extractor(
            [
                {
                    "ac_index": 0,
                    "tier": "invalid_tier",
                    "pattern": "",
                    "expected_value": "",
                    "file_hint": "",
                    "description": "",
                }
            ]
        )
        result = await extractor.extract("seed_tier", ("test",))
        assert result.is_ok
        assert result.value[0].tier == VerificationTier.T4_UNVERIFIABLE
