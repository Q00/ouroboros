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

    def test_t1_constant_preserves_quoted_multiword_value(self) -> None:
        """Exact comparison retains the complete contents of quoted scalars."""
        project = self._create_project({"config.py": 'GREETING = "hello world"\n'})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Greeting should be hello world",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"GREETING\s*=\s*",
            expected_value="hello world",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all((assertion,))

        assert summary.verified_count == 1
        assert summary.failed_count == 0

    @pytest.mark.parametrize(
        "source_value",
        [
            '"foo" + "bar"',
            "'foo' 'bar'",
            '"foo".strip()',
            '"foo" // 2',
        ],
        ids=[
            "binary-concatenation",
            "implicit-concatenation",
            "method-expression",
            "floor-division-expression",
        ],
    )
    def test_t1_constant_rejects_quoted_scalar_expression_prefix(self, source_value: str) -> None:
        project = self._create_project({"config.py": f"NAME = {source_value}\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Name should be foo",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"NAME\s*=\s*",
            expected_value="foo",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1

    @pytest.mark.parametrize(
        "suffix",
        ["", "   ", " # source comment", ";"],
        ids=["line-end", "whitespace-line-end", "comment", "statement-terminator"],
    )
    def test_t1_constant_accepts_complete_quoted_scalar_terminator(self, suffix: str) -> None:
        project = self._create_project({"config.py": f'NAME = "foo"{suffix}\n'})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Name should be foo",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"NAME\s*=\s*",
            expected_value="foo",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all((assertion,))

        assert summary.verified_count == 1
        assert summary.failed_count == 0

    @pytest.mark.parametrize(
        ("source_value", "expected_value"),
        [
            (r"hello \"world\"", 'hello "world"'),
            ("x" * 110, "x" * 110),
        ],
        ids=["escaped-quote", "beyond-old-lookahead"],
    )
    def test_t1_constant_preserves_complete_quoted_value(
        self,
        source_value: str,
        expected_value: str,
    ) -> None:
        project = self._create_project({"config.py": f'GREETING = "{source_value}"\n'})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Greeting must match",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"GREETING\s*=\s*",
            expected_value=expected_value,
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all((assertion,))

        assert summary.verified_count == 1
        assert summary.failed_count == 0

    @pytest.mark.parametrize("actual_value", ["50", "15"])
    def test_t1_constant_rejects_prefix_and_suffix_collisions(self, actual_value: str) -> None:
        """Expected constants require exact extracted-value equality."""
        project = self._create_project({"config.py": f"MAX_RETRIES = {actual_value}\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MAX_RETRIES should be 5",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"MAX_RETRIES\s*",
            expected_value="5",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1

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
        """File hint matches nothing → verification fails closed."""
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
        summary = verifier.verify_all((assertion,), agent_results={0: True})
        assert summary.verified_count == 0
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1

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

    # A pattern that matches the empty string can succeed without any
    # criterion-specific content, so it verifies whatever it is pointed at. All of
    # these compile, which is the only question the gate used to ask.
    #
    # Split by which subject exposes each one: the patterns below also match ordinary
    # non-empty files, while `\A\Z` succeeds only against an empty one — so proving
    # that case needs a project that has such a file.
    @pytest.mark.parametrize("pattern", [".*", "x?", r"\s*", "(?:)", "|", "^"])
    def test_t2_pattern_matching_any_input_is_not_evidence(self, pattern: str) -> None:
        """Such a pattern must not verify an AC the project does not satisfy."""
        project = self._create_project({"main.py": "print('hello')\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST define a CameraProvider interface",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=pattern,
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: False}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False

    @pytest.mark.parametrize("pattern", [".*", "x?", r"\s*", "(?:)", "|", "^"])
    def test_t1_pattern_matching_any_input_is_not_evidence(self, pattern: str) -> None:
        """The T1 constant path consumes matches too and needs the same guard."""
        project = self._create_project({"config.py": "FPS = 60\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST set WARMUP_FRAMES to 10",
            tier=VerificationTier.T1_CONSTANT,
            pattern=pattern,
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: False}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    def test_anchored_empty_pattern_is_not_evidence(self, tier: VerificationTier) -> None:
        """`\\A\\Z` succeeds only on an empty file, so only an empty file exposes it.

        An empty ``__init__.py`` is ordinary in a Python package, and against it the
        old verifier reported `Pattern found in __init__.py` on both tiers. Against a
        non-empty fixture this pattern matches nothing either way, so a test without
        an empty candidate would pass with the guard removed and prove nothing.
        """
        project = self._create_project({"pkg/__init__.py": "", "main.py": "print('hello')\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST define a CameraProvider interface",
            tier=tier,
            pattern=r"\A\Z",
            file_hint="**/*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: False}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False

    def test_empty_matching_pattern_fails_closed_against_an_agent_pass_claim(self) -> None:
        """Refusing the pattern must raise a discrepancy, not silently skip the AC.

        With the agent claiming PASS, a refused pattern has to leave a result behind:
        an empty report list would make ``verified_pass`` fall back to the agent's own
        self-report, turning a refused pattern into an unchecked pass.
        """
        project = self._create_project({"main.py": "print('hello')\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST define a CameraProvider interface",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=".*",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )
        report = summary.reports[0]

        assert report.results, "a refused pattern must still produce a result"
        assert report.verified_pass is False
        assert report.has_discrepancy is True
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False
        assert "Unusable regex pattern" in report.results[0].detail

    def test_empty_matching_pattern_does_not_overturn_an_agent_fail(self) -> None:
        """The spec verifier exists to catch agent lies, not to promote an honest FAIL.

        ``has_discrepancy`` is False here because that flag means "agent claimed PASS
        but verification disagreed", and there is no such claim to contradict — the
        point is that ``verified_pass`` stays False instead of overturning the agent.
        """
        project = self._create_project({"main.py": "print('hello')\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST define a CameraProvider interface",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=".*",
            file_hint="*.py",
        )

        report = (
            SpecVerifier(project_dir=project)
            .verify_all((assertion,), agent_results={0: False})
            .reports[0]
        )

        assert report.verified_pass is False
        assert report.has_discrepancy is False
        assert report.results[0].verified is False

    @pytest.mark.parametrize(
        "pattern",
        [
            r"class\s+CameraProvider",
            r"\bCameraProvider\b",
            "(?=class CameraProvider)",
            r"^(?=[\s\S]*CameraProvider)",
        ],
    )
    def test_discriminating_pattern_still_verifies(self, pattern: str) -> None:
        """The guard must not refuse patterns that really discriminate.

        The lookahead forms matter: they consume nothing, so a guard written in terms
        of match width would refuse them and report a genuine PASS as a discrepancy —
        the same failure this change fixes, running the other way.
        """
        project = self._create_project({"camera.py": "class CameraProvider:\n    pass\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST define a CameraProvider interface",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=pattern,
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 1
        assert summary.reports[0].has_discrepancy is False
        assert summary.override_approval is None

    def test_genuine_constant_match_still_verifies(self) -> None:
        """The same on the T1 path, so the guard is not proven only through T2."""
        project = self._create_project({"config.py": "WARMUP_FRAMES = 10\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Warmup frames should be 10",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"WARMUP_FRAMES\s*=\s*",
            expected_value="10",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all((assertion,))

        assert summary.verified_count == 1


# -- Extractor Tests --


class TestAssertionExtractor:
    """Tests for LLM-based assertion extraction."""

    def _make_extractor(self, response_json: list[dict]) -> AssertionExtractor:
        """Create extractor with mocked LLM that returns given JSON."""
        return self._make_extractor_content(json.dumps(response_json))

    def _make_extractor_content(self, content: str) -> AssertionExtractor:
        """Create extractor with mocked LLM that returns raw content."""
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                CompletionResponse(
                    content=content,
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
    async def test_t1_assertion_requires_expected_value_before_verifier(self) -> None:
        """Empty T1 expected_value is rejected before regex presence can verify it."""
        extractor = self._make_extractor(
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": r"WARMUP_FRAMES\s*=\s*",
                    "expected_value": "",
                    "file_hint": "*.py",
                    "description": "Warmup frames check",
                }
            ]
        )
        result = await extractor.extract("seed_empty_expected", ("WARMUP_FRAMES should be 10",))
        assert result.is_ok
        assert result.value == ()

        project = TestSpecVerifier()._create_project({"config.py": "WARMUP_FRAMES = 999\n"})
        summary = SpecVerifier(project_dir=project).verify_all(
            result.value, agent_results={0: True}
        )
        assert summary.total_assertions == 0
        assert summary.verified_count == 0

    @pytest.mark.asyncio
    async def test_wrapped_invalid_regex_assertion_rejected_before_verifier(self) -> None:
        """Invalid T1/T2 regexes are unusable and must not become assertions."""
        payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": "(",
                    "expected_value": "5",
                    "file_hint": "*.py",
                    "description": "MAX_RETRIES should be five",
                }
            ]
        )
        extractor = self._make_extractor_content(f"Here is the answer:\n```json\n{payload}\n```")

        result = await extractor.extract("seed_invalid_regex", ("MAX_RETRIES should be 5",))

        assert result.is_ok
        assert result.value == ()

    @pytest.mark.asyncio
    async def test_overflowing_regex_assertion_rejected_before_verifier(self) -> None:
        """Regex integer overflow follows the extractor's invalid-pattern fallback."""
        payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": "a{9999999999}",
                    "expected_value": "5",
                    "file_hint": "*.py",
                    "description": "overflowing regex",
                }
            ]
        )
        extractor = self._make_extractor_content(payload)

        result = await extractor.extract("seed_overflow_regex", ("constant is five",))

        assert result.is_ok
        assert result.value == ()

    def test_overflowing_regex_fails_closed_in_verifier(self) -> None:
        """Direct verifier callers cannot crash it with a regex overflow."""
        project = TestSpecVerifier()._create_project({"config.py": "aaaa = 5\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="constant is five",
            tier=VerificationTier.T1_CONSTANT,
            pattern="a{9999999999}",
            expected_value="5",
            file_hint="*.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1

    @pytest.mark.asyncio
    async def test_wrapped_nonmatching_file_hint_fails_verification(self) -> None:
        """A real extracted assertion with no matching files is failed, not promoted."""
        payload = json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": r"MAX_RETRIES\s*=\s*",
                    "expected_value": "5",
                    "file_hint": "*.rs",
                    "description": "MAX_RETRIES should be five",
                }
            ]
        )
        extractor = self._make_extractor_content(f"```json\n{payload}\n```\nDone.")
        result = await extractor.extract("seed_no_files", ("MAX_RETRIES should be 5",))
        assert result.is_ok
        assert len(result.value) == 1

        project = TestSpecVerifier()._create_project({"config.py": "MAX_RETRIES = 5\n"})
        summary = SpecVerifier(project_dir=project).verify_all(
            result.value, agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.failed_count == 1
        assert summary.discrepancy_count == 1
        assert "No files matched hint" in summary.reports[0].results[0].detail

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
    async def test_invalid_tier_is_rejected(self) -> None:
        """Unknown tier string is rejected instead of defaulting to unverifiable."""
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
        assert result.value == ()
