"""SpecVerifier — reads actual source files and checks assertions.

Handles T1 (constant/config) and T2 (structural) verification tiers
by scanning project files with regex patterns. T3/T4 are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
import glob
import logging
import os
import re

from ouroboros.verification.models import (
    ACVerificationReport,
    SpecAssertion,
    SpecVerificationResult,
    SpecVerificationSummary,
    VerificationTier,
)

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 50 * 1024  # 50KB per file
MAX_FILES_PER_HINT = 100
MAX_PATTERN_LENGTH = 200  # Limit LLM-generated regex length to reduce ReDoS risk


@dataclass
class SpecVerifier:
    """Verifies spec assertions against actual project files.

    Reads source files and applies regex patterns to check whether
    the expected values/structures actually exist in the codebase.
    """

    project_dir: str

    def verify_all(
        self,
        assertions: tuple[SpecAssertion, ...],
        agent_results: dict[int, bool] | None = None,
    ) -> SpecVerificationSummary:
        """Verify all assertions against project files.

        Args:
            assertions: Assertions to verify.
            agent_results: Map of ac_index → agent-reported pass/fail.

        Returns:
            SpecVerificationSummary with all results.
        """
        if not assertions:
            return SpecVerificationSummary(project_dir=self.project_dir)

        agent_results = agent_results or {}

        # Group assertions by AC index
        by_ac: dict[int, list[SpecAssertion]] = {}
        for a in assertions:
            by_ac.setdefault(a.ac_index, []).append(a)

        reports: list[ACVerificationReport] = []
        for ac_idx in sorted(by_ac.keys()):
            ac_assertions = by_ac[ac_idx]
            ac_text = ac_assertions[0].ac_text if ac_assertions else ""
            agent_pass = agent_results.get(ac_idx, True)

            results: list[SpecVerificationResult] = []
            for assertion in ac_assertions:
                result = self._verify_one(assertion)
                if result is not None:
                    results.append(result)

            reports.append(
                ACVerificationReport(
                    ac_index=ac_idx,
                    ac_text=ac_text,
                    results=tuple(results),
                    agent_reported_pass=agent_pass,
                )
            )

        return SpecVerificationSummary.from_reports(
            tuple(reports),
            project_dir=self.project_dir,
        )

    def _safe_compile(self, pattern: str, flags: int = 0) -> re.Pattern | None:
        """Compile regex with length guard against ReDoS from LLM-generated patterns."""
        if len(pattern) > MAX_PATTERN_LENGTH:
            logger.warning("Regex pattern too long (%d chars), skipping", len(pattern))
            return None
        try:
            return re.compile(pattern, flags)
        except re.error as e:
            logger.warning("Invalid regex pattern: %s", e)
            return None

    def _verify_one(self, assertion: SpecAssertion) -> SpecVerificationResult | None:
        """Verify a single assertion. Returns None for skipped tiers."""
        if assertion.tier == VerificationTier.T1_CONSTANT:
            return self._verify_constant(assertion)
        elif assertion.tier == VerificationTier.T2_STRUCTURAL:
            return self._verify_structural(assertion)
        else:
            # T3/T4: skip verification
            return None

    def _verify_constant(self, assertion: SpecAssertion) -> SpecVerificationResult:
        """Verify a T1 constant/config assertion by searching source files."""
        if not assertion.pattern:
            return SpecVerificationResult(
                assertion=assertion,
                verified=True,
                detail="No pattern to verify",
            )

        files = self._find_files(assertion.file_hint)
        if not files:
            return SpecVerificationResult(
                assertion=assertion,
                verified=True,  # Can't verify = trust agent
                detail=f"No files matched hint: {assertion.file_hint}",
            )

        pattern = self._safe_compile(assertion.pattern)
        if pattern is None:
            return SpecVerificationResult(
                assertion=assertion,
                verified=True,
                detail="Invalid or too-long regex pattern",
            )

        for file_path in files:
            content = self._read_file(file_path)
            if content is None:
                continue

            match = pattern.search(content)
            if match:
                # Extract the value after the pattern
                actual = self._extract_value_after_match(content, match)
                if assertion.expected_value:
                    verified = assertion.expected_value in actual
                    return SpecVerificationResult(
                        assertion=assertion,
                        verified=verified,
                        actual_value=actual,
                        file_path=file_path,
                        discrepancy=not verified,
                        detail=(
                            f"Expected '{assertion.expected_value}', "
                            f"found '{actual}' in {os.path.basename(file_path)}"
                        ),
                    )
                else:
                    # Pattern found, no expected value to check
                    return SpecVerificationResult(
                        assertion=assertion,
                        verified=True,
                        actual_value=actual,
                        file_path=file_path,
                        detail=f"Pattern found in {os.path.basename(file_path)}",
                    )

        # Pattern not found in any file
        return SpecVerificationResult(
            assertion=assertion,
            verified=False,
            discrepancy=True,
            detail=f"Pattern '{assertion.pattern}' not found in {len(files)} files",
        )

    def _verify_structural(self, assertion: SpecAssertion) -> SpecVerificationResult:
        """Verify a T2 structural assertion (file/class/function exists)."""
        if not assertion.pattern:
            return SpecVerificationResult(
                assertion=assertion,
                verified=True,
                detail="No pattern to verify",
            )

        files = self._find_files(assertion.file_hint)

        # First check: does the pattern match any filename?
        name_pattern = self._safe_compile(assertion.pattern, re.IGNORECASE)

        if name_pattern:
            for file_path in files:
                basename = os.path.basename(file_path)
                if name_pattern.search(basename):
                    return SpecVerificationResult(
                        assertion=assertion,
                        verified=True,
                        file_path=file_path,
                        detail=f"Found file: {basename}",
                    )

        # Second check: search file contents for class/function/interface
        content_pattern = self._safe_compile(assertion.pattern)
        if content_pattern is None:
            return SpecVerificationResult(
                assertion=assertion,
                verified=True,
                detail="Invalid or too-long regex pattern",
            )

        for file_path in files:
            content = self._read_file(file_path)
            if content is None:
                continue
            if content_pattern.search(content):
                return SpecVerificationResult(
                    assertion=assertion,
                    verified=True,
                    file_path=file_path,
                    detail=f"Pattern found in {os.path.basename(file_path)}",
                )

        return SpecVerificationResult(
            assertion=assertion,
            verified=False,
            discrepancy=True,
            detail=f"Structure '{assertion.pattern}' not found in {len(files)} files",
        )

    def _find_files(self, file_hint: str) -> list[str]:
        """Find project files matching a glob hint.

        Validates that all returned paths are within project_dir to prevent
        path traversal via crafted file_hint patterns (e.g., "../../etc/*").
        """
        if not file_hint:
            file_hint = "**/*.py"

        pattern = os.path.join(self.project_dir, file_hint)
        files = glob.glob(pattern, recursive=True)

        # Canonicalize project_dir for path traversal check
        real_project = os.path.realpath(self.project_dir)

        # Filter: must be within project_dir + exclude noise directories
        filtered = [
            f
            for f in files
            if os.path.realpath(f).startswith(real_project + os.sep)
            and not any(
                skip in f for skip in ("__pycache__", ".git", "node_modules", ".venv", ".tox")
            )
        ]

        return filtered[:MAX_FILES_PER_HINT]

    def _read_file(self, file_path: str) -> str | None:
        """Read a file, respecting size limits."""
        try:
            size = os.path.getsize(file_path)
            if size > MAX_FILE_SIZE:
                return None
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, PermissionError):
            return None

    def _extract_value_after_match(self, content: str, match: re.Match) -> str:
        """Extract the value immediately following a regex match.

        Handles common patterns:
        - VAR = 10
        - VAR: 10
        - VAR(10)
        - "value"
        """
        end = match.end()
        rest = content[end : end + 100]

        # Try to extract a value: number, quoted string, or identifier
        value_match = re.match(
            r'\s*[=:]\s*["\']?([^"\'\s,;)\]}{]+)["\']?',
            rest,
        )
        if value_match:
            return value_match.group(1)

        # Try parenthesized value
        paren_match = re.match(r'\s*\(\s*["\']?([^"\'\s,;)]+)["\']?\s*\)', rest)
        if paren_match:
            return paren_match.group(1)

        # Return first 50 chars of what follows
        return rest.strip()[:50]
