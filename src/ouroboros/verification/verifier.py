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
MAX_SCALAR_LENGTH = 4096

# Inputs a pattern is tried against to decide whether it discriminates at all. A
# pattern that matches *every* one of these matches any file it is pointed at.
#
# Empty is one probe among several, not the test on its own: `\A\Z` matches the
# empty string and nothing else, which is exactly how "this file MUST remain empty"
# is expressed, and rejecting it turns an honest criterion into a formal failure.
# The distinction that matters is not "can match nothing" but "cannot tell two
# files apart". Deliberately short and few — these run on every model-supplied
# pattern, and a long probe is how a screen against ReDoS becomes its own ReDoS.
_INDISCRIMINATE_PROBES = ("", "a", "0\n", " ")

# Inputs carrying no content of their own. A pattern one of these satisfies is
# saying "this file is empty" — a claim about one named file, not about a set.
_CONTENTLESS_PROBES = ("", " ", "\n")


def _satisfied_by_an_empty_file(compiled: re.Pattern) -> bool:
    """True when an empty or blank file alone is enough to make this pattern hit."""
    return any(compiled.search(probe) is not None for probe in _CONTENTLESS_PROBES)


def _unnamed_empty_file(compiled: re.Pattern, files: list[str]) -> bool:
    """True when a hit would only say "one of these candidates happens to be empty".

    `\\A\\Z` is honest evidence for "`pkg/__init__.py` MUST remain empty" — but only
    because that hint names the file the criterion is about. Point the same pattern
    at `**/*.py` and the search stops at whichever candidate is empty first, which
    in a Python project is some package marker that no criterion ever mentioned.
    The pattern discriminates between files; searching a set for *any* hit is what
    throws that away, so this is a property of the pair, not of the regex.
    """
    return len(files) > 1 and _satisfied_by_an_empty_file(compiled)


def _skip_inline_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\f\v":
        index += 1
    return index


def _scan_scalar(text: str, index: int) -> tuple[str, int] | None:
    """Read one bounded scalar without truncating quoted source values."""
    index = _skip_inline_space(text, index)
    if index >= len(text) or text[index] in "\r\n":
        return None

    quote = text[index]
    if quote in {'"', "'"}:
        index += 1
        value: list[str] = []
        consumed = 0
        while index < len(text) and consumed <= MAX_SCALAR_LENGTH:
            char = text[index]
            if char in "\r\n":
                return None
            if char == quote:
                return "".join(value), index + 1
            if char == "\\":
                if index + 1 >= len(text) or text[index + 1] in "\r\n":
                    return None
                escaped = text[index + 1]
                if escaped in {quote, "\\"}:
                    value.append(escaped)
                else:
                    value.extend(("\\", escaped))
                index += 2
                consumed += 2
                continue
            value.append(char)
            index += 1
            consumed += 1
        return None

    start = index
    while (
        index < len(text)
        and index - start <= MAX_SCALAR_LENGTH
        and text[index] not in "\"'\r\n\t ,;)]}{"
    ):
        index += 1
    if index == start or index - start > MAX_SCALAR_LENGTH:
        return None
    return text[start:index], index


def _preceding_assignment_operator(text: str, index: int) -> str | None:
    index -= 1
    while index >= 0 and text[index] in " \t\f\v":
        index -= 1
    return text[index] if index >= 0 and text[index] in "=:" else None


def _has_complete_scalar_terminator(text: str, index: int, operator: str | None) -> bool:
    """Reject a scalar that is only the prefix of an assigned expression."""
    index = _skip_inline_space(text, index)
    if index >= len(text) or text[index] in "\r\n":
        return True
    if text[index] == "#":
        return True
    if operator == "=":
        return text[index] == ";"
    return text[index] in ",;)]}"


def _extract_following_scalar(content: str, index: int) -> str:
    """Extract a direct, assigned, or parenthesized scalar at index."""
    index = _skip_inline_space(content, index)
    if index < len(content) and content[index] in "=:":
        operator = content[index]
        scanned = _scan_scalar(content, index + 1)
        if scanned is None:
            return ""
        value, end = scanned
        return value if _has_complete_scalar_terminator(content, end, operator) else ""
    if index < len(content) and content[index] == "(":
        scanned = _scan_scalar(content, index + 1)
        if scanned is None:
            return ""
        value, end = scanned
        end = _skip_inline_space(content, end)
        if end >= len(content) or content[end] != ")":
            return ""
        return value if _has_complete_scalar_terminator(content, end + 1, None) else ""
    scanned = _scan_scalar(content, index)
    if scanned is None:
        return ""
    value, end = scanned
    operator = _preceding_assignment_operator(content, index)
    return value if _has_complete_scalar_terminator(content, end, operator) else ""


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
        """Compile a model-supplied regex, refusing one that cannot be evidence."""
        if len(pattern) > MAX_PATTERN_LENGTH:
            logger.warning("Regex pattern too long (%d chars), skipping", len(pattern))
            return None
        try:
            compiled = re.compile(pattern, flags)
        except (re.error, OverflowError) as e:
            logger.warning("Invalid regex pattern: %s", e)
            return None
        if all(compiled.search(probe) is not None for probe in _INDISCRIMINATE_PROBES):
            # A pattern that matches every one of these matches whatever file it is
            # pointed at, so a hit is evidence that a file was read, not evidence
            # about the criterion. `.*`, `x?`, `\s*`, `(?:)`, `|` and `^` all compile,
            # and all verified whatever criterion they were given.
            logger.warning(
                "Regex pattern can match without criterion content, skipping: %r", pattern
            )
            return None
        return compiled

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
                verified=False,
                discrepancy=True,
                detail="No pattern to verify",
            )

        files = self._find_files(assertion.file_hint)
        if not files:
            return SpecVerificationResult(
                assertion=assertion,
                verified=False,
                discrepancy=True,
                detail=f"No files matched hint: {assertion.file_hint}",
            )

        pattern = self._safe_compile(assertion.pattern)
        if pattern is None:
            return SpecVerificationResult(
                assertion=assertion,
                verified=False,
                discrepancy=True,
                detail="Unusable regex pattern: invalid, too long, or matches any input",
            )

        if _unnamed_empty_file(pattern, files):
            return SpecVerificationResult(
                assertion=assertion,
                verified=False,
                discrepancy=True,
                detail=(
                    f"Pattern '{assertion.pattern}' is satisfied by an empty file, and hint "
                    f"'{assertion.file_hint}' matched {len(files)} files: a hit would name "
                    f"no particular one. Point the hint at the file the criterion is about."
                ),
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
                    verified = assertion.expected_value.strip() == actual.strip()
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
                verified=False,
                discrepancy=True,
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
                verified=False,
                discrepancy=True,
                detail="Unusable regex pattern: invalid, too long, or matches any input",
            )

        if _unnamed_empty_file(content_pattern, files):
            return SpecVerificationResult(
                assertion=assertion,
                verified=False,
                discrepancy=True,
                detail=(
                    f"Pattern '{assertion.pattern}' is satisfied by an empty file, and hint "
                    f"'{assertion.file_hint}' matched {len(files)} files: a hit would name "
                    f"no particular one. Point the hint at the file the criterion is about."
                ),
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
        return _extract_following_scalar(content, match.end())
