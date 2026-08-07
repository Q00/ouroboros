"""Unit tests for spec verification — models, extractor, verifier."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
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

    # A pattern that matches any input succeeds without any criterion-specific
    # content, so it verifies whatever it is pointed at. All of these compile, which
    # is the only question the gate used to ask.
    #
    # Split by which subject exposes each one: the patterns below also match ordinary
    # non-empty files, while `\A\Z` succeeds only against an empty one — so proving
    # that case needs a project that has such a file. Matching the empty string is
    # not itself the defect: `\A\Z` tells an empty file from every other file, which
    # is exactly what an "MUST remain empty" criterion needs. What fabricates a pass
    # is searching a *set* of candidates for any hit, so that pair is tested apart.
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
    @pytest.mark.parametrize(
        "files",
        [
            {"pkg/__init__.py": "", "main.py": "print('hello')\n"},
            {"pkg/__init__.py": "", ".venv/lib/site.py": "x = 1\n"},
        ],
        ids=["two-candidates", "one-candidate"],
    )
    def test_anchored_empty_pattern_over_a_glob_is_not_evidence(
        self, tier: VerificationTier, files: dict[str, str]
    ) -> None:
        """`\\A\\Z` succeeds only on an empty file, so only an empty file exposes it.

        An empty ``__init__.py`` is ordinary in a Python package, and against it the
        old verifier reported `Pattern found in __init__.py` on both tiers. Against a
        non-empty fixture this pattern matches nothing either way, so a test without
        an empty candidate would pass with the guard removed and prove nothing.

        The criterion here is about a `CameraProvider` and the hint sweeps, so the
        emptiness answer below is not on offer: the search stops at whichever
        candidate is empty and a pass names no particular file. Contrast the exact
        hint and matching criterion in the tests that follow.

        Both fixtures use the same wildcard hint and differ only in how many
        candidates survive ``_find_files``: the second's other `.py` sits under
        `.venv` and is dropped, leaving one. Neither the count nor the hint alone
        may open the allowance, so both must refuse.
        """
        project = self._create_project(files)
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

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    def test_anchored_empty_pattern_verifies_the_file_its_hint_names(
        self, tier: VerificationTier
    ) -> None:
        """An "MUST remain empty" criterion is real, and `\\A\\Z` is its honest regex.

        Refusing `\\A\\Z` outright reports a project that satisfies its AC as a formal
        failure, and the adapter downstream reads that as a discrepancy — the guard
        against fabricated passes manufacturing a fabricated fail instead.

        What is verified here is the file, not the pattern: the criterion asks whether
        `pkg/__init__.py` holds anything, and reading it answers that outright.
        """
        project = self._create_project({"pkg/__init__.py": "", "main.py": "print('hello')\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="pkg/__init__.py MUST remain empty",
            tier=tier,
            pattern=r"\A\Z",
            file_hint="pkg/__init__.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 1
        assert summary.reports[0].verified_pass is True
        assert summary.discrepancy_count == 0

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    def test_anchored_empty_pattern_still_fails_when_the_named_file_has_content(
        self, tier: VerificationTier
    ) -> None:
        """The exact-hint allowance must not become a free pass.

        Same criterion, same pattern, same single-file hint — but the file has content
        now, and `\\A\\Z` has to report that. This is what makes the test above evidence
        of discrimination rather than evidence that the guard stopped looking.
        """
        project = self._create_project({"pkg/__init__.py": "from .camera import x\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="pkg/__init__.py MUST remain empty",
            tier=tier,
            pattern=r"\A\Z",
            file_hint="pkg/__init__.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False
        assert summary.discrepancy_count == 1

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    def test_anchored_empty_refusal_fails_closed_against_an_agent_pass_claim(
        self, tier: VerificationTier
    ) -> None:
        """The refusal has to raise a discrepancy, not merely decline to verify.

        With the agent claiming PASS, a refusal that left ``discrepancy=False`` would
        be indistinguishable from silence: nothing contradicts the self-report and the
        AC is approved anyway. Both tiers must reach the same verdict here — the guard
        living on one exit and not its sibling is what let T2 approve what T1 refused.
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
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False
        assert "Unusable regex pattern" in summary.reports[0].results[0].detail

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize("whitespace", ["\t", "  ", "\n\n"], ids=["tab", "spaces", "newlines"])
    def test_whitespace_is_blank_but_it_is_not_empty(
        self, tier: VerificationTier, whitespace: str
    ) -> None:
        """Empty and blank are different questions, and the pattern asks whichever it asks.

        A file of one tab is blank and is not empty. `\\A\\Z` draws that line and
        `\\A\\s*\\Z` does not, so both are admitted and each answers itself — the
        difference lands in the verdict without anything here reading the
        criterion's English to find it.
        """
        project = self._create_project({"pkg/__init__.py": whitespace})

        def verify(pattern: str) -> object:
            assertion = SpecAssertion(
                ac_index=0,
                ac_text="pkg/__init__.py MUST remain empty",
                tier=tier,
                pattern=pattern,
                file_hint="pkg/__init__.py",
            )
            return SpecVerifier(project_dir=project).verify_all(
                (assertion,), agent_results={0: True}
            )

        strict = verify(r"\A\Z")
        assert strict.verified_count == 0
        assert strict.reports[0].verified_pass is False
        assert strict.discrepancy_count == 1

        loose = verify(r"\A\s*\Z")
        assert loose.verified_count == 1
        assert loose.reports[0].verified_pass is True
        assert loose.discrepancy_count == 0

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"\A\Z", r"\A[0-9\n]*\Z", r"\A(?![a0])", r"\A\Z|[\s\S]{3,}", r"\A[A-Z\n]*\Z"],
        ids=["honest", "digits", "negative-lookahead", "alternation", "upper"],
    )
    def test_a_degenerate_pattern_earns_nothing_from_an_emptiness_criterion(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """Every pattern here matches the file; the file is not empty; all must fail.

        `\\A(?![a0])`, `\\A\\Z|[\\s\\S]{3,}` and `\\A[A-Z\\n]*\\Z` are each built to slip past
        a screen that decides "matches empty and nothing else" by trying a fixed pair
        of sample strings — they dodge the samples and match every real file. None of
        that reaches the verdict, because the verdict is `content.strip()`. A screen
        made of examples only rejects the examples it contains; this one holds
        whatever the pattern turns out to be.
        """
        project = self._create_project({"pkg/__init__.py": "from .camera import x\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="pkg/__init__.py MUST remain empty",
            tier=tier,
            pattern=pattern,
            file_hint="pkg/__init__.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"(?m)^$", r"^$", r"\A.*\Z", r"(?s)\A.*\Z", r"\A\w*\Z", r"\A[^x]*\Z", r"\A\s*", r"\s*\Z"],
        ids=[
            "multiline-blank-line",
            "line-anchors",
            "dot-star",
            "dotall-star",
            "word-star",
            "negated-class",
            "start-only",
            "end-only",
        ],
    )
    def test_a_pattern_a_file_with_content_can_satisfy_is_never_admitted(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """The allowance is for patterns no non-empty file can match — these all can.

        Each of these matches the empty subject, and each also matches this file,
        which has content in it. Admitting any of them would report `PASS` on a
        file the criterion rejects, which is the fabricated pass this whole guard
        exists to stop, arriving through the exit built to rescue honest ACs.

        `(?m)^$` and `^$` are the pair that a naive reading gets wrong: the parser
        emits the same `AT_BEGINNING` for `^` whether or not `re.MULTILINE` is
        set — the compiler makes it a line anchor later, from flags — so counting
        `^` as pinned to the start of the file would admit a pattern that any
        blank line inside a full file satisfies. `\\A\\s*` and `\\s*\\Z` are pinned at
        one end only, and match the run of whitespace at whichever end they name.
        """
        project = self._create_project({"pkg/__init__.py": "\nfrom .camera import x\n\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="pkg/__init__.py MUST remain empty",
            tier=tier,
            pattern=pattern,
            file_hint="pkg/__init__.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False
        assert "Unusable regex pattern" in summary.reports[0].results[0].detail

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    def test_a_must_not_be_empty_criterion_is_left_to_the_ordinary_path(
        self, tier: VerificationTier
    ) -> None:
        """The criterion says "empty", but `\\S` needs content and answers for itself.

        Routing on the word alone would invert this one: the emptiness answer would
        read a file that is correctly non-empty and report a discrepancy against an AC
        the project satisfies.
        """
        project = self._create_project({"pkg/config.py": "FPS = 60\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="pkg/config.py MUST NOT be empty",
            tier=tier,
            pattern=r"\S",
            file_hint="pkg/config.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 1
        assert summary.reports[0].verified_pass is True
        assert summary.discrepancy_count == 0

    def test_a_pattern_is_refused_by_reading_it_and_never_by_running_it(self) -> None:
        """Deciding nullability by execution hands the verifier a denial of service.

        Each pattern here is under twenty characters, so the length cap admits
        it, and each compiles instantly — the cost is all in the matching, which
        used to happen against `""` before the verifier had even looked for a
        file. A repetition count is one number in the pattern and two billion
        steps in the run, so no cap on the pattern's length caps the work. All
        three run for over twelve seconds under execution and are refused in
        under a millisecond by reading the parse tree, which is bounded by the
        pattern's length whatever numbers are written inside it.

        This runs in a child process rather than a thread because a runaway
        `re.search` holds the GIL for its whole duration: no timeout, alarm or
        `join` in this process could interrupt one, and a regression would hang
        interpreter shutdown instead of failing. A child can simply be killed.
        """
        project = self._create_project({"marker.txt": "content"})
        script = textwrap.dedent(
            f"""
            from ouroboros.verification.models import SpecAssertion, VerificationTier
            from ouroboros.verification.verifier import SpecVerifier

            for pattern in [r"(?:){{2000000000}}", r"(?:x?){{2000000000}}", r"(?:\\s*){{2000000000}}"]:
                for tier in (VerificationTier.T1_CONSTANT, VerificationTier.T2_STRUCTURAL):
                    assertion = SpecAssertion(
                        ac_index=0,
                        ac_text="marker.txt MUST contain a header",
                        tier=tier,
                        pattern=pattern,
                        file_hint="marker.txt",
                    )
                    summary = SpecVerifier(project_dir={project!r}).verify_all(
                        (assertion,), agent_results={{0: True}}
                    )
                    assert summary.reports[0].verified_pass is False
                    assert summary.override_approval is False
            """
        )

        try:
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            )
        except subprocess.TimeoutExpired:
            pytest.fail("hostile patterns did not finish — nullability is being decided by running")

        assert completed.returncode == 0, completed.stderr

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "ac_text",
        [
            "marker.txt MUST be empty",
            "marker.txt must be empty.",
            "The file marker.txt must be empty",
            "Ensure marker.txt is empty",
            "We require marker.txt to be empty",
            "marker.txt MUST remain empty",
            "`marker.txt` must be empty",
            '"marker.txt" must be empty',
            "Please ensure marker.txt is empty",
            "Kindly make sure marker.txt is empty",
            "It is required that marker.txt be empty",
            "It is necessary that marker.txt remains empty",
            "Check that marker.txt is empty",
            "Please confirm marker.txt is blank",
            "Verify whether marker.txt is empty",
            "The marker.txt file must remain empty",
            "marker.txt는 비어 있어야 한다",
            "ᛗᚨᚱᚲᛖᚱ",
        ],
        ids=[
            "bare",
            "trailing-period",
            "noun-modifier",
            "transparent-verb",
            "transparent-verb-with-subject",
            "copula-variant",
            "backticked-name",
            "quoted-name",
            "politeness-frame",
            "politeness-and-periphrasis",
            "impersonal-obligation",
            "impersonal-necessity",
            "checking-verb",
            "politeness-and-blank",
            "interrogative",
            "noun-phrase-subject",
            "not-english",
            "names-nothing",
        ],
    )
    def test_the_wording_of_the_criterion_never_decides_the_verdict(
        self, tier: VerificationTier, ac_text: str
    ) -> None:
        """Every wording reaches the same verdict, because none of them is read.

        An earlier revision recognised a closed sentence shape and let the
        allowance turn on it, so ordinary criteria — `Verify whether marker.txt
        is empty`, `The marker.txt file must remain empty` — fell through to the
        blanket refusal and became formal failures for a project that satisfies
        them. A word list can always be one word short, and each missing word is
        another honest AC reported as broken.

        Nothing here parses English any more. The allowance is decided by the
        pattern (`\\A\\Z` can only match a subject with nothing in it) and the hint
        (it names one file, and that file is what gets read), so the criterion's
        wording is free — interrogative, noun-phrase, another language, or text
        that names nothing at all. All of them verify, because all of them are
        answered by reading `marker.txt`.
        """
        project = self._create_project({"marker.txt": ""})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z",
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is True, f"{ac_text!r} must still verify"
        assert summary.discrepancy_count == 0

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "ac_text",
        [
            "marker.txt must be empty or contain # generated",
            "marker.txt must be empty, or contain # generated",
            "marker.txt must be empty unless the build is incremental",
        ],
        ids=["or", "comma-or", "unless"],
    )
    def test_emptiness_offered_as_one_option_is_not_an_emptiness_requirement(
        self, tier: VerificationTier, ac_text: str
    ) -> None:
        """A pattern with a second branch that matches content is not an emptiness test.

        `\\A\\Z|# generated` matches the empty subject, so it is refused unless it
        can only match a subject with nothing in it — and this one plainly can
        match `# generated`. The alternation is what disqualifies it; that the
        criterion also offers a second way to be satisfied is a fact about the
        same pattern, read off the pattern rather than off the prose. The refusal
        has to say what it actually is — a pattern that cannot be trusted — so
        the failure is legible instead of a confident wrong reason.
        """
        project = self._create_project({"marker.txt": "# generated\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z|# generated",
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        detail = summary.reports[0].results[0].detail
        assert "empty" not in detail, f"{ac_text!r} must not be answered as an emptiness claim"
        assert "regex" in detail.lower()
        assert summary.override_approval is False

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

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"(a)?\1", r"(?P<x>a)?(?P=x)", r"(a)\1", r"(a)(b)?\1", r"((a))?\2"],
        ids=[
            "optional-group-numbered-backreference",
            "optional-group-named-backreference",
            "plain-backreference",
            "unparticipating-optional-group",
            "nested-group-backreference",
        ],
    )
    def test_a_backreference_is_read_through_to_the_group_it_names(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """A backreference is empty only when the group it refers to can be.

        The reading called every `GROUPREF` zero-width, which is true of the
        *node* and false of what it matches: `(a)?\\1` matches `aa` and cannot
        match nothing, because a backreference to a group that never
        participated fails outright and one to a group that did repeats what it
        captured. Calling these nullable refused a pattern that genuinely
        discriminates, so a satisfied AC became an authoritative failure — the
        blocker this PR opened with, running the other way.

        Each pattern here is now judged from its group: the four with a
        consuming group are evidence, and `(x?)\\1` in the refusal test below
        still is not.
        """
        project = self._create_project({"marker.txt": "aa\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a doubled letter",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is True, f"{pattern!r} must stay evidence"
        assert summary.discrepancy_count == 0
        assert summary.override_approval is None

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"(x?)\1", r"(a|)\1", r"(a)?\1{0,3}"],
        ids=["nullable-group", "empty-branch-group", "optional-backreference"],
    )
    def test_a_backreference_to_something_that_can_be_empty_is_still_refused(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """Reading the group through must not open the hole it was closing.

        Each of these matches a subject with nothing in it, so it verifies any
        file at all and is not evidence of the criterion — exactly what the
        earlier blanket treatment of `GROUPREF` let through once a nullable
        group stood in front of it.
        """
        project = self._create_project({"marker.txt": "aa\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a doubled letter",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False, f"{pattern!r} must not be evidence"
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"(?!\b)", r"(?!\bx)", r"(?<!\b)"],
        ids=["negated-boundary", "negated-boundary-with-tail", "negated-lookbehind"],
    )
    def test_a_word_boundary_is_not_zero_width_once_something_negates_it(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """`\\b` consumes nothing, which is not the same as holding on nothing.

        Every anchor was treated alike, as a thing that holds wherever it
        appears. That is true of `^`, `\\A`, `$` and `\\Z` on a subject with
        nothing in it, and false of `\\b`, which needs a word character on
        exactly one side and so can never hold there. On its own the error was
        the harmless one — a pattern wrongly called nullable is only refused —
        but `(?!\\b)` reverses it: `\\b` fails on an empty subject, so the
        negation holds, so each of these matches every file including an empty
        one, and each was admitted as evidence and published as a formal PASS.

        Anchors are now classified one at a time by what they actually do, and
        `\\bCameraProvider\\b` stays evidence in the acceptance test above.
        """
        project = self._create_project({"marker.txt": "hello\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a CameraProvider declaration",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False, f"{pattern!r} must not be evidence"
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"\B", r"(?!\B)", r"(?<!\B)", r"(?=\B)"],
        ids=["non-boundary", "negated", "negated-lookbehind", "asserted"],
    )
    def test_a_non_boundary_is_read_from_the_interpreter_that_will_run_it(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """`\\B` on an empty subject is a fact about CPython, not about regexes.

        Before 3.14 it required a position between two characters, of which an
        empty subject has none; from 3.14 the sole position of an empty subject
        is a non-boundary and `\\B` holds there. Written into the table as a
        constant, the entry was right on the interpreter it was written on and
        wrong on 3.12, where `(?!\\B)` matches every file — an unrelated
        `marker.txt` verified as evidence and published as a formal PASS — and
        `(?=\\B)` discriminates but was refused.

        So the assertion here is not which way `\\B` falls, but that the guard
        falls the same way this interpreter does: what matches a subject with
        nothing in it is refused, and what does not stays evidence. The same
        test therefore pins 3.12, 3.13 and 3.14 without naming any of them.
        """
        matches_nothing = re.search(pattern, "") is not None
        project = self._create_project({"marker.txt": "hello\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a CameraProvider declaration",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is not matches_nothing, (
            f"{pattern!r} matches the empty string here: {matches_nothing}"
        )
        assert summary.discrepancy_count == (1 if matches_nothing else 0)

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [r"(?!)|aa", r"(?!(?:))|aa", r"(?!)aa|aa", r"aa(?!)|aa", r"(?!(?!))"],
        ids=["alternative", "empty-body", "before-the-literal", "after-the-literal", "negated"],
    )
    def test_an_assertion_that_can_never_hold_is_read_on_every_supported_parser(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """Which node the parser hands over is a fact about the interpreter.

        `(?!)` is the idiom for a branch that must never be taken. Through 3.12
        it parses as a negative assertion with an empty body, which this reading
        already answers: the body matches nothing, so the negation of it matches
        nothing. From 3.13 the parser folds the whole thing into a single
        `FAILURE` opcode, which fell through to "unknown construct" and refused
        the pattern — so `(?!)|CameraProvider` was evidence on 3.12 and an
        authoritative failure on 3.13 and 3.14, from the same source.

        The assertion is therefore not which node arrives but that the guard
        agrees with the interpreter that will run it, which pins all three
        without naming any of them. It has to hold in both directions, so the
        last case negates the whole thing: `(?!(?!))` matches every file, empty
        ones included, and stays refused on every parser.
        """
        matches_nothing = re.search(pattern, "") is not None
        assert re.search(pattern, "aa\n"), f"{pattern!r} must be satisfied by the fixture"

        project = self._create_project({"marker.txt": "aa\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a doubled letter",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is not matches_nothing, (
            f"{pattern!r} matches the empty string here: {matches_nothing}"
        )
        assert summary.discrepancy_count == (1 if matches_nothing else 0)

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a)?(?(1)|a)",
            r"(a)?(?(1)a|a)",
            r"(a|b)?(?(1)|a)",
            r"(a?)(?(1)a|)",
            r"(?=())(?(1)a|)",
            r"(?<=())(?(1)a|)",
            r"(?=(a?))(?(1)a|)",
            r"((?=()))(?(2)a|)",
            r"(?=(?=()))(?(1)a|)",
            r"(?!()x)(?(1)|a)",
            r"(?!(a)x)(?(1)|a)",
            r"(?!x())(?(1)|a)",
            r"(?!x(a))(?(1)|a)",
            r"(?!x(a?))(?(1)|a)",
            r"(?!()x())(?(2)|a)",
            r"(?!x())\1|aa",
            r"()(?(1)Impossible|)|aa",
            r"aa|()(?(1)Impossible|)",
            r"x|()(?(1)a|)",
            r"(?:(a)|b)(?(1)a|b)",
            r"(?:()|x)(?(1)a|)",
            r"(?=()|x)(?(1)a|)",
            r"()(?(1)()|)(?(2)a|)",
        ],
        ids=[
            "unparticipating-group-runs-the-other-arm",
            "both-arms-consume",
            "branching-group",
            "participating-group-runs-its-own-arm",
            "capture-inside-a-lookahead",
            "capture-inside-a-lookbehind",
            "nullable-capture-inside-a-lookahead",
            "lookahead-inside-a-capture",
            "capture-inside-nested-lookaheads",
            "capture-inside-a-failed-negative-lookahead",
            "consuming-capture-inside-a-negative-lookahead",
            "capture-after-a-consuming-atom",
            "consuming-capture-after-a-consuming-atom",
            "nullable-capture-after-a-consuming-atom",
            "second-capture-after-a-consuming-atom",
            "backreference-to-a-capture-that-did-not-take-part",
            "capture-and-conditional-in-the-first-branch",
            "capture-and-conditional-in-the-second-branch",
            "conditional-in-a-branch-beside-a-consuming-one",
            "conditional-after-a-branch-that-captures-either-way",
            "conditional-after-a-branch-only-one-of-which-can-be-empty",
            "conditional-after-such-a-branch-inside-a-lookahead",
            "capture-in-the-arm-the-conditional-selects",
        ],
    )
    def test_a_conditional_is_read_from_whether_its_group_could_have_taken_part(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """`(?(1)yes|no)` runs one arm, and which one is not a coin toss.

        Requiring both arms to agree refused patterns that plainly discriminate.
        `(a)?(?(1)|a)` cannot match nothing: on a match that consumed nothing the
        optional group cannot have taken part — taking part would have consumed
        the `a` — so the empty arm is exactly the arm that does not run, and the
        `a` is the arm that does. Refusing it turned a satisfied AC into an
        authoritative failure.

        Participation is now carried alongside nullability: a group whose body
        certainly consumes certainly did not take part in an empty match. Where
        participation really is undecidable the arms must still agree, and the
        refusal test below keeps that half honest.

        An assertion is not an undecidable path. A positive one has to hold for
        the match to happen, so a capture inside it took part exactly as much as
        the same capture written outside it; a negative one succeeds only where
        its body fails, and a failed subpattern leaves nothing captured, so a
        capture inside it certainly did not. Reading both as "may have been
        skipped" made every one of these unreadable and refused a criterion the
        source plainly satisfies.

        Knowing that is no use if the walk never reaches the capture. One
        consuming atom settles whether its own sequence can be empty, but the
        sequence inside a negative assertion takes part in the match by failing,
        and a conditional outside still asks what the captures written after that
        atom did. So the walk records them instead of stopping at the first
        thing that consumes.

        Which branch of an alternation runs is undecidable from outside it, but
        not from inside: a capture and a conditional written in the same branch
        stand or fall together. Reading every branch as a path that may have
        been skipped made `()(?(1)Impossible|)` — read correctly on its own —
        unreadable the moment an alternative was written beside it, so the last
        seven are the same conditionals with one added.

        Nor is it always a choice. On a subject with nothing in it nothing can
        consume anything, so a branch that cannot match nothing cannot run at
        all, and when that leaves a single branch standing the alternation
        decides nothing. The same holds one step further in: once participation
        settles which arm of a conditional runs, a capture written in that arm
        is as much on the path as the conditional itself.
        """
        project = self._create_project({"marker.txt": "aa\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a doubled letter",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is True, f"{pattern!r} must stay evidence"
        assert summary.discrepancy_count == 0
        assert summary.override_approval is None

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a)?(?(1)a|)",
            r"(a?)(?(1)|a)",
            r"(a?)?(?(1)a|)",
            r"(?=())(?(1)|a)",
            r"(?<=())(?(1)|a)",
            r"(?=(a?))(?(1)|a)",
            r"(?=())?(?(1)a|)",
            r"(?!()x)(?(1)a|)",
            r"(?!x())(?(1)a|)",
            r"(a)b|(?(1)x|)",
            r"()|(?(1)a|b)",
        ],
        ids=[
            "empty-arm-is-the-one-that-runs",
            "participating-group-empty-arm",
            "either-arm-may-run",
            "lookahead-capture-empty-arm",
            "lookbehind-capture-empty-arm",
            "nullable-lookahead-capture-empty-arm",
            "lookahead-that-may-be-skipped",
            "failed-negative-lookahead-capture-empty-arm",
            "capture-after-a-consuming-atom-empty-arm",
            "conditional-alone-in-its-branch",
            "empty-branch-beside-the-conditional",
        ],
    )
    def test_a_conditional_that_can_run_an_empty_arm_is_still_refused(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """Reading participation through must not open the hole it was closing.

        The first two are certain the other way — the arm that runs is the empty
        one — and the third is the case participation cannot settle, because the
        group's body can itself match nothing, so it may equally have taken part
        or been skipped. Reading assertions accurately has to leave that half
        intact: a capture inside a positive assertion still runs the empty arm
        when the empty arm is the one its participation selects, a capture inside
        a failed negative one still runs the other, and an assertion that is
        itself under a `?` is back to being a path that may have been skipped.
        Reading a branch on its own path must not leak the other way either:
        the last two put the conditional and the capture in *different*
        branches, where the branch that runs is exactly the one that did not
        record the capture. Every pattern here matches a subject with nothing in
        it, so none is evidence of anything.
        """
        project = self._create_project({"marker.txt": "aa\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a doubled letter",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False, f"{pattern!r} must not be evidence"
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(?!(a)?(?(1)|c))",
            r"(?!(a)?(?(1)|x)b)",
            "(?!" + "(" * 45 + "x" + ")" * 45 + ")",
            r"(?!()(?(1)a|))",
            r"(?!(?!(a?)(?(1)|a)))",
        ],
        ids=[
            "negated-conditional",
            "negated-conditional-with-tail",
            "negated-past-depth-limit",
            "negated-conditional-on-a-capture-beside-it",
            "twice-negated-conditional-on-a-nullable-capture",
        ],
    )
    def test_doubt_inside_a_negation_does_not_become_confidence_outside_it(
        self, tier: VerificationTier, pattern: str
    ) -> None:
        """A guess that is safe on its own is unsafe once something negates it.

        The reading answers "can this match nothing?" and sends every doubt to
        the safe side by answering yes, because a pattern wrongly called nullable
        is only refused. `(?!…)` reverses which side is safe. A guessed yes about
        the inside becomes a confident *no* about the outside, and the guard then
        reports that the pattern discriminates on the strength of not having
        understood it — the fabricated pass this PR exists to stop, produced by
        the guard itself.

        Each pattern here matches every file, empty or not, and each was admitted
        as evidence: the first two because the conditional's arms disagree and
        which one runs depends on a capture this does not track, the third
        because nesting past `_MAX_PARSE_DEPTH` guessed yes about the inside.
        The answer is a third value — unknown — that negation leaves unknown.

        The last two are not doubt but the opposite mistake: a *confident* wrong
        answer about the inside. Declaring the body's captures absent before the
        body had been read made `(?!()(?(1)a|))` take its empty arm, so the
        negation of a body that cannot match nothing was read as a pattern that
        discriminates — and it matches every file. What a failed body leaves
        behind is `False` to the outside; while it is being attempted, a
        conditional written beside the capture reads it like any other.
        """
        project = self._create_project({"marker.txt": "hello\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="marker.txt MUST contain a CameraProvider declaration",
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False, f"{pattern!r} must not be evidence"
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        ("filename", "hint", "ac_text"),
        [
            ("marker.txt", "marker.txt", "Marker.txt must be empty"),
            ("marker.txt", "marker.txt", "MARKER.TXT must be empty"),
            ("Marker.txt", "Marker.txt", "marker.txt must be empty"),
            ("marker.txt", "marker.txt", "Please ensure Marker.txt is empty"),
        ],
        ids=["capitalized-mention", "shouted-mention", "capitalized-hint", "capitalized-in-frame"],
    )
    def test_a_capital_letter_in_the_filename_does_not_manufacture_a_failure(
        self, tier: VerificationTier, filename: str, hint: str, ac_text: str
    ) -> None:
        """One normalization, used in both places that read the filename.

        The mention check lowered both sides before looking; the masking that
        follows it did not. So a criterion whose spelling of the name differed
        only in case got past the check, kept its unmasked name, and lost the one
        token the reading anchors on — an ordinary emptiness requirement on a
        file that satisfies it, failed formally by a capital letter.
        """
        project = self._create_project({filename: ""})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z",
            file_hint=hint,
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is True, f"{ac_text!r} must still verify"
        assert summary.discrepancy_count == 0

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

_GOOD_EXTRACTION = json.dumps(
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

    def _make_extractor_sequence(self, *contents: str) -> AssertionExtractor:
        """Create extractor whose mocked LLM answers each call in turn."""
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(
                    CompletionResponse(
                        content=content, model="test", usage={"input": 0, "output": 0}
                    )
                )
                for content in contents
            ]
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
    @pytest.mark.parametrize(
        "unreadable",
        [
            "Sorry, I cannot help with that.",
            "[{",
            '{"assertions": []}',
            "",
        ],
        ids=["prose", "malformed-json", "object-instead-of-array", "nothing-at-all"],
    )
    async def test_an_unreadable_response_is_not_remembered_as_an_extraction(
        self, unreadable: str
    ) -> None:
        """A reply nobody could read must not answer every later generation.

        The cache exists so a seed is extracted once. But the empty tuple a
        failed parse returns is indistinguishable, to the caller, from "nothing
        here needs verifying" — it makes `_verify_spec_compliance` return None
        and the evaluator fall back to the agent's own self-report. Cached, that
        single unreadable reply silently disables spec verification for every
        later generation of the seed, and the log even calls it a cache hit.

        The transport-failure path above is already retried on the next
        generation. Only this path was permanent.
        """
        extractor = self._make_extractor_sequence(unreadable, _GOOD_EXTRACTION, _GOOD_EXTRACTION)

        first = await extractor.extract("seed_unreadable", ("Has class Foo",))
        assert first.is_ok
        assert first.value == ()

        second = await extractor.extract("seed_unreadable", ("Has class Foo",))
        assert second.is_ok
        assert len(second.value) == 1, "the next generation must be allowed to ask again"

        third = await extractor.extract("seed_unreadable", ("Has class Foo",))
        assert third.value is second.value, "the reply that was read is remembered"
        assert extractor.llm_adapter.complete.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "wrong_schema",
        [
            json.dumps([{"ac_index": 0}]),
            json.dumps([{"ac_index": 0, "tier": "t9_imaginary", "pattern": "class Foo"}]),
            json.dumps([{"ac_index": 7, "tier": "t2_structural", "pattern": "class Foo"}]),
            json.dumps(["just a string"]),
            json.dumps([{"ac_index": 0, "tier": "t2_structural", "pattern": ""}]),
        ],
        ids=[
            "no-tier",
            "tier-that-does-not-exist",
            "ac_index-past-the-end",
            "item-that-is-not-an-object",
            "structural-assertion-with-no-pattern",
        ],
    )
    async def test_an_array_whose_every_entry_is_rejected_is_not_remembered(
        self, wrong_schema: str
    ) -> None:
        """An array the model filled with the wrong shape was never read either.

        The JSON parses and the outer array is right, so the old code walked it,
        threw every entry away, and returned the same empty tuple an honest
        "nothing to verify" returns — then cached it. The seed is then answered
        forever from a reply in which nothing arrived in the schema this asks
        for, which is the same permanent silence as unreadable prose.
        """
        extractor = self._make_extractor_sequence(wrong_schema, _GOOD_EXTRACTION, _GOOD_EXTRACTION)

        first = await extractor.extract("seed_wrong_schema", ("Has class Foo",))
        assert first.is_ok
        assert first.value == ()

        second = await extractor.extract("seed_wrong_schema", ("Has class Foo",))
        assert second.is_ok
        assert len(second.value) == 1, "a wrong-schema reply must stay retryable"

        third = await extractor.extract("seed_wrong_schema", ("Has class Foo",))
        assert third.value is second.value
        assert extractor.llm_adapter.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_an_array_only_partly_rejected_is_remembered_as_what_survived(self) -> None:
        """One good entry among bad ones is still an extraction that was read.

        The rule is about a reply in which *nothing* arrived, not about every
        entry being perfect. If one assertion survives, the model answered in
        this schema and the seed should not pay for a second extraction.
        """
        extractor = self._make_extractor_sequence(
            json.dumps(
                [
                    {"ac_index": 0},
                    {
                        "ac_index": 0,
                        "tier": "t2_structural",
                        "pattern": "class Foo",
                        "expected_value": "",
                        "file_hint": "*.py",
                        "description": "",
                    },
                    {"ac_index": 99, "tier": "t2_structural", "pattern": "class Bar"},
                ]
            )
        )

        first = await extractor.extract("seed_partly_rejected", ("Has class Foo",))
        second = await extractor.extract("seed_partly_rejected", ("Has class Foo",))

        assert first.is_ok and len(first.value) == 1
        assert second.value is first.value
        extractor.llm_adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_readable_empty_extraction_is_still_remembered(self) -> None:
        """An empty array is an answer — the model read the ACs and found nothing.

        This is the boundary of the rule above: only a response that could not
        be read is retried. Otherwise a seed whose criteria are genuinely not
        machine-verifiable would pay for an extraction on every generation.
        """
        extractor = self._make_extractor([])

        first = await extractor.extract("seed_nothing_verifiable", ("Looks nice",))
        second = await extractor.extract("seed_nothing_verifiable", ("Looks nice",))

        assert first.is_ok and second.is_ok
        assert first.value == () and second.value == ()
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
