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

# The standard library's own regex parser, so that a pattern can be inspected
# without being run. Private, but stable across 3.11–3.14 and vendored by every
# CPython this runs on; the alternative is to hand-write a regex parser, and a
# second parser that disagrees with the real one in a corner is worse than this.
from re import _parser as regex_parser
from typing import NamedTuple

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

# Whether a pattern can match the empty string is decided by *reading* it, never
# by running it. Running it is what a hostile pattern is waiting for: `(?:)
# {100000000}` is fifteen characters, passes the length limit, compiles
# instantly, and then takes longer than any timeout to match a subject with
# nothing in it — a stall reached before the verifier has even looked for a file.
# Capping the pattern's length does not cap a repetition count written inside it,
# because that count is one number rather than one character each.
#
# The parse tree is bounded by the pattern's length, and in it a repetition count
# is a number that is read rather than a number of steps that are taken. So the
# analysis is linear in the pattern no matter what the pattern says.
_MAX_PARSE_DEPTH = 40

# Consumes at least one character, so a sequence containing one cannot be empty.
_CONSUMING = frozenset({"LITERAL", "NOT_LITERAL", "IN", "ANY", "RANGE", "CATEGORY"})
# Anchors consume nothing, but they still either hold or fail on a subject with
# nothing in it, and which one it is has to be read off the individual anchor.
# `^`, `\A`, `$` and `\Z` hold at the sole position of an empty subject; `\b`
# needs a word character on exactly one side and so can never hold there.
# Calling `\b` zero-width would be the safe error on its own — a pattern wrongly
# called nullable is only refused — but `(?!\b)` negates it into the unsafe one,
# so each anchor is classified by what it actually does.
_ANCHORS_ALWAYS_HOLDING_ON_EMPTY = frozenset(
    {
        "AT_BEGINNING",
        "AT_BEGINNING_STRING",
        "AT_BEGINNING_LINE",
        "AT_END",
        "AT_END_STRING",
        "AT_END_LINE",
    }
)
_BOUNDARY = frozenset({"AT_BOUNDARY", "AT_LOC_BOUNDARY", "AT_UNI_BOUNDARY"})
_NON_BOUNDARY = frozenset({"AT_NON_BOUNDARY", "AT_LOC_NON_BOUNDARY", "AT_UNI_NON_BOUNDARY"})

# `\B` is the one anchor whose answer here is not a fact about regular
# expressions but a fact about this interpreter: before 3.14 it required a
# position between two characters and so failed on an empty subject; from 3.14
# the sole position of an empty subject is a non-boundary and it holds. Asking
# the engine once, with a constant pattern of ours against a constant subject,
# is both correct on every version this runs on and correct on versions that do
# not exist yet — which hard-coding a version number is not. Nothing
# model-supplied is run: the pattern here is two characters and this file's own.
_NON_BOUNDARY_HOLDS_ON_EMPTY = re.search(r"\B", "") is not None

_ANCHORS_HOLDING_ON_EMPTY = _ANCHORS_ALWAYS_HOLDING_ON_EMPTY | (
    _NON_BOUNDARY if _NON_BOUNDARY_HOLDS_ON_EMPTY else frozenset()
)
_ANCHORS_FAILING_ON_EMPTY = _BOUNDARY | (
    frozenset() if _NON_BOUNDARY_HOLDS_ON_EMPTY else _NON_BOUNDARY
)
_REPEATS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})


class _Group(NamedTuple):
    """What a capture group did on a match that consumed nothing."""

    empty: bool | None
    """Whether the group's own body can match nothing."""
    took_part: bool | None
    """Whether the group can have participated in such a match."""


def _participation(body_empty: bool | None, optional: bool) -> bool | None:
    """Whether a group reached here can have taken part in an empty match.

    A group whose body certainly consumes certainly did not take part: had it
    run, the match would not have been empty. A group whose body can be empty
    took part only if the path through it is the only path; under a repeat that
    may run zero times, an alternative, or a lookaround, it may equally have
    been skipped, and that is not something this reading can settle.
    """
    if body_empty is False:
        return False
    if body_empty is True and not optional:
        return True
    return None


def _all_empty(answers: list[bool | None]) -> bool | None:
    """Can a run of things, taken together, match nothing?

    Only if each of them can. One that certainly cannot settles it for the whole
    run; otherwise a single unknown leaves the run unknown.
    """
    if any(answer is False for answer in answers):
        return False
    if all(answer is True for answer in answers):
        return True
    return None


def _any_empty(answers: list[bool | None]) -> bool | None:
    """Can one of a set of alternatives match nothing?

    Yes as soon as one certainly can; no only if every one certainly cannot.
    """
    if any(answer is True for answer in answers):
        return True
    if all(answer is False for answer in answers):
        return False
    return None


def _agreed(answers: list[bool | None]) -> bool | None:
    """The one answer they all give, or unknown if they do not all give it.

    For a construct where which part runs is itself undecidable — a conditional
    on whether a group took part — the result is only certain when the parts
    cannot disagree.
    """
    if all(answer is True for answer in answers):
        return True
    if all(answer is False for answer in answers):
        return False
    return None


def _can_match_nothing(
    sequence: object,
    depth: int = 0,
    groups: dict[int, _Group] | None = None,
    optional: bool = False,
) -> bool | None:
    """Whether a parsed regex can match the empty string, read rather than run.

    Three answers, not two: True, False, and None for "this reading cannot tell".
    An unknown construct or a tree deeper than `_MAX_PARSE_DEPTH` is None, and so
    is anything built out of a None.

    The third answer is what keeps the doubt pointed at refusal. Refusal is the
    safe direction — a pattern wrongly called nullable costs an honest criterion
    a formal failure, while one wrongly called discriminating is admitted as
    evidence, which is the defect this file exists to close — but "safe" is a
    direction, and `(?!…)` reverses direction. Answering an unreadable inner
    pattern with a plain True and then negating it produces a confident False:
    the guard would report *certainty that the pattern discriminates* on the
    strength of not having understood it. None negates to None, so the doubt
    survives the negation and `_matches_the_empty_string` still refuses.

    `groups` carries, for each capture group once seen, what it can itself match
    and whether it can have taken part in an empty match — the first so a
    backreference can be judged by what it refers to, the second so a conditional
    can be judged by which arm it would run. `optional` says whether the path
    being walked is one the match could have avoided taking.
    """
    if groups is None:
        groups = {}
    if depth > _MAX_PARSE_DEPTH:
        return None
    answers: list[bool | None] = []
    for opcode, argument in sequence:  # type: ignore[attr-defined]
        name = getattr(opcode, "name", str(opcode))
        if name in _CONSUMING:
            return False
        if name == "AT":
            anchor = getattr(argument, "name", str(argument))
            if anchor in _ANCHORS_HOLDING_ON_EMPTY:
                continue
            answers.append(False if anchor in _ANCHORS_FAILING_ON_EMPTY else None)
        elif name in _REPEATS:
            minimum, _maximum, item = argument
            # Walked whatever the count, so that groups inside a repeat that may
            # run zero times are still recorded for a later backreference.
            item_empty = _can_match_nothing(item, depth + 1, groups, optional or minimum == 0)
            # A repeat that may run zero times is skippable; one that must run
            # is empty only if what it repeats is. The count itself is never
            # counted out.
            answers.append(True if minimum == 0 else item_empty)
        elif name == "SUBPATTERN":
            body_empty = _can_match_nothing(argument[-1], depth + 1, groups, optional)
            number = argument[0]
            if number is not None:
                groups[number] = _Group(body_empty, _participation(body_empty, optional))
            answers.append(body_empty)
        elif name == "GROUPREF":
            # A backreference repeats whatever its group captured, so it is empty
            # only when that group can be. If the group never participated the
            # reference cannot match at all — never empty either. A group not yet
            # seen (a forward reference) is unknown, and stays unknown.
            seen = groups.get(argument)
            answers.append(None if seen is None else seen.empty)
        elif name == "GROUPREF_EXISTS":
            # `(?(1)yes|no)` runs the arm the group's participation selects. On a
            # match that consumed nothing, a group whose body consumes cannot have
            # taken part, so `(a)?(?(1)|b)` certainly runs `b` and certainly is
            # not empty. Only when participation itself is undecidable does the
            # conditional fall back on the arms having to agree.
            reference, yes_arm, no_arm = argument
            took_part = groups[reference].took_part if reference in groups else None
            yes = _can_match_nothing(yes_arm, depth + 1, groups, True)
            no = True if no_arm is None else _can_match_nothing(no_arm, depth + 1, groups, True)
            if took_part is True:
                answers.append(yes)
            elif took_part is False:
                answers.append(no)
            else:
                answers.append(_agreed([yes, no]))
        elif name == "ATOMIC_GROUP":
            answers.append(_can_match_nothing(argument, depth + 1, groups, optional))
        elif name == "BRANCH":
            # Every branch is walked, not just up to the first empty one, so that
            # groups defined in a later branch are recorded too. Only one of them
            # runs, so none of them is a path the match had to take.
            answers.append(
                _any_empty(
                    [_can_match_nothing(branch, depth + 1, groups, True) for branch in argument[1]]
                )
            )
        elif name == "ASSERT":
            # On a subject with nothing in it there is nothing to either side, so
            # a lookaround holds exactly when what it looks for can be empty.
            # `(?=.*foo)` cannot, which is why a lookahead-only pattern stays
            # admissible evidence.
            answers.append(_can_match_nothing(argument[1], depth + 1, groups, True))
        elif name == "ASSERT_NOT":
            inner = _can_match_nothing(argument[1], depth + 1, groups, True)
            answers.append(None if inner is None else not inner)
        else:
            answers.append(None)
    return _all_empty(answers)


def _matches_the_empty_string(pattern: str, flags: int = 0) -> bool:
    """Whether `pattern` can match a subject with nothing in it. Never runs it.

    Unknown counts as yes, which refuses the pattern as evidence.
    """
    try:
        parsed = regex_parser.parse(pattern, flags)
    except Exception:  # pragma: no cover - re.compile has already accepted this
        return True
    return _can_match_nothing(parsed) is not False


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
# The shape that licenses the rescue is narrow on purpose, and it is matched
# against the criterion in full: words that may precede a subject, the file, a
# chain of auxiliaries and adverbs, a copula, the emptiness word, and then
# nothing at all. Everything is decided by what may appear *in* that shape
# rather than by how near some other word happens to fall, because a window has
# a far side and a criterion can always put a negation, a governing verb or a
# second obligation past it.

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
# The only words allowed to stand between the start of the criterion and the
# file. Anything else means the file is not what the criterion is about: a
# preposition makes it the object of something ("the status field in
# marker.txt"), a verb makes it the object of that verb ("do not let marker.txt
# be empty"), and a competing noun makes it a modifier of that noun. This is an
# allow-list for the same reason the forward chain is — no preposed phrasing can
# outrun a rule that admits only what it names.
#
# Each word here belongs to one of four classes, and membership is decided by a
# single test: can putting this word in front of "marker.txt is empty" change
# what is being claimed about marker.txt? For a determiner, a name for the file
# itself, a request to see to it, or a politeness or impersonal frame, it cannot.
# For anything else — every negation, every governing verb, every preposition,
# every competing noun — it can, and so it is left out and refuses the criterion.
_CRITERION_LEAD = frozenset(
    {
        # Determiners and possessives. A closed class in English, listed whole.
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "each",
        "every",
        "all",
        "any",
        "its",
        "their",
        "our",
        "my",
        "your",
        "we",
        # Nouns that name the file rather than something the file contains.
        "file",
        "files",
        "filename",
        "path",
        "artifact",
        "output",
        "document",
        "log",
        "report",
        # Verbs that ask for the clause after them without changing what it
        # claims: "ensure marker.txt is empty" requires exactly what
        # "marker.txt is empty" does. Negating one of these negates the
        # criterion, but the negation is itself a word this set does not admit.
        "ensure",
        "ensures",
        "ensured",
        "verify",
        "verifies",
        "verified",
        "confirm",
        "confirms",
        "confirmed",
        "require",
        "requires",
        "required",
        "expect",
        "expects",
        "expected",
        "check",
        "checks",
        "assert",
        "asserts",
        "validate",
        "validates",
        "guarantee",
        "guarantees",
        "make",
        "sure",
        # Politeness and impersonal frames — "please", "it is necessary that".
        # These carry no claim of their own at all, which is exactly why leaving
        # them out cost an honest criterion a formal failure.
        "please",
        "kindly",
        "it",
        "is",
        "necessary",
        "mandatory",
    }
)

# Stands in for the file's own name, so that the match has one token to anchor
# on and `empty.txt MUST contain data` stops carrying an emptiness word it
# never meant. Underscored to keep it out of reach of any English word.
_FILE_TOKEN = "__the_file__"

# Quotes and brackets wrap a name without changing what is claimed about it, and
# whitespace separates. Everything else in a criterion has to be a word or one of
# these marks, because a character this cannot read may be the whole of the
# meaning: `!=` and `≠` invert the very claim they sit in, and a digit or an
# operator can carry an obligation of its own. So the criterion is consumed
# character by character and an unreadable one refuses the whole reading — a scan
# that silently drops what it does not recognise is matching a *part* again, one
# layer below the tokens.
_READABLE = re.compile(r"[a-z_]+|[.,;:]|['’\"`()\[\]]|\s+")
_MARKUP = frozenset("'’\"`()[]")


def _criterion_tokens(text: str) -> list[str] | None:
    """Every word and mark of `text`, or None if any character is unreadable."""
    tokens: list[str] = []
    consumed = 0
    for match in _READABLE.finditer(text):
        if match.start() != consumed:
            return None
        consumed = match.end()
        token = match.group()
        if not token.isspace() and token not in _MARKUP:
            tokens.append(token)
    return tokens if consumed == len(text) else None


def _mask_file_hint(ac_text: str, file_hint: str) -> str:
    """Replace mentions of the file's own name with `_FILE_TOKEN`.

    Names are matched as whole tokens for the same reason the mention check is —
    `a.py` sits inside `data.py`.

    And case-insensitively for the same reason too. The mention check already
    ignores case, so `Marker.txt must be empty` against a hint of `marker.txt`
    reached this function, went unmasked because the substitution did not, and
    lost the one token the reading anchors on — an ordinary criterion on a file
    that satisfies it, turned into an authoritative failure by a capital letter.
    Both places normalize the same way now.
    """
    if not file_hint:
        return ac_text
    return re.sub(
        rf"(\A|[\s'\"`(\[]){re.escape(file_hint)}(?=\Z|[\s'\"`)\],.;:])",
        rf"\1{_FILE_TOKEN}",
        ac_text,
        flags=re.IGNORECASE,
    )


def _emptiness_the_criterion_requires(ac_text: str, file_hint: str) -> str | None:
    """The emptiness word the criterion predicates of the file, or None.

    Returns `empty` or `blank` because the two do not mean the same thing to a
    file holding one tab, and the caller has to answer the question that was
    actually asked.

    The criterion has to be this shape and nothing besides::

        <lead>* <file> <chain>* <copula> (empty | blank) ["."]

    every character of it consumed. Matching the whole of it is what makes the rescue
    safe to answer, and it is a stronger claim than matching a part: a criterion
    that says more is also *asking* for more — "must be empty and contain a
    header" carries a second obligation, and answering only the emptiness half
    would publish a pass for a requirement nothing checked. Distinguishing a
    second obligation from a harmless aside means reading English, which is the
    guessing this exists to avoid. So anything it cannot consume in full returns
    None and fails closed on the ordinary path, which says plainly that the
    pattern is unusable rather than authoritatively answering the wrong question.

    Consuming the whole criterion also leaves nowhere to put the words that
    broke every narrower version of this: a negation, a governing verb, a
    competing subject and a trailing obligation are all outside the shape, on
    either side of the name, at any distance — and so is a negation written as a
    symbol, because the reading is over characters and `!=` is not among the ones
    it can read.

    Reading words rather than substrings is what keeps `nonempty` from being an
    occurrence of `empty`.
    """
    tokens = _criterion_tokens(_mask_file_hint(ac_text, file_hint).lower())
    if tokens is None:
        return None
    if tokens and tokens[-1] == ".":
        tokens.pop()
    step = 0
    while step < len(tokens) and tokens[step] in _CRITERION_LEAD:
        step += 1
    if step >= len(tokens) or tokens[step] != _FILE_TOKEN:
        return None
    step += 1
    saw_copula = False
    while step < len(tokens) and tokens[step] in _PREDICATE_CHAIN:
        saw_copula = saw_copula or tokens[step] in _COPULAS
        step += 1
    if not saw_copula or step != len(tokens) - 1:
        return None
    return tokens[step] if tokens[step] in _EMPTINESS_WORDS else None


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
        if _matches_the_empty_string(pattern, flags):
            # A pattern that can match a subject with nothing in it proves nothing
            # about a subject that has something in it either — `\A\Z` matches only
            # the empty file, `.*` and `x?` and `\s*` and `(?:)` and `|` and `^`
            # match anywhere in any file, and all of them verified whatever
            # criterion they were handed. What the two kinds share is that the
            # match is not evidence of the criterion. A criterion that is genuinely
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
        if compiled is None or not _matches_the_empty_string(assertion.pattern):
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
                detail="Unusable regex pattern: invalid, too long, or able to match a file with no content",
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
                detail="Unusable regex pattern: invalid, too long, or able to match a file with no content",
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
