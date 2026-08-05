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

# Words that make an acceptance criterion a claim about a file holding nothing.
# Such a criterion has no content for a regex to find, so every honest way to
# write it — `\A\Z` and its blank-file variants — is refused by the empty-string
# rule in `_safe_compile`, and an honest criterion fails formally.
#
# `_empty_file_criterion_result` answers that one criterion from the file itself.
# Emptiness is a property of the file, and reading it is ground truth; asking a
# regex what it *could* match is inference, and inference run against a fixed
# list of sample strings only ever rejects what the list literally contains.
_EMPTINESS_WORDS = frozenset({"empty", "blank"})

# A criterion that *forbids* emptiness reads almost identically to one that
# requires it, and so does one that asks it of something the file merely
# contains. Both have to be told apart here, from `ac_text`, because the only
# other thing in the assertion that could carry the difference is the extracted
# pattern — and `\A\Z` is what a model writes for every one of these readings.
#
# The shape that licenses the rescue is narrow on purpose: the file, then a
# chain of auxiliaries and adverbs, then a copula, then the emptiness word, then
# the end of the clause. Everything is decided by what may appear *in* that
# chain rather than by how near some other word happens to fall, because a
# window has a far side and a criterion can always put its negation past it.

# A copula is what puts the emptiness on the subject.
_COPULAS = frozenset(
    {
        "be",
        "is",
        "are",
        "was",
        "were",
        "remain",
        "remains",
        "stay",
        "stays",
        "become",
        "becomes",
    }
)
# The only words allowed to stand between the file and the emptiness word.
# Negations are absent from this set rather than enumerated in one of their own,
# so "must not be empty" and "must not under any circumstances be empty" are
# refused by the same rule and no phrasing can outrun it. So is any noun that
# would move the subject elsewhere — "marker.txt entries must be empty" is about
# the entries. An unrecognised word means the sentence is not one this can read,
# which is a reason to fail closed and not a reason to guess.
_PREDICATE_CHAIN = _COPULAS | {
    "must",
    "shall",
    "should",
    "will",
    "would",
    "has",
    "have",
    "had",
    "needs",
    "need",
    "to",
    "left",
    "kept",
    "always",
    "still",
    "already",
    "completely",
    "entirely",
    "totally",
    "fully",
    "strictly",
    "initially",
    "currently",
}
# A file named as the object of a preposition is not the subject of the
# sentence: "the status field in marker.txt must be empty" is a requirement on
# the field, and the file it lives in is anything but empty.
_PREPOSITIONS = frozenset(
    {
        "in",
        "inside",
        "within",
        "of",
        "from",
        "at",
        "for",
        "on",
        "under",
        "into",
        "by",
        "with",
        "across",
        "through",
        "throughout",
    }
)
# Where the predicate is allowed to stop. An emptiness word followed by anything
# else is qualifying that thing rather than the file.
_CLAUSE_ENDS = frozenset({".", ",", ";", ":", "and", "then"})
# Emptiness offered as one option among several is not a requirement to be
# empty, and answering it alone would throw away the evidence the other branch
# names. The ordinary path can weigh a pattern that spells out both.
_ALTERNATIVES = frozenset({"or", "unless", "otherwise", "either", "alternatively", "except"})
_SENTENCE_ENDS = frozenset({".", ";"})


# Stands in for the file's own name, so that the walk has one token to start
# from and `empty.txt MUST contain data` stops carrying an emptiness word it
# never meant. Underscored to keep it out of reach of any English word.
_FILE_TOKEN = "__the_file__"


def _mask_file_hint(ac_text: str, file_hint: str) -> str:
    """Replace mentions of the file's own name with `_FILE_TOKEN`.

    Names are matched as whole tokens for the same reason the mention check is —
    `a.py` sits inside `data.py`.
    """
    if not file_hint:
        return ac_text
    return re.sub(
        rf"(\A|[\s'\"`(\[]){re.escape(file_hint)}(?=\Z|[\s'\"`)\],.;:])",
        rf"\1{_FILE_TOKEN}",
        ac_text,
    )


def _offers_an_alternative_to_emptiness(tokens: list[str], position: int) -> bool:
    """True when the rest of the sentence lets something other than emptiness satisfy it."""
    for token in tokens[position + 1 :]:
        if token in _SENTENCE_ENDS:
            return False
        if token in _ALTERNATIVES:
            return True
    return False


def _names_the_subject(tokens: list[str], position: int) -> bool:
    """True when nothing turns this mention into the object of a preposition.

    Scans the whole phrase back to the nearest clause boundary rather than the
    adjacent token, because a preposition can stand any number of modifiers away
    from the name it governs: in "the status field in the generated marker.txt",
    two words separate `in` from the file it is still the object of. Stopping at
    the boundary is what keeps a preposition belonging to an earlier clause —
    "for the release, marker.txt must be empty" — from disqualifying anything.
    """
    for token in reversed(tokens[:position]):
        if token in _CLAUSE_ENDS:
            return True
        if token in _PREPOSITIONS:
            return False
    return True


def _emptiness_the_criterion_requires(ac_text: str, file_hint: str) -> str | None:
    """The emptiness word the criterion predicates of the file, or None.

    Returns `empty` or `blank` because the two do not mean the same thing to a
    file holding one tab, and the caller has to answer the question that was
    actually asked.

    Walks forward from each mention of the file, and returns a word only when
    every step of the walk holds: no preposition governs the mention anywhere in
    the phrase leading up to it, so the file is the subject rather than something
    the subject lives in; every
    word between it and the emptiness word belongs to `_PREDICATE_CHAIN`, which
    no negation and no competing noun does; one of them is a copula, without
    which the emptiness is being predicated of something else; the emptiness word
    ends its clause, because `empty JSON object` describes contents rather than a
    file; and the sentence offers no alternative to being empty.

    Reading words rather than substrings is what keeps `nonempty` from being an
    occurrence of `empty`. One qualifying mention is enough: "must be empty, and
    must not be deleted" requires emptiness despite carrying a negation in its
    other clause. Anything this cannot place on the file itself returns None,
    and the criterion fails closed on the ordinary path.
    """
    text = _mask_file_hint(ac_text, file_hint).lower().replace("'", "").replace("’", "")
    tokens = re.findall(r"[a-z_]+|[.,;:]", text)
    for position, token in enumerate(tokens):
        if token != _FILE_TOKEN:
            continue
        if not _names_the_subject(tokens, position):
            continue
        saw_copula = False
        step = position + 1
        while step < len(tokens) and tokens[step] in _PREDICATE_CHAIN:
            saw_copula = saw_copula or tokens[step] in _COPULAS
            step += 1
        if not saw_copula or step >= len(tokens) or tokens[step] not in _EMPTINESS_WORDS:
            continue
        following = tokens[step + 1] if step + 1 < len(tokens) else None
        if following is not None and following not in _CLAUSE_ENDS:
            continue
        if _offers_an_alternative_to_emptiness(tokens, step):
            continue
        return tokens[step]
    return None


def _asks_whether_a_named_file_is_empty(assertion: SpecAssertion) -> str | None:
    """The emptiness the criterion asks of the file its hint names, or None.

    All three halves are load-bearing. The hint must name one file, because
    `\\A\\Z` over `**/*.py` stops at whichever candidate is empty first — in a
    Python project some package marker no criterion ever mentioned. The
    criterion must name that same file, because `pkg/__init__.py` is empty in
    most repositories, so an exact hint pointed at it would otherwise "verify" a
    criterion about something else entirely. And the criterion must *require*
    emptiness of that file rather than forbid it, mention it in a filename, or
    ask it of a value nested inside — an empty file satisfies one reading and
    violates the others while the pattern looks the same for all of them.

    The hint comes from the same model completion as the pattern and licenses
    nothing on its own; `ac_text` is the spec's own wording, which the model
    selects by index but does not write. Anything this returns None for falls
    through to the ordinary path, where `_safe_compile` refuses `\\A\\Z` and the
    criterion fails closed.
    """
    hint = assertion.file_hint
    if not hint or any(c in hint for c in "*?["):
        return None
    # As a whole token, not a substring: `a.py` sits inside `data.py`, and a
    # criterion about the latter must not license a hint pointed at the former.
    if not re.search(
        rf"(?:\A|[\s'\"`(\[]){re.escape(hint.lower())}(?=\Z|[\s'\"`)\],.;:])",
        assertion.ac_text.lower(),
    ):
        return None
    return _emptiness_the_criterion_requires(assertion.ac_text, hint)


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

    def _compile_or_none(self, pattern: str, flags: int = 0) -> re.Pattern | None:
        """Compile a model-supplied regex, refusing one that is unusable as a regex."""
        if len(pattern) > MAX_PATTERN_LENGTH:
            logger.warning("Regex pattern too long (%d chars), skipping", len(pattern))
            return None
        try:
            return re.compile(pattern, flags)
        except (re.error, OverflowError) as e:
            logger.warning("Invalid regex pattern: %s", e)
            return None

    def _safe_compile(self, pattern: str, flags: int = 0) -> re.Pattern | None:
        """Compile a model-supplied regex, refusing one that cannot be evidence."""
        compiled = self._compile_or_none(pattern, flags)
        if compiled is None:
            return None
        if compiled.search("") is not None:
            # A pattern that hits a file with no content in it hits every file:
            # `.*`, `x?`, `\s*`, `(?:)`, `|` and `^` all compile, and all verified
            # whatever criterion they were handed. A criterion that is genuinely
            # about a file being empty is answered by `_empty_file_criterion_result`
            # from the file, so nothing honest depends on admitting these here.
            logger.warning(
                "Regex pattern can match without criterion content, skipping: %r", pattern
            )
            return None
        return compiled

    def _empty_file_criterion_result(
        self, assertion: SpecAssertion
    ) -> SpecVerificationResult | None:
        """Answer an "X MUST remain empty" criterion from the file, not from the pattern.

        Returns None whenever this is not that criterion, leaving the assertion to
        the ordinary path. Deliberately one gate ahead of the tier split: a verdict
        that differs between T1 and T2 is a hole, and here that cannot be written.
        """
        if assertion.tier not in (VerificationTier.T1_CONSTANT, VerificationTier.T2_STRUCTURAL):
            return None
        requirement = _asks_whether_a_named_file_is_empty(assertion)
        if requirement is None:
            return None

        # The pattern still decides which way the criterion is being asked. One that
        # needs content — `\S` for "MUST NOT be empty" — is answered by the ordinary
        # path, which can see the content it needs; only one that survives on a file
        # with nothing in it lands here, and that is the pattern this rescue is for.
        compiled = self._compile_or_none(assertion.pattern)
        if compiled is None or compiled.search("") is None:
            return None

        files = self._find_files(assertion.file_hint)
        if len(files) != 1:
            return None
        content = self._read_file(files[0])
        if content is None:
            return None

        # Which word the criterion used decides the test, because they are not the
        # same test. A file of one tab is blank and is not empty, and `\A\Z` — the
        # pattern that motivates this whole path — draws exactly that line.
        # Answering "empty" with the looser reading would formally approve a file
        # the criterion rejects.
        remainder = content if requirement == "empty" else content.strip()
        satisfied = not remainder
        basename = os.path.basename(files[0])
        return SpecVerificationResult(
            assertion=assertion,
            verified=satisfied,
            file_path=files[0],
            discrepancy=not satisfied,
            detail=(
                f"Criterion asks whether {basename} is {requirement}; it is {requirement}"
                if satisfied
                else f"Criterion asks whether {basename} is {requirement}; it holds "
                f"{len(remainder)} characters of content"
            ),
        )

    def _verify_one(self, assertion: SpecAssertion) -> SpecVerificationResult | None:
        """Verify a single assertion. Returns None for skipped tiers."""
        empty_file = self._empty_file_criterion_result(assertion)
        if empty_file is not None:
            return empty_file

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
