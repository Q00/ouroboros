"""SpecVerifier — reads actual source files and checks assertions.

Handles T1 (constant/config) and T2 (structural) verification tiers
by scanning project files with regex patterns. T3/T4 are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    VerificationOutcome,
    VerificationTier,
)

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 50 * 1024  # 50KB per file
MAX_FILES_PER_HINT = 100
MAX_PATTERN_LENGTH = 200  # Limit LLM-generated regex length to reduce ReDoS risk
MAX_SCALAR_LENGTH = 4096
MAX_OCCURRENCES_PER_FILE = 64  # Bound the agreement scan over an untrusted pattern

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

# The two ends of the *subject* a match can be pinned to. Withholding one of
# them asks a different question of the same pattern: not "can this match
# nothing" but "can it match at all without that end of the subject".
#
# Only `\A` and `\Z` are here, and `^` and `$` are not. The parser emits the
# same `AT_BEGINNING` for `^` whether or not `re.MULTILINE` is on — it is the
# compiler, working from flags, that turns it into a line anchor — so a reading
# that counted `^` as pinned to the file would admit `(?m)^$`, which is
# satisfied by a blank line inside a file full of content. `^` is therefore
# read as pinning to nothing at all here, which costs a pattern written that
# way the same formal failure it already gets today.
_START_ANCHORS = frozenset({"AT_BEGINNING_STRING"})
_END_ANCHORS = frozenset({"AT_END_STRING"})


class _Group(NamedTuple):
    """What a capture group did on a match that consumed nothing."""

    empty: bool | None
    """Whether the group's own body can match nothing."""
    took_part: bool | None
    """Whether the group can have participated in such a match."""


def _skippable(on_path: bool | None) -> bool | None:
    """A path the match may have gone around — unless it was never on it at all.

    A repeat that may run zero times, an alternative, and a conditional's arms
    are all paths the match need not have taken. Inside a negative assertion the
    answer is already the stronger `False` and stays there: something the match
    certainly did not walk does not become something it merely might have.
    """
    return False if on_path is False else None


def _participation(body_empty: bool | None, on_path: bool | None) -> bool | None:
    """Whether a group reached here can have taken part in an empty match.

    A group the match certainly never walked certainly did not take part, whether
    or not its body consumes. Otherwise a group whose body certainly consumes
    certainly did not take part either: had it run, the match would not have been
    empty. A group whose body can be empty took part only if the path through it
    is the only path; where the path may have been skipped it may equally have
    taken part or not, and that is not something this reading can settle.
    """
    if on_path is False:
        return False
    if body_empty is False:
        return False
    if body_empty is True and on_path is True:
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
    on_path: bool | None = True,
    withheld: frozenset[str] = frozenset(),
    porous: bool = False,
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
    can be judged by which arm it would run.

    `on_path` says what the match did with the path being walked, and is itself
    three-valued: True for a path the match certainly took, False for one it
    certainly did not — the inside of a negative assertion, which succeeds only
    by failing — and None where it may have gone either way.

    `withheld` names anchors to read as ones that cannot hold, and `porous`
    reads a consuming atom as something a match can cross rather than something
    that ends the question. Together they turn this walk into a second question
    asked of the same tree — "can this match somewhere that is not the start of
    the subject" — whose answer is what tells a pattern pinned to both ends of
    the file apart from one that matches anywhere in it. Defaults leave the
    first question exactly as it was.
    """
    if groups is None:
        groups = {}
    if depth > _MAX_PARSE_DEPTH:
        return None
    answers: list[bool | None] = []
    for opcode, argument in sequence:  # type: ignore[attr-defined]
        name = getattr(opcode, "name", str(opcode))
        if name in _CONSUMING:
            # Recorded rather than returned. One consuming atom already settles
            # this sequence — `_all_empty` answers False as soon as it sees one —
            # but returning here would stop the walk, and captures written after
            # it would never be recorded. A conditional outside can still ask
            # what those captures did, and inside a negative assertion, which is
            # where such a sequence takes part in a match by failing, the answer
            # is that they certainly did not.
            #
            # Unless the walk was asked to be porous, where a consuming atom is
            # something the match crosses rather than something that settles it:
            # the second question is about *where* a match can be, not about how
            # long it is, and a subject free to hold whatever the atom wants is
            # a subject the atom does not rule out.
            answers.append(porous)
        elif name == "FAILURE":
            # An assertion that can never hold. From 3.13 the parser folds `(?!)`
            # and `(?!(?:))` into this single opcode; before that the same source
            # arrives as an `ASSERT_NOT` with an empty body, which this reading
            # already answered. Nothing matches it, a subject with nothing in it
            # included, so the answer is the same False — and reading it as an
            # unknown construct instead refused `(?!)|CameraProvider` on exactly
            # the interpreters that do the folding and nowhere else.
            answers.append(False)
        elif name == "AT":
            anchor = getattr(argument, "name", str(argument))
            # A withheld anchor is read as one that cannot hold. That is not a
            # claim about the anchor — it is how the second and third walks ask
            # their narrower question: with the start of the subject withheld,
            # a `True` answer would mean the pattern can match nothing without
            # ever pinning to the start, and a `False` means every empty match
            # it has needs that end.
            if anchor in withheld:
                answers.append(False)
            elif anchor in _ANCHORS_HOLDING_ON_EMPTY:
                continue
            elif porous:
                # A word boundary says something about the characters on either
                # side, and a subject free to hold whatever it likes is free to
                # hold those. It rules out no position on the porous reading.
                answers.append(True)
            else:
                answers.append(False if anchor in _ANCHORS_FAILING_ON_EMPTY else None)
        elif name in _REPEATS:
            minimum, _maximum, item = argument
            # Walked whatever the count, so that groups inside a repeat that may
            # run zero times are still recorded for a later backreference.
            item_empty = _can_match_nothing(
                item,
                depth + 1,
                groups,
                _skippable(on_path) if minimum == 0 else on_path,
                withheld,
                porous,
            )
            # A repeat that may run zero times is skippable; one that must run
            # is empty only if what it repeats is. The count itself is never
            # counted out.
            answers.append(True if minimum == 0 else item_empty)
        elif name == "SUBPATTERN":
            body_empty = _can_match_nothing(
                argument[-1], depth + 1, groups, on_path, withheld, porous
            )
            number = argument[0]
            if number is not None:
                groups[number] = _Group(body_empty, _participation(body_empty, on_path))
            answers.append(body_empty)
        elif name == "GROUPREF":
            # A backreference repeats whatever its group captured, so it is empty
            # only when that group can be. A reference to a group that certainly
            # did not take part refers to nothing captured, and this interpreter
            # fails such a reference rather than matching nothing with it — so
            # the sequence around it cannot match at all, empty included. A group
            # the walk has not reached is one the match has not reached either,
            # so it has certainly captured nothing here and the reference fails
            # the same way.
            seen = groups.get(argument)
            if seen is None or seen.took_part is False:
                answers.append(False)
            elif porous:
                # A reference that resolves repeats text the subject is free to
                # repeat, so on the porous reading it is crossed like any other
                # consuming atom rather than being empty.
                answers.append(True)
            else:
                answers.append(seen.empty)
        elif name == "GROUPREF_EXISTS":
            # `(?(1)yes|no)` runs the arm the group's participation selects. On a
            # match that consumed nothing, a group whose body consumes cannot have
            # taken part, so `(a)?(?(1)|b)` certainly runs `b` and certainly is
            # not empty. Only when participation itself is undecidable does the
            # conditional fall back on the arms having to agree.
            #
            # Which arm runs is only undecidable while participation is. Once
            # the group settles it, the selected arm is walked on the path the
            # conditional itself is on and the other one certainly does not run,
            # so a capture in the selected arm is as much on the path as the
            # conditional is — reading both arms as skippable left every such
            # capture unknown and refused the conditionals reading them.
            #
            # Nor is a group the walk has not reached an open question. A
            # conditional written before the group it names runs at a point the
            # match has not yet carried the group into, so nothing can have been
            # captured in it and the interpreter takes the `no` arm — every
            # time, on every version. Repetition does not reopen it: a repeat
            # matches nothing only if each of its runs does, and its first run
            # always meets the group unreached. Reading a forward reference as
            # unknown made the arms have to agree, and `(?(1)|a)(a?)` — which
            # plainly discriminates — was refused as evidence.
            reference, yes_arm, no_arm = argument
            seen_by_conditional = groups.get(reference)
            took_part = False if seen_by_conditional is None else seen_by_conditional.took_part
            if took_part is True:
                yes_path, no_path = on_path, False
            elif took_part is False:
                yes_path, no_path = False, on_path
            else:
                yes_path = no_path = _skippable(on_path)
            yes = _can_match_nothing(yes_arm, depth + 1, groups, yes_path, withheld, porous)
            no = (
                True
                if no_arm is None
                else _can_match_nothing(no_arm, depth + 1, groups, no_path, withheld, porous)
            )
            if took_part is True:
                answers.append(yes)
            elif took_part is False:
                answers.append(no)
            else:
                answers.append(_agreed([yes, no]))
        elif name == "ATOMIC_GROUP":
            answers.append(
                _can_match_nothing(argument, depth + 1, groups, on_path, withheld, porous)
            )
        elif name == "BRANCH":
            # Every branch is walked, not just up to the first empty one, so that
            # groups defined in a later branch are recorded too.
            #
            # Which branch runs is undecidable from outside, but not from inside:
            # a capture and a conditional reading it that sit in the same branch
            # stand or fall together, so within a branch the path is the one the
            # branch inherits. Handing every branch a skippable path instead made
            # participation unknown for its own captures, and `()(?(1)x|)` — read
            # correctly on its own — became unreadable the moment an alternative
            # was written beside it.
            #
            # Each branch therefore reads and records over its own copy of the
            # table, so one branch cannot answer a question about a capture that
            # only exists in a sibling it does not run with. What a branch
            # recorded merges back with participation weakened to "may have gone
            # either way", which is what the alternation makes it for anything
            # reading that capture from outside.
            #
            # Except when it is not a choice. On a subject with nothing in it
            # nothing can consume anything, so a branch that cannot match
            # nothing cannot run at all, and when that leaves a single branch
            # standing the alternation decides nothing: its captures took part
            # exactly as much as the alternation itself did. `(?:()|x)(?(1)a|)`
            # is that shape, and weakening it refused a pattern that plainly
            # discriminates.
            walked: list[tuple[bool | None, dict[int, _Group]]] = []
            for branch in argument[1]:
                local = dict(groups)
                walked.append(
                    (_can_match_nothing(branch, depth + 1, local, on_path, withheld, porous), local)
                )
            branch_answers = [answer for answer, _ in walked]
            settled = branch_answers.count(True) == 1 and all(
                answer is not None for answer in branch_answers
            )
            for answer, local in walked:
                for number, group in local.items():
                    if number in groups:
                        continue
                    if answer is False:
                        took = False
                    elif settled:
                        took = group.took_part
                    else:
                        took = _skippable(group.took_part)
                    groups[number] = _Group(group.empty, took)
            answers.append(_any_empty(branch_answers))
        elif name == "ASSERT":
            # On a subject with nothing in it there is nothing to either side, so
            # a lookaround holds exactly when what it looks for can be empty.
            # `(?=.*foo)` cannot, which is why a lookahead-only pattern stays
            # admissible evidence.
            #
            # A positive assertion has to hold for the match to happen, so the
            # path through it is not one the match could have gone around — a
            # capture inside it took part exactly as much as the same capture
            # written outside it would have. Calling it optional here is what
            # made `(?=())(?(1)x|)` unreadable and refused it as evidence.
            answers.append(
                _can_match_nothing(argument[1], depth + 1, groups, on_path, withheld, porous)
            )
        elif name == "ASSERT_NOT":
            # A negative assertion succeeds only where its body fails, and a
            # subpattern that fails leaves nothing captured behind it. So this is
            # the one path the match certainly did not walk, and every capture
            # inside it certainly did not take part.
            #
            # From outside. The body is still attempted, and while it is being
            # attempted its captures are as real as any other — a conditional
            # written inside the body reads the group beside it exactly as it
            # would anywhere else. Declaring them absent before the body had
            # been read made `(?!()(?(1)a|))` take its empty arm, so a pattern
            # that matches every file was admitted as evidence and published as
            # a formal PASS, which is the defect this file exists to close. So
            # the body is walked on the path it inherits, over its own copy of
            # the table, and only what escapes to the outside is `False`.
            local_to_body = dict(groups)
            inner = _can_match_nothing(
                argument[1], depth + 1, local_to_body, on_path, withheld, porous
            )
            for number, group in local_to_body.items():
                if number not in groups:
                    groups[number] = _Group(group.empty, False)
            if porous:
                # The porous reading asks whether the body can match *somewhere*,
                # and the negation of "somewhere" is not "nowhere" — `(?!x)` holds
                # at every position that is not followed by an `x`, however freely
                # an `x` can appear elsewhere. So only a body that can match
                # nowhere at all lets this assertion be read with certainty.
                answers.append(True if inner is False else None)
            else:
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


# Whitespace is the one thing a file can hold that no acceptance criterion is
# about, so it is the one thing a pattern may consume and still be read as a
# claim that the file holds nothing. `\s` is exactly this class; a literal space,
# tab or newline is a member of it.
_BLANK_CATEGORIES = frozenset({"CATEGORY_SPACE"})

# Character ranges are read, not enumerated, past this width. Every run of
# whitespace codepoints is a handful long — `\t` through `\r` is five — so a
# wide range is not a blank class that this failed to recognise, it is a class
# with content in it.
_MAX_BLANK_RANGE = 64


def _is_blank_codepoint(value: object) -> bool:
    return isinstance(value, int) and 0 <= value <= 0x10FFFF and chr(value).isspace()


def _is_blank_range(bounds: object) -> bool:
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        return False
    low, high = bounds
    if not isinstance(low, int) or not isinstance(high, int):
        return False
    if high - low > _MAX_BLANK_RANGE:
        return False
    return all(_is_blank_codepoint(code) for code in range(low, high + 1))


def _is_blank_class(items: object) -> bool:
    """Whether a `[...]` class can only ever match whitespace.

    A negated class is not one of them whatever it lists: `[^ ]` is every
    character that is not a space, which is what content is made of.
    """
    try:
        members = list(items)  # type: ignore[call-overload]
    except TypeError:  # pragma: no cover - the parser always hands back a list
        return False
    for opcode, argument in members:
        name = getattr(opcode, "name", str(opcode))
        if name == "LITERAL":
            if not _is_blank_codepoint(argument):
                return False
        elif name == "RANGE":
            if not _is_blank_range(argument):
                return False
        elif name == "CATEGORY":
            if getattr(argument, "name", str(argument)) not in _BLANK_CATEGORIES:
                return False
        else:
            return False
    return True


def _consumes_only_blank(sequence: object, depth: int = 0) -> bool:
    """Whether nothing this can consume is content.

    What a lookaround looks at is not consumed and so is never part of what
    matched; it is skipped here for that reason. A backreference is not, because
    it repeats whatever its group captured, and a group inside a lookaround can
    have captured anything at all.

    False for anything this cannot read, so an unrecognised construct narrows
    nothing.
    """
    if depth > _MAX_PARSE_DEPTH:
        return False
    try:
        items = list(sequence)  # type: ignore[call-overload]
    except TypeError:  # pragma: no cover - the parser always hands back a sequence
        return False
    for opcode, argument in items:
        name = getattr(opcode, "name", str(opcode))
        if name in ("AT", "ASSERT", "ASSERT_NOT", "FAILURE"):
            continue
        if name == "LITERAL":
            if not _is_blank_codepoint(argument):
                return False
        elif name == "IN":
            if not _is_blank_class(argument):
                return False
        elif name == "RANGE":
            if not _is_blank_range(argument):
                return False
        elif name == "CATEGORY":
            if getattr(argument, "name", str(argument)) not in _BLANK_CATEGORIES:
                return False
        elif name in _REPEATS or name == "SUBPATTERN":
            # Both carry the thing they wrap last: a repeat cannot consume what
            # its body does not, and a group consumes exactly its body.
            if not _consumes_only_blank(argument[-1], depth + 1):
                return False
        elif name == "ATOMIC_GROUP":
            if not _consumes_only_blank(argument, depth + 1):
                return False
        elif name == "BRANCH":
            if not all(_consumes_only_blank(branch, depth + 1) for branch in argument[1]):
                return False
        elif name == "GROUPREF_EXISTS":
            _reference, yes_arm, no_arm = argument
            if not _consumes_only_blank(yes_arm, depth + 1):
                return False
            if no_arm is not None and not _consumes_only_blank(no_arm, depth + 1):
                return False
        else:
            return False
    return True


def _matches_only_a_blank_subject(pattern: str, flags: int = 0) -> bool:
    r"""Whether the only file this can match is one with nothing in it.

    `\A\Z` and `\A\s*\Z` match a subject with nothing in it, and the empty-string
    rule refuses them for it. But the reason that rule exists does not reach
    them. A pattern that can match nothing *somewhere in the middle* matches
    every file there is, so its match is evidence of nothing; these match one
    file and no other, which makes them not the weakest evidence available but
    the sharpest. Refusing them failed the one criterion they are the right
    answer to — that a file be left empty — and left it needing a rescue that
    read the criterion's English instead, which is guesswork this file should
    not be doing.

    Three readings, none of which runs the pattern. Nothing it consumes may be
    content, or the file it matched had something in it. No match may begin
    anywhere but the start of the subject, and none may end anywhere but the
    end, or what matched was a blank stretch inside a file rather than the whole
    of one — asked by walking the same tree twice more with one end of the
    subject withheld, which forces every match to justify itself without that
    end. Both walks answering "cannot" is what says both ends were required.

    Only `\A` and `\Z` count as the ends of the subject, so `\A$` and `(?m)^$`
    are refused rather than admitted — see `_START_ANCHORS`. That costs a
    criterion written with `$` the failure it already gets today, and it is the
    only reading that stays sound without inspecting flags the parser has not
    applied.
    """
    try:
        parsed = regex_parser.parse(pattern, flags)
    except Exception:  # pragma: no cover - re.compile has already accepted this
        return False
    if not _consumes_only_blank(parsed):
        return False
    unpinned_at_start = _can_match_nothing(parsed, withheld=_START_ANCHORS, porous=True)
    unpinned_at_end = _can_match_nothing(parsed, withheld=_END_ANCHORS, porous=True)
    return unpinned_at_start is False and unpinned_at_end is False


def _skip_inline_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\f\v":
        index += 1
    return index


def _scan_scalar(text: str, index: int) -> tuple[str, int] | None:
    """Read one bounded scalar without truncating quoted source values.

    An unquoted scan stops at a bracket it did not open, and now also at one it
    would have to: a scalar is a value written whole, and `(` or `[` is the
    start of something whose end is somewhere this does not look. Without that,
    a value wrapped across lines the way a formatter wraps one --

        WARMUP_FRAMES = (
            10
        )

    -- scanned to the line end and returned `(`, which then reached the
    comparison as what the file says the constant is.
    """
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
        and text[index] not in "\"'\r\n\t ,;([)]}{\\="
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


class _UnreadBinding:
    """An occurrence that binds a value the scanner could not read.

    Distinct from ``None``, which is an occurrence that binds nothing at all.
    Both are failures to read, but only one of them is a place where the file
    says what the constant is: `X: int = 3`, `X = int("3")`, `X = 3 if c else 4`
    and `X = ""` are declarations, and the scanner reading none of them does not
    make them prose. Spelling them the same way is what let a declaration go
    missing from the comparison rather than unsettle it.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<unread binding>"


UNREAD_BINDING = _UnreadBinding()

Reading = str | _UnreadBinding | None


def _binds_at(content: str, index: int) -> bool:
    """Whether a value is written at index, readable or not.

    The three shapes are the ones the scanner itself looks for: an assignment or
    mapping operator after the match, a parenthesized argument after it, and an
    operator the pattern consumed and left behind it — `NAME\\s*=\\s*` is one of
    the shapes the extractor's own prompt asks the model for.

    One closing quote may stand between the match and the operator, because a
    quoted key is a binding written the way JSON, YAML and every settings dict
    write one. A quote with no operator after it is still nothing bound: prose
    that names the constant in quotes reaches the same place and stops here.

    Failing all three, an operator anywhere later on the same physical line is
    taken as one too, because a declaration is free to put a type, a modifier or
    a default between the name and the operator — `NAME int = 10`, `NAME ?= 10`,
    `NAME: int = 10` — and requiring adjacency read every one of them as prose.
    Prose that happens to carry an operator is then called a binding it cannot
    read, which costs the criterion its verdict and never its soundness; a
    declaration read as prose costs the opposite, and vanishes from the very
    agreement this scanner exists to compute.
    """
    at = _skip_inline_space(content, index)
    if at < len(content) and content[at] in "\"'":
        at = _skip_inline_space(content, at + 1)
    if at < len(content) and content[at] in "=:(":
        return True
    if _preceding_assignment_operator(content, index) is not None:
        return True
    line_end = content.find("\n", index)
    rest = content[index:] if line_end == -1 else content[index:line_end]
    return "=" in rest or ":" in rest


def _extract_following_scalar(content: str, index: int) -> Reading:
    """Extract a direct, assigned, or parenthesized scalar at index.

    Three outcomes, because there are three things that can be at an occurrence
    and only one of them is a value. A string is what was read. ``None`` is
    prose — a mention with nothing bound to it, which settles nothing and has to
    settle nothing. ``UNREAD_BINDING`` is a declaration this scanner cannot
    parse, which settles nothing either but is not free of consequence: the file
    says what the constant is there, so no reading taken elsewhere can be called
    what the candidates agree on.

    A scalar scanned as blank is one of those declarations. `X = ""` and
    `X = " "` reach the scanner and the strings they yield are the one thing
    every file holds anyway, so reading one settles no criterion — but the
    binding is still written, so it counts as one that went unread rather than
    as prose.
    """
    scanned = _scan_following_scalar(content, index)
    if scanned is not None and scanned.strip():
        return scanned
    return UNREAD_BINDING if _binds_at(content, index) else None


def _scan_following_scalar(content: str, index: int) -> str | None:
    """Scan a direct, assigned, or parenthesized scalar at index, blanks and all."""
    index = _skip_inline_space(content, index)
    if index < len(content) and content[index] in "=:":
        operator = content[index]
        scanned = _scan_scalar(content, index + 1)
        if scanned is None:
            return None
        value, end = scanned
        return value if _has_complete_scalar_terminator(content, end, operator) else None
    if index < len(content) and content[index] == "(":
        scanned = _scan_scalar(content, index + 1)
        if scanned is None:
            return None
        value, end = scanned
        end = _skip_inline_space(content, end)
        if end >= len(content) or content[end] != ")":
            return None
        return value if _has_complete_scalar_terminator(content, end + 1, None) else None
    scanned = _scan_scalar(content, index)
    if scanned is None:
        return None
    value, end = scanned
    operator = _preceding_assignment_operator(content, index)
    return value if _has_complete_scalar_terminator(content, end, operator) else None


@dataclass
class _Readings:
    """What a pattern found at its occurrences, and the file each was found in.

    Distinctness is judged on the stripped value, because that is what the
    comparison judges equality on. Occurrences that read the same value are one
    reading however many there are; two that read different values are a
    disagreement however far apart they sit.

    An occurrence that binds a value the scanner could not read is kept too, as
    the file it was written in. It contributes no value to compare and it cannot
    be made to agree with one, so a single one of them decides the outcome on
    its own — which is why it is recorded rather than dropped.

    Both of those settle the outcome, and nothing read afterwards can unsettle
    it, so collection stops there.
    """

    values: list[tuple[str, str]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    unread_in: str | None = None

    @property
    def settled(self) -> bool:
        """Whether no further occurrence could change the outcome."""
        return self.unread_in is not None or len(self.values) > 1

    def add(self, reading: Reading, file_path: str) -> None:
        if reading is None or self.settled:
            return
        if isinstance(reading, _UnreadBinding):
            self.unread_in = file_path
            return
        if reading.strip() in self.seen:
            return
        self.seen.add(reading.strip())
        self.values.append((reading, file_path))


@dataclass
class SpecVerifier:
    """Verifies spec assertions against actual project files.

    Reads source files and applies regex patterns to check whether
    the expected values/structures actually exist in the codebase. Strict mode
    is the safe default: unavailable and skipped evidence block an approval
    override. Exploratory callers may set ``strict=False`` to observe those
    outcomes without requesting an override; they still never become VERIFIED.
    """

    project_dir: str
    strict: bool = True

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
            return SpecVerificationSummary(project_dir=self.project_dir, strict=self.strict)

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
                if agent_pass is False:
                    result = self._demoted_from_overturning(result)
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
            strict=self.strict,
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

    def _searches_one_named_file(self, file_hint: str | None, candidates: list[str] | None) -> bool:
        """Whether this search is about one file the hint named outright.

        A glob is not that, however few files it happens to match today. `\\A\\Z`
        over `**/*.py` asks whether the project contains *any* empty file, and in
        a Python project it does — some package marker no criterion mentioned. A
        hint that names one path asks about that path.
        """
        if not file_hint or any(char in file_hint for char in "*?["):
            return False
        return candidates is not None and len(candidates) == 1

    def _safe_compile(
        self,
        pattern: str,
        flags: int = 0,
        file_hint: str | None = None,
        candidates: list[str] | None = None,
    ) -> re.Pattern | None:
        """Compile a model-supplied regex, refusing one that cannot be evidence."""
        compiled = self._compile_or_none(pattern, flags)
        if compiled is None:
            return None
        if _matches_the_empty_string(pattern, flags):
            # `.*` and `x?` and `(?:)` and `|` and `^` match anywhere in any file,
            # so they verified whatever criterion they were handed — the match is
            # not evidence of the criterion. `\A\Z` shares the empty match and
            # nothing else: pinned to both ends of the subject and consuming no
            # content, it holds for a file with nothing in it and fails for every
            # other, which is discrimination rather than the absence of it. So it
            # is admitted where it is being asked about one named file, and the
            # search below answers the criterion from that file directly.
            if self._searches_one_named_file(file_hint, candidates) and (
                _matches_only_a_blank_subject(pattern, flags)
            ):
                return compiled
            logger.warning(
                "Regex pattern can match without criterion content, skipping: %r", pattern
            )
            return None
        return compiled

    def _demoted_from_overturning(self, result: SpecVerificationResult) -> SpecVerificationResult:
        """Refuse a VERIFIED as grounds to overturn an agent-reported FAIL.

        This scanner exists to check whether an agent's *claimed PASS* survives
        contact with the source. Overturning a FAIL is the opposite direction,
        and it is the direction with no safe stopping point: what a regex match
        proves about a criterion depends on whether the pattern is really about
        that criterion, and nothing available here can settle that. A rule that
        reads the criterion's wording admits `class\\s+\\w+` for "a
        CameraProvider class" through the shared word `class`; tightening it to
        read named targets from their spelling admits `MUST` through ordinary
        requirement prose. Each repair moves the hole rather than closing it,
        because the question — does this text name this criterion's subject —
        is not one a finite reading of prose answers.

        So the authority is withdrawn rather than qualified. An agent that
        reported FAIL keeps its FAIL, whatever the pattern matched. No property
        of the pattern can restore the override, which is what makes this
        closed rather than merely narrower: there is nothing left to bypass.

        Only this direction. A VERIFIED that agrees with the agent's own PASS
        claims no authority the agent had not already claimed, and is passed
        through untouched — the false-PASS check that is this scanner's actual
        job.

        The demotion is to UNVERIFIABLE, not DISCREPANCY: the evidence is not
        usable *here*, which is different from evidence that the criterion is
        unmet.
        """
        if result.outcome is not VerificationOutcome.VERIFIED:
            return result
        logger.warning(
            "Refusing regex evidence as grounds to overturn an agent FAIL: %r",
            result.assertion.pattern,
        )
        return SpecVerificationResult(
            assertion=result.assertion,
            outcome=VerificationOutcome.UNVERIFIABLE,
            file_path=result.file_path,
            detail=(
                "Pattern matched, but source-scan evidence cannot overturn an agent-reported FAIL"
            ),
        )

    def _verify_one(self, assertion: SpecAssertion) -> SpecVerificationResult:
        """Verify one assertion, including tiers this scanner deliberately skips."""
        if assertion.tier == VerificationTier.T1_CONSTANT:
            return self._verify_constant(assertion)
        if assertion.tier == VerificationTier.T2_STRUCTURAL:
            return self._verify_structural(assertion)
        if assertion.tier == VerificationTier.T3_BEHAVIORAL:
            detail = "Behavioral assertion requires test execution or semantic analysis"
        else:
            detail = "Subjective assertion is not independently verifiable by source scanning"
        return SpecVerificationResult(
            assertion=assertion,
            outcome=VerificationOutcome.SKIPPED,
            detail=detail,
        )

    def _verdict_from_readings(
        self,
        assertion: SpecAssertion,
        readings: _Readings,
        first_match_in: str,
    ) -> SpecVerificationResult:
        """Compare the expected value against what the candidates read as.

        Every occurrence of the pattern is read, in every candidate file, rather
        than the first match in the first file that had one. Taking the first
        was a choice among readings that nothing here gives grounds for: a
        constant is routinely written twice, once in a comment above the
        declaration and once in the declaration itself, and which of the two
        `search` returns is decided by line order. Whichever way that fell, the
        verdict was reported as what the source says — a decoy comment above a
        contradicting declaration minted a PASS, and one above an agreeing
        declaration minted a DISCREPANCY. The same held between files: a
        mention in the first file the glob happened to yield settled a criterion
        about a constant bound differently in the file that declares it.

        So the readings have to agree. Where they do, the candidates say one
        thing and the comparison is about that thing. Where they disagree there
        is no single value they can be said to hold, and that is missing
        evidence rather than counter-evidence: UNVERIFIABLE, not DISCREPANCY.

        An occurrence that binds a value this scanner cannot read has the same
        effect for the same reason. It is a place the file states the constant,
        so a reading taken anywhere else is not what the candidates agree on —
        it is only what was legible. Dropping such an occurrence would put the
        conflation this method exists to remove back one level up, with an
        unread declaration and a bare mention spelled the same way.

        This does not make a comment unreadable — nothing here can tell source
        from prose in a file whose language it does not know. It removes the
        silent preference between them, so a disagreeing comment costs the
        verdict its authority instead of deciding it.
        """
        if readings.unread_in is not None:
            return SpecVerificationResult(
                assertion=assertion,
                file_path=readings.unread_in,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=(
                    f"Pattern matched a binding in {os.path.basename(readings.unread_in)} "
                    "whose value could not be read; nothing read elsewhere can stand for it"
                ),
            )
        if not readings.values:
            # Every occurrence landed on prose, a comment, or a bare mention —
            # somewhere no value follows it. Nothing was read, so there is
            # nothing here that agrees with the expected value or contradicts it.
            return SpecVerificationResult(
                assertion=assertion,
                file_path=first_match_in,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=(
                    f"Pattern matched in {os.path.basename(first_match_in)} "
                    "but no value follows the match to compare"
                ),
            )
        if len(readings.values) > 1:
            (first, first_in), (second, second_in) = readings.values[0], readings.values[1]
            return SpecVerificationResult(
                assertion=assertion,
                file_path=first_in,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=(
                    f"Pattern reads '{first}' in {os.path.basename(first_in)} and "
                    f"'{second}' in {os.path.basename(second_in)}; no single value to compare"
                ),
            )

        actual, found_in = readings.values[0]
        verified = assertion.expected_value.strip() == actual.strip()
        return SpecVerificationResult(
            assertion=assertion,
            outcome=(VerificationOutcome.VERIFIED if verified else VerificationOutcome.DISCREPANCY),
            actual_value=actual,
            file_path=found_in,
            detail=(
                f"Expected '{assertion.expected_value}', found '{actual}' "
                f"in {os.path.basename(found_in)}"
            ),
        )

    def _verify_constant(self, assertion: SpecAssertion) -> SpecVerificationResult:
        """Verify a T1 constant/config assertion by searching source files."""
        if not assertion.pattern:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail="No pattern to verify",
            )

        files = self._find_files(assertion.file_hint)
        if not files:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=f"No files matched hint: {assertion.file_hint}",
            )

        pattern = self._safe_compile(
            assertion.pattern, file_hint=assertion.file_hint, candidates=files
        )
        if pattern is None:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail="Unusable regex pattern: invalid, too long, or able to match a file with no content",
            )

        readable_files = 0
        first_match_in: str | None = None
        readings = _Readings()
        for file_path in files:
            content = self._read_file(file_path)
            if content is None:
                continue
            readable_files += 1

            # Lazily, and no further than the outcome depends on: the pattern is
            # untrusted model output, so every byte scanned after the answer is
            # settled is work an author of the pattern chose.
            occurrences = pattern.finditer(content)
            first_occurrence = next(occurrences, None)
            if first_occurrence is None:
                continue
            if not assertion.expected_value:
                # Pattern found, no expected value to check
                first_reading = self._extract_value_after_match(content, first_occurrence)
                return SpecVerificationResult(
                    assertion=assertion,
                    outcome=VerificationOutcome.VERIFIED,
                    actual_value=first_reading if isinstance(first_reading, str) else "",
                    file_path=file_path,
                    detail=f"Pattern found in {os.path.basename(file_path)}",
                )
            if first_match_in is None:
                first_match_in = file_path
            readings.add(self._extract_value_after_match(content, first_occurrence), file_path)
            read_here = 1
            while not readings.settled:
                if read_here >= MAX_OCCURRENCES_PER_FILE:
                    # The cap was reached with the readings still agreeing. What
                    # the rest of the file says is unknown, and unknown cannot be
                    # part of an agreement, so it counts as a binding gone unread.
                    readings.add(UNREAD_BINDING, file_path)
                    break
                occurrence = next(occurrences, None)
                if occurrence is None:
                    break
                readings.add(self._extract_value_after_match(content, occurrence), file_path)
                read_here += 1
            if readings.settled:
                # Readings that differ, or a binding that could not be read, already
                # settle it, and stopping here keeps both the work and the reported
                # detail bounded.
                break

        if first_match_in is not None:
            return self._verdict_from_readings(assertion, readings, first_match_in)

        if readable_files == 0:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=f"Could not read any of {len(files)} matched files",
            )

        # A usable pattern did not match any readable candidate: a real discrepancy.
        return SpecVerificationResult(
            assertion=assertion,
            outcome=VerificationOutcome.DISCREPANCY,
            detail=f"Pattern '{assertion.pattern}' not found in {len(files)} files",
        )

    def _verify_structural(self, assertion: SpecAssertion) -> SpecVerificationResult:
        """Verify a T2 structural assertion (file/class/function exists)."""
        if not assertion.pattern:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail="No pattern to verify",
            )

        files = self._find_files(assertion.file_hint)
        if not files:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=f"No files matched hint: {assertion.file_hint}",
            )

        # First check: does the pattern match any filename?
        name_pattern = self._safe_compile(assertion.pattern, re.IGNORECASE)

        if name_pattern:
            for file_path in files:
                basename = os.path.basename(file_path)
                if name_pattern.search(basename):
                    return SpecVerificationResult(
                        assertion=assertion,
                        outcome=VerificationOutcome.VERIFIED,
                        file_path=file_path,
                        detail=f"Found file: {basename}",
                    )

        # Second check: search file contents for class/function/interface
        content_pattern = self._safe_compile(
            assertion.pattern, file_hint=assertion.file_hint, candidates=files
        )
        if content_pattern is None:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail="Unusable regex pattern: invalid, too long, or able to match a file with no content",
            )

        readable_files = 0
        for file_path in files:
            content = self._read_file(file_path)
            if content is None:
                continue
            readable_files += 1
            if content_pattern.search(content):
                return SpecVerificationResult(
                    assertion=assertion,
                    outcome=VerificationOutcome.VERIFIED,
                    file_path=file_path,
                    detail=f"Pattern found in {os.path.basename(file_path)}",
                )

        if files and readable_files == 0:
            return SpecVerificationResult(
                assertion=assertion,
                outcome=VerificationOutcome.UNVERIFIABLE,
                detail=f"Could not read any of {len(files)} matched files",
            )

        return SpecVerificationResult(
            assertion=assertion,
            outcome=VerificationOutcome.DISCREPANCY,
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

    def _extract_value_after_match(self, content: str, match: re.Match) -> Reading:
        """Extract the value immediately following a regex match.

        Handles common patterns:
        - VAR = 10
        - VAR: 10
        - VAR(10)
        - "value"

        Returns ``None`` when nothing is bound at the match, and
        ``UNREAD_BINDING`` when something is bound there that this cannot read.
        """
        return _extract_following_scalar(content, match.end())
