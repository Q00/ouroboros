"""Unit tests for spec verification — models, extractor, verifier."""

from __future__ import annotations

import json
import os
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
    def test_empty_file_answer_needs_the_criterion_to_name_the_file(
        self, tier: VerificationTier
    ) -> None:
        """An exact hint is not consent: `ac_text` has to be about the file too.

        `pkg/__init__.py` is empty in most repositories, and both the hint and the
        pattern come out of the same model completion. Keyed on the hint alone, the
        allowance lets `\\A\\Z` pointed at any ordinary package marker "verify" a
        criterion about a camera interface — verbatim the fabrication this change
        exists to close, re-entered through the door opened for the honest case.
        """
        project = self._create_project({"pkg/__init__.py": "", "main.py": "print('hello')\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="MUST define a CameraProvider interface",
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
        assert summary.override_approval is False
        assert "Unusable regex pattern" in summary.reports[0].results[0].detail

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    def test_criterion_must_name_the_hinted_file_as_a_whole_token(
        self, tier: VerificationTier
    ) -> None:
        """`a.py` sits inside `data.py`, and a substring test cannot tell them apart.

        The criterion is about `data.py`; the hint points at a different, empty file
        whose name happens to be a tail of it. Read as a substring, the criterion
        appears to corroborate a hint it never mentioned.
        """
        project = self._create_project({"a.py": "", "data.py": "rows = []\n"})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="data.py MUST remain empty",
            tier=tier,
            pattern=r"\A\Z",
            file_hint="a.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.verified_count == 0
        assert summary.reports[0].verified_pass is False
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize("whitespace", ["\t", "  ", "\n\n"], ids=["tab", "spaces", "newlines"])
    def test_whitespace_is_blank_but_it_is_not_empty(
        self, tier: VerificationTier, whitespace: str
    ) -> None:
        """The two words ask different questions, and `\\A\\Z` draws exactly that line.

        A file of one tab is blank. It is not empty, and the pattern that motivates
        this whole path says so. Answering an `empty` criterion with the looser
        reading would hand a formal PASS to a file the criterion rejects, so the
        word the criterion used has to survive as far as the comparison.
        """
        project = self._create_project({"pkg/__init__.py": whitespace})

        def verify(word: str) -> object:
            assertion = SpecAssertion(
                ac_index=0,
                ac_text=f"pkg/__init__.py MUST remain {word}",
                tier=tier,
                pattern=r"\A\Z",
                file_hint="pkg/__init__.py",
            )
            return SpecVerifier(project_dir=project).verify_all(
                (assertion,), agent_results={0: True}
            )

        strict = verify("empty")
        assert strict.verified_count == 0
        assert strict.reports[0].verified_pass is False
        assert strict.discrepancy_count == 1

        loose = verify("blank")
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

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "ac_text",
        [
            "pkg/config.py MUST NOT be empty",
            "pkg/config.py must never be empty",
            "pkg/config.py cannot be blank",
            "pkg/config.py must not be blank",
            "pkg/config.py must be non-empty",
            "pkg/config.py must contain a nonempty value",
            "pkg/config.py must not under any circumstances be empty.",
            "pkg/config.py should never, under any reading of this spec, be blank",
            "Do not let pkg/config.py be empty",
            "Never allow pkg/config.py to remain empty",
            "Do not permit pkg/config.py to become empty.",
            "Never, under any reading of this spec, allow pkg/config.py to be blank",
            "Do not remove the guard and let pkg/config.py be empty",
            "Please do not let pkg/config.py be empty",
            "Please ensure pkg/config.py is not empty",
            "Please avoid leaving pkg/config.py empty",
            "It is required that pkg/config.py never be empty",
            "Make sure pkg/config.py is not empty",
            "It is forbidden that pkg/config.py be empty",
        ],
        ids=[
            "must-not",
            "never",
            "cannot-blank",
            "not-blank",
            "hyphenated",
            "nonempty-word",
            "distant-negation",
            "very-distant-negation",
            "preposed-negation",
            "preposed-negation-infinitive",
            "preposed-negation-causative",
            "preposed-negation-past-comma",
            "preposed-negation-past-conjunction",
            "politeness-then-negation",
            "politeness-then-negated-copula",
            "politeness-then-avoidance",
            "impersonal-then-negation",
            "periphrasis-then-negation",
            "impersonal-prohibition",
        ],
    )
    def test_a_criterion_forbidding_emptiness_is_not_satisfied_by_an_empty_file(
        self, tier: VerificationTier, ac_text: str
    ) -> None:
        """The violated reading must not pass, and the pattern cannot tell them apart.

        `\\A\\Z` is what a model writes for "must be empty" and for "must not be
        empty" alike, so polarity has to be read from `ac_text`. Deciding it from
        the pattern verified a criterion the file breaks: an empty `config.py`
        against an AC that requires content.

        `nonempty` is here because reading the word as a substring saw `empty`
        inside it and called the criterion an emptiness requirement. The distant
        negations are here because a fixed lookback window has a far side, and a
        criterion can always put its `not` past it.

        The politeness and impersonal forms are the other half of admitting
        `please`, `kindly`, `make sure` and `it is required that` as words that
        change no claim: each of these opens exactly as an admitted criterion
        does and then negates it, so widening the lead must not widen this.
        """
        project = self._create_project({"pkg/config.py": ""})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z",
            file_hint="pkg/config.py",
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False, f"{ac_text!r} must not verify"
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        "ac_text",
        [
            "pkg/marker.txt must be empty and contain a header",
            "pkg/marker.txt must be empty and must be deleted",
            "pkg/marker.txt must be empty, but must also contain generated metadata",
            "pkg/marker.txt MUST be empty and must not be deleted",
            "pkg/marker.txt must be deleted, and pkg/marker.txt must be empty",
            "pkg/marker.txt must be empty then the build proceeds",
            "For the release, pkg/marker.txt must be empty",
            "Run the generator; pkg/marker.txt must be empty",
        ],
        ids=[
            "and-obligation",
            "and-modal-obligation",
            "comma-but-also",
            "and-negated-obligation",
            "obligation-in-preamble",
            "trailing-consequence",
            "preposed-adjunct",
            "preposed-clause",
        ],
    )
    def test_a_criterion_that_asks_for_more_than_emptiness_is_not_answered_here(
        self, tier: VerificationTier, ac_text: str
    ) -> None:
        """Emptiness is only half of a compound criterion, and half is not an answer.

        An earlier revision of this test asserted the opposite for the `and must
        not be deleted` form, on the reasoning that one un-negated occurrence is
        the requirement. That reasoning was wrong in a way the deletion clause
        makes obvious: nothing verifies the second obligation, so reporting a
        pass reports on a criterion that was never checked. The rescue now has
        to consume the criterion in full, and everything here says something it
        cannot consume — including the two preposed forms, which it has no way
        to tell apart from an obligation without reading English.

        Failing closed costs a satisfied emptiness AC its pass, but it costs it
        an honest `Unusable regex pattern` failure rather than an authoritative
        wrong answer, which is the trade this whole guard exists to make.
        """
        project = self._create_project({"pkg/marker.txt": ""})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z",
            file_hint="pkg/marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all((assertion,))

        assert summary.reports[0].verified_pass is False, f"{ac_text!r} must not verify"
        detail = summary.reports[0].results[0].detail
        assert "empty" not in detail, f"{ac_text!r} must not be answered as an emptiness claim"
        assert "Unusable regex pattern" in detail

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
            "pkg/marker.txt MUST be != empty",
            "pkg/marker.txt MUST be ≠ empty",
            "pkg/marker.txt !must be empty",
            "pkg/marker.txt must be empty 100%",
            "pkg/marker.txt must be empty > 0 bytes",
            "pkg/marker.txt must be empty & committed",
        ],
        ids=[
            "ascii-symbolic-negation",
            "unicode-symbolic-negation",
            "symbol-before-modal",
            "numeric-obligation",
            "operator-obligation",
            "symbolic-conjunction",
        ],
    )
    def test_a_character_the_reading_cannot_read_refuses_the_whole_criterion(
        self, tier: VerificationTier, ac_text: str
    ) -> None:
        """Consuming every token is not consuming the criterion, if tokens can be dropped.

        The reading used to gather tokens with `findall`, which silently deletes
        whatever the pattern does not match — so `!=` and `≠` vanished and the
        criteria here read as bare emptiness requirements, publishing a pass for
        the exact claim they negate. That is the same defect as matching a part
        of the criterion, one layer down: a scan over characters that discards
        the ones it does not recognise is a scan over a part of the characters.

        So the criterion is now consumed character by character, and a character
        outside words, `.,;:`, quotes, brackets and whitespace refuses the whole
        reading rather than disappearing from it. An operator or a digit can be
        the entire meaning, and there is no way to tell the harmless ones apart
        without reading English.
        """
        project = self._create_project({"pkg/marker.txt": ""})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z",
            file_hint="pkg/marker.txt",
        )

        summary = SpecVerifier(project_dir=project).verify_all((assertion,))

        assert summary.reports[0].verified_pass is False, f"{ac_text!r} must not verify"
        detail = summary.reports[0].results[0].detail
        assert "empty" not in detail, f"{ac_text!r} must not be answered as an emptiness claim"
        assert "Unusable regex pattern" in detail

    @pytest.mark.parametrize("tier", [VerificationTier.T2_STRUCTURAL, VerificationTier.T1_CONSTANT])
    @pytest.mark.parametrize(
        ("filename", "ac_text"),
        [
            ("empty.txt", "empty.txt MUST contain data"),
            ("blank.json", "blank.json MUST hold at least one record"),
            ("marker.txt", "marker.txt MUST contain an empty JSON string field"),
            ("marker.txt", "marker.txt MUST be an empty JSON object"),
            ("marker.txt", "marker.txt MUST list the empty partitions"),
            ("marker.txt", "The status field in marker.txt must be empty."),
            ("marker.txt", "Every record within marker.txt must be blank"),
            ("marker.txt", "marker.txt entries must be empty"),
            ("marker.txt", "The status field in the generated marker.txt must be empty."),
            ("marker.txt", "The first record of the newly created marker.txt must be blank"),
        ],
        ids=[
            "empty-filename",
            "blank-filename",
            "nested-value",
            "attributive",
            "object-of-verb",
            "prepositional-subject",
            "prepositional-subject-blank",
            "competing-noun-subject",
            "modifier-separated-preposition",
            "two-modifiers-separated-preposition",
        ],
    )
    def test_an_emptiness_word_that_is_not_about_the_file_earns_nothing(
        self, tier: VerificationTier, filename: str, ac_text: str
    ) -> None:
        """The word has to be predicated of the file, not merely present in the AC.

        Each of these mentions emptiness while requiring the file to hold content,
        and each was read as an emptiness requirement: the word sat in the file's own
        name, or described a value nested inside it, or was the adjective on some
        other noun, or the file was named as the object of a preposition — directly
        or with modifiers standing between the two — while some field inside it was
        the actual subject. With `\\A\\Z` and an empty file that produced a formal
        PASS for a criterion the file plainly violates.

        Everything here falls through to the ordinary path, where `\\A\\Z` is refused
        and the criterion fails closed.
        """
        project = self._create_project({filename: ""})
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=r"\A\Z",
            file_hint=filename,
        )

        summary = SpecVerifier(project_dir=project).verify_all(
            (assertion,), agent_results={0: True}
        )

        assert summary.reports[0].verified_pass is False, f"{ac_text!r} must not verify"
        assert summary.discrepancy_count == 1
        assert summary.override_approval is False

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
        ],
    )
    def test_the_subject_check_does_not_reject_a_file_it_only_stands_near(
        self, tier: VerificationTier, ac_text: str
    ) -> None:
        """The shapes the rescue must keep answering, pinned against over-tightening.

        A guard this strict is one edit away from refusing everything, and a
        refusal is a formal failure for an AC the project satisfies — the
        original blocker, relocated. `ensure` and `require` ask for the clause
        after them without changing what it claims, so a criterion that opens
        with one still says exactly what it says, and a determiner or a noun for
        the file itself changes nothing either.

        The politeness and impersonal forms are the shapes that were refused —
        `Please ensure marker.txt is empty` was a formal failure on a project
        that satisfied it, because `please` names no claim and was not listed as
        naming none. Every word admitted here answers one question: can it change
        what is claimed about the file? `please`, `kindly`, `make sure` and `it
        is required that` cannot, so they are read through. `do not`, `never`,
        `avoid` and `prevent` can, so they are not — and the paired rejection
        test pins that.
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
        """A criterion the file can satisfy another way is not this rescue's to answer.

        Answering the emptiness branch alone would throw away the evidence the
        other branch names and publish an authoritative "the file is not empty"
        for a criterion that never required it to be. The refusal has to say what
        it actually is — a pattern that cannot be trusted — so the failure is
        legible instead of a confident wrong reason.
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
