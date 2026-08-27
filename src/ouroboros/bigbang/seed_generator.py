"""Seed generation module for transforming interview results to immutable Seeds.

This module implements the transformation from InterviewState to Seed,
gating on ambiguity score (must be <= 0.2) to ensure requirements are
clear enough for execution.

The SeedGenerator:
1. Validates ambiguity score is within threshold
2. Uses LLM to extract structured requirements from interview
3. Creates immutable Seed with proper metadata
4. Optionally saves to YAML file
"""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.bigbang.acceptance_criteria_parsing import (
    _parse_extracted_acceptance_criteria,
)
from ouroboros.bigbang.ambiguity import AMBIGUITY_THRESHOLD, AmbiguityScore
from ouroboros.bigbang.answer_provenance import extraction_rounds
from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    InterviewState,
    initial_context_summary_missing,
    prompt_safe_initial_context,
)
from ouroboros.bigbang.requirement_distillation import (
    apply_requirement_distillation,
    build_promoted_reference_seed,
    build_requirement_distillation,
    is_reference_aware_distillation,
    seed_readiness_details,
)
from ouroboros.config import get_llm_model_for_role
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.owner_only import write_owner_only
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    parse_expected_artifact_list,
)
from ouroboros.core.types import Result
from ouroboros.evolution.acceptance_contracts import evolve_seed_contract_fields
from ouroboros.providers.base import CompletionConfig, LLMAdapter, Message, MessageRole

log = structlog.get_logger()

EXTRACTION_TEMPERATURE = 0.2
_MAX_EXTRACTION_RETRIES = 1
_AC_RESERVED_FIELD_NAMES = ("verify", "artifacts", "expect")
_AC_FIELD_NAME_OBFUSCATORS = frozenset({"'", '"', "\\"})
_WORD_APOSTROPHE_SUFFIXES = frozenset({"d", "ll", "m", "re", "s", "t", "ve"})


@dataclass(frozen=True)
class _ACFieldMarker:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class _ACReservedFieldFragment:
    name: str
    end: int
    has_colon: bool
    canonical: bool


def _is_word_apostrophe(value: str, index: int, *, shell_context: bool = False) -> bool:
    if value[index] != "'" or index == 0 or not value[index - 1].isalnum():
        return False
    if shell_context:
        # POSIX has no contraction exception in command tokens: an apostrophe
        # always opens a single-quoted segment, including adjacent forms such
        # as ``foo're'``. Prose heuristics remain limited to descriptions and
        # non-command fields, so later possessives cannot hide outer markers.
        return False
    next_character = value[index + 1] if index + 1 < len(value) else ""
    if next_character.isspace() or (
        next_character.lower() == "s"
        and (index + 2 == len(value) or not value[index + 2].isalnum())
    ):
        return True

    # Recognize ordinary English contractions from the local word suffix.
    # Looking for any later apostrophe is incorrect: a later artifact or
    # assertion such as ``user's.txt`` must not turn the apostrophe in
    # ``don't`` into a quote that hides the structured fields in between.
    suffix_end = index + 1
    while suffix_end < len(value) and value[suffix_end].isascii() and value[suffix_end].isalpha():
        suffix_end += 1
    suffix = value[index + 1 : suffix_end].lower()
    if suffix in _WORD_APOSTROPHE_SUFFIXES and (
        suffix_end == len(value) or not value[suffix_end].isalnum()
    ):
        return True

    # Shell permits a quoted word to be adjacent to its command/token
    # (``printf'%s'`` and ``python -c'...'``). Other apostrophes with a local
    # closing mate therefore remain quote delimiters.
    return value.find("'", index + 1) < 0


def _starts_posix_shell_comment(char: str, *, word_started: bool) -> bool:
    """Return whether an unquoted ``#`` begins a fresh POSIX comment token."""
    return char == "#" and not word_started


def _ends_posix_shell_word(value: str, index: int) -> bool:
    """Return whether ``index`` starts a shell token boundary after a word."""
    return index >= len(value) or value[index].isspace() or value[index] in ";&|(){}<>"


@dataclass
class _PosixCaseTracker:
    """Track the reserved-word and pattern phases of nested POSIX case commands."""

    states: list[str] = field(default_factory=list)
    command_position: bool = True
    word_started: bool = False
    function_name_candidate: bool = False
    awaiting_function_body: bool = False
    for_phase: str | None = None
    invalid: bool = False

    @property
    def incomplete(self) -> bool:
        """Return whether a compound construct is unfinished or malformed."""
        return bool(self.states or self.for_phase is not None or self.invalid)

    def consume_segment(self) -> None:
        """Record a quote, escape, or expansion joined to the current shell word."""
        self.word_started = True
        self.function_name_candidate = False

    def consume_word(self, word: str, *, token_ends: bool) -> None:
        """Consume one unquoted identifier while preserving token provenance."""
        if self.awaiting_function_body:
            # POSIX function bodies are compound commands.  Braces and
            # subshells are handled by ``consume_grouping``; the remaining
            # valid direct forms begin with one of these reserved words.
            # Restore command position before consuming it so a ``case`` body
            # cannot expose reserved-looking pipes in its patterns as outer AC
            # fields (for example ``f() case x in x | artifacts:) ...``).
            if (
                not self.word_started
                and token_ends
                and word
                in {
                    "case",
                    "if",
                    "while",
                    "until",
                    "for",
                }
            ):
                self.awaiting_function_body = False
                self.command_position = True
            else:
                self.invalid = True
                self.awaiting_function_body = False
        reserved = not self.word_started and token_ends
        if self.for_phase == "await_name":
            self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            if token_ends:
                self.for_phase = "after_name"
            return
        if self.for_phase == "after_name":
            if reserved and word == "in":
                self.for_phase = "in_list"
            else:
                self.invalid = True
            self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            return
        if self.for_phase == "in_list":
            self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            return
        if self.for_phase == "await_do":
            if reserved and word == "do":
                self.for_phase = None
                self.command_position = True
            else:
                self.invalid = True
                self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            return
        state = self.states[-1] if self.states else None
        if state == "pattern" and not (reserved and self.command_position and word == "esac"):
            self.word_started = True
            self.function_name_candidate = False
            return
        was_command_position = self.command_position
        if reserved and self.command_position and word == "case":
            self.states.append("await_in")
            self.command_position = False
        elif reserved and state == "await_in" and word == "in":
            self.states[-1] = "pattern"
            self.command_position = True
        elif (
            reserved
            and self.states
            and self.command_position
            and word == "esac"
            and self.states[-1] in {"pattern", "action"}
        ):
            self.states.pop()
            self.command_position = False
        elif (
            reserved
            and self.command_position
            and word
            in {
                "if",
                "then",
                "elif",
                "else",
                "while",
                "until",
                "do",
                "for",
            }
        ):
            if word == "for":
                self.for_phase = "await_name"
                self.command_position = False
            else:
                self.command_position = True
        else:
            self.command_position = False
        self.function_name_candidate = (
            was_command_position
            and token_ends
            and not (
                reserved
                and word in {"case", "if", "then", "elif", "else", "while", "until", "do", "for"}
            )
        )
        self.word_started = self.for_phase != "await_name"

    def consume_function_header(self, value: str, index: int) -> int:
        """Consume the ``()`` after a plain command-position function name."""
        if self.function_name_candidate and value.startswith("()", index):
            self.function_name_candidate = False
            self.awaiting_function_body = True
            self.command_position = False
            self.word_started = False
            return 2
        return 0

    def consume_grouping(self, char: str) -> None:
        """Enter or leave a brace/subshell compound-command boundary."""
        if char == "{" and (self.command_position or self.awaiting_function_body):
            self.awaiting_function_body = False
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
        elif char == "(":
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
        else:
            self.command_position = False
            self.word_started = False
            self.function_name_candidate = False

    def consume_case_operator(self, value: str, index: int) -> int:
        """Consume case-only syntax and return the number of bytes claimed."""
        if self.states and self.states[-1] == "action" and value.startswith(";;", index):
            self.states[-1] = "pattern"
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
            return 2
        if self.states and self.states[-1] == "pattern":
            char = value[index]
            if char == ")":
                self.states[-1] = "action"
                self.command_position = True
                self.word_started = False
                self.function_name_candidate = False
                return 1
            if char in "(|":
                self.word_started = False
                self.function_name_candidate = False
                return 1
        return 0

    def consume_ordinary(self, char: str) -> None:
        """Update token position for non-case shell text."""
        if char.isspace():
            if self.for_phase == "await_name" and self.word_started:
                self.for_phase = "after_name"
            if char == "\n" and self.for_phase in {"after_name", "in_list"}:
                self.for_phase = "await_do"
                self.command_position = True
            self.word_started = False
        elif char in ";|&\n" or (char == "!" and self.command_position and not self.word_started):
            if self.for_phase == "await_name" and self.word_started:
                self.for_phase = "after_name"
            if char == ";" and self.for_phase in {"after_name", "in_list"}:
                self.for_phase = "await_do"
            elif self.for_phase is not None and char in "|&":
                self.invalid = True
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
        else:
            self.word_started = True
            self.function_name_candidate = False


def _posix_expansion_end(value: str, index: int) -> int | None:
    """Return the end of one balanced ``$()``/``${}`` shell expansion.

    A command substitution is not merely parenthesis-balanced shell text:
    each POSIX ``case`` pattern has an unmatched syntactic ``)``.  Track the
    small reserved-word state needed to keep those pattern terminators inside
    their substitution while leaving ordinary subshell parentheses on the
    existing frame stack.
    """
    if index + 1 >= len(value) or value[index] != "$" or value[index + 1] not in "({":
        return None
    frames: list[tuple[str, str | None, _PosixCaseTracker | None]] = [
        (")", None, _PosixCaseTracker()) if value[index + 1] == "(" else ("}", None, None)
    ]
    quote: str | None = None
    escaped = False
    cursor = index + 2
    while cursor < len(value):
        char = value[cursor]
        if quote == "'":
            if char == "'":
                quote = None
            cursor += 1
            continue
        if escaped:
            escaped = False
            cursor += 1
            continue
        if char == "\\":
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            escaped = True
            cursor += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                cursor += 1
                continue
            if char == "$" and cursor + 1 < len(value) and value[cursor + 1] in "({":
                tracker = frames[-1][2]
                if tracker is not None:
                    tracker.consume_segment()
                frames.append(
                    (")", quote, _PosixCaseTracker())
                    if value[cursor + 1] == "("
                    else ("}", quote, None)
                )
                quote = None
                cursor += 2
                continue
            if char == "`":
                substitution_end = _posix_backtick_substitution_end(value, cursor)
                if substitution_end is None:
                    return None
                tracker = frames[-1][2]
                if tracker is not None:
                    tracker.consume_segment()
                cursor = substitution_end
                continue
            cursor += 1
            continue
        if char in {"'", '"'}:
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            quote = char
            cursor += 1
            continue
        if char == "`":
            substitution_end = _posix_backtick_substitution_end(value, cursor)
            if substitution_end is None:
                return None
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            cursor = substitution_end
            continue
        if char == "$" and cursor + 1 < len(value) and value[cursor + 1] in "({":
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            frames.append(
                (")", quote, _PosixCaseTracker())
                if value[cursor + 1] == "("
                else ("}", quote, None)
            )
            quote = None
            cursor += 2
            continue
        closer, _, tracker = frames[-1]
        if tracker is not None and _starts_posix_shell_comment(
            char, word_started=tracker.word_started
        ):
            newline = value.find("\n", cursor + 1)
            if newline < 0:
                return None
            tracker.consume_ordinary("\n")
            cursor = newline + 1
            continue
        if tracker is not None and (char.isascii() and (char.isalpha() or char == "_")):
            word_end = cursor + 1
            while word_end < len(value) and (
                value[word_end].isascii() and (value[word_end].isalnum() or value[word_end] == "_")
            ):
                word_end += 1
            tracker.consume_word(
                value[cursor:word_end],
                token_ends=_ends_posix_shell_word(value, word_end),
            )
            cursor = word_end
            continue
        if tracker is not None:
            claimed = tracker.consume_case_operator(value, cursor)
            if claimed:
                cursor += claimed
                continue
            claimed = tracker.consume_function_header(value, cursor)
            if claimed:
                cursor += claimed
                continue
        if char == "(":
            arithmetic = cursor >= 2 and value[cursor - 2 : cursor] == "$("
            frames.append((")", quote, None if arithmetic else _PosixCaseTracker()))
            cursor += 1
            continue
        if char == frames[-1][0]:
            _, return_quote, closing_tracker = frames.pop()
            if closing_tracker is not None and closing_tracker.incomplete:
                return None
            quote = return_quote
            cursor += 1
            if not frames:
                return cursor
            parent_tracker = frames[-1][2]
            if parent_tracker is not None:
                parent_tracker.consume_segment()
            continue
        if tracker is not None:
            if char in "{}":
                tracker.consume_grouping(char)
                cursor += 1
                continue
            tracker.consume_ordinary(char)
        cursor += 1
    return None


def _scan_pipe_led_ac_field_fragment(
    value: str,
    pipe_index: int,
) -> _ACReservedFieldFragment | None:
    """Recognize reserved AC fields after normalizing token obfuscators."""
    if pipe_index < 0 or pipe_index >= len(value) or value[pipe_index] != "|":
        return None

    index = pipe_index + 1
    normalized = ""
    obfuscated = False
    while index < len(value):
        char = value[index]
        if char.isascii() and char.isalpha():
            normalized += char.lower()
            if not any(name.startswith(normalized) for name in _AC_RESERVED_FIELD_NAMES):
                return None
            index += 1
            if normalized not in _AC_RESERVED_FIELD_NAMES:
                continue
            if index < len(value) and (value[index].isalnum() or value[index] == "_"):
                return None

            # Whitespace after a complete canonical token is a boundary, not
            # field-name obfuscation. It may lead to an outer ``name :`` marker
            # or to quoted/escaped shell arguments such as ``verify "$mode"``
            # and ``verify \--mode``. Obfuscators encountered *before* the name
            # completed remain recorded by the loop below and fail closed.
            field_end = index
            saw_argument_boundary = False
            while field_end < len(value) and value[field_end].isspace():
                saw_argument_boundary = True
                field_end += 1
            if (
                not saw_argument_boundary
                and field_end < len(value)
                and value[field_end] in _AC_FIELD_NAME_OBFUSCATORS
            ):
                obfuscated = True
                while field_end < len(value) and (
                    value[field_end].isspace() or value[field_end] in _AC_FIELD_NAME_OBFUSCATORS
                ):
                    field_end += 1
            has_colon = field_end < len(value) and value[field_end] == ":"
            return _ACReservedFieldFragment(
                name=normalized,
                end=field_end + 1 if has_colon else index,
                has_colon=has_colon,
                canonical=not obfuscated,
            )
        if char.isspace() or char in _AC_FIELD_NAME_OBFUSCATORS:
            if char in _AC_FIELD_NAME_OBFUSCATORS or normalized:
                obfuscated = True
            index += 1
            continue
        return None
    return None


def _find_pipe_led_ac_field_fragment(value: str) -> _ACReservedFieldFragment | None:
    pipe_index = value.find("|")
    while pipe_index >= 0:
        fragment = _scan_pipe_led_ac_field_fragment(value, pipe_index)
        if fragment is not None:
            return fragment
        pipe_index = value.find("|", pipe_index + 1)
    return None


def _is_reserved_verify_pipeline_command(
    fragment: _ACReservedFieldFragment,
    *,
    active_field: str | None,
) -> bool:
    """Return whether a reserved token is command payload, not outer syntax.

    A colonless ``verify`` following an established ``verify:`` marker is
    unambiguously part of that shell payload, including any arguments that
    follow it. Colonless ``artifacts``/``expect`` tokens remain fail-closed
    because they are ambiguous with malformed next-field markers.
    """
    return (
        active_field == "verify"
        and fragment.name == "verify"
        and fragment.canonical
        and not fragment.has_colon
    )


def _parse_string_array_values(
    raw_value: object, *, field_label: str, strict: bool = False
) -> tuple[str, ...]:
    """Parse a list-valued extraction field from a JSON array, a sequence, or a legacy pipe list.

    The extraction format requests a single-line JSON array so literal pipe
    characters inside one constraint survive as data (#1696).

    In strict mode (extraction time) the value must be a valid JSON array of
    strings regardless of shape — plain pipe lists, bracket prose, and any
    other non-array text raise so the extraction retry path can ask the LLM
    to reformat into the JSON array the extraction prompt already demands.
    The strict boundary therefore has no fallback surface at all. In lenient
    mode (stored legacy requirements consumed at seed-build time) invalid
    JSON falls back to the historical pipe split, including bracket-prefixed
    prose such as ``[P0] Must work offline``, and never raises: stored data
    has no retry path.

    Raises:
        ValueError: Only in strict mode, when the value is not a valid JSON
            array of strings.
    """
    if isinstance(raw_value, list | tuple):
        if strict:
            non_strings = tuple(
                type(entry).__name__ for entry in raw_value if not isinstance(entry, str)
            )
            if non_strings:
                raise ValueError(
                    f"{field_label} array must contain only strings; got {', '.join(non_strings)}."
                )
        return tuple(item for item in (str(entry).strip() for entry in raw_value) if item)
    if not isinstance(raw_value, str):
        return ()
    text = raw_value.strip()
    if not text:
        return ()
    if strict:
        try:
            decoded = json.loads(
                text, parse_int=_bounded_json_int, parse_constant=_JsonNonFiniteToken
            )
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{field_label} must be a single-line JSON array of strings: {e}. Value: {text[:200]}"
            ) from e
        if not isinstance(decoded, list):
            raise ValueError(
                f"{field_label} must be a JSON array of strings, got "
                f"{type(decoded).__name__}. Value: {text[:200]}"
            )
        non_strings = tuple(type(entry).__name__ for entry in decoded if not isinstance(entry, str))
        if non_strings:
            raise ValueError(
                f"{field_label} JSON array must contain only strings; got "
                f"{', '.join(non_strings)}. Value: {text[:200]}"
            )
        return tuple(item for item in (entry.strip() for entry in decoded) if item)
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, list):
                return tuple(item for item in (str(entry).strip() for entry in decoded) if item)
    return tuple(item.strip() for item in text.split("|") if item.strip())


def _iter_outer_ac_field_markers(body: str) -> tuple[_ACFieldMarker, ...]:
    """Return structured AC field markers found outside quoted/escaped payloads."""
    markers: list[_ACFieldMarker] = []
    quote: str | None = None
    quote_start: int | None = None
    structured_payload_started = False
    active_field: str | None = None
    escaped = False
    unquoted_comment = False
    shell_tracker = _PosixCaseTracker()
    index = 0
    while index < len(body):
        char = body[index]
        if unquoted_comment:
            fragment = _scan_pipe_led_ac_field_fragment(body, index) if char == "|" else None
            if fragment is None or not fragment.has_colon or not fragment.canonical:
                index += 1
                continue
            # The AC extraction DSL terminates the verify substring before it
            # reaches the shell. Its canonical outer marker therefore ends
            # scanner comment state even though an untrimmed shell line would
            # treat the same bytes as comment text.
            unquoted_comment = False
        if quote == "'":
            # POSIX single quotes make every enclosed character literal.
            # In particular, a backslash cannot escape the closing quote.
            if char == "'":
                if not structured_payload_started and quote_start is not None:
                    quoted_payload = body[quote_start:index]
                    quoted_marker = _find_pipe_led_ac_field_fragment(quoted_payload)
                    if quoted_marker is not None:
                        raise ValueError(
                            f"Quoted {quoted_marker.name} field in acceptance criterion"
                        )
                quote = None
                quote_start = None
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            if not structured_payload_started:
                escaped_remainder = body[index + 1 :]
                escaped_marker = _scan_pipe_led_ac_field_fragment(escaped_remainder, 0)
                if escaped_marker is not None:
                    raise ValueError(f"Escaped {escaped_marker.name} field in acceptance criterion")
            if structured_payload_started and active_field == "verify" and quote is None:
                # The escaped byte belongs to the current word even when its
                # raw spelling is whitespace or a control operator. Preserve
                # that provenance so a following # cannot become a comment.
                shell_tracker.consume_segment()
            escaped = True
            index += 1
            continue
        if (
            structured_payload_started
            and active_field == "verify"
            and char == "$"
            and index + 1 < len(body)
            and body[index + 1] in "({"
            and quote in {None, '"'}
        ):
            expansion_end = _posix_expansion_end(body, index)
            if expansion_end is None:
                # Reuse the existing fail-closed unterminated-contract path.
                quote = quote or "$("
                index = len(body)
                continue
            shell_tracker.consume_segment()
            index = expansion_end
            continue
        if (
            structured_payload_started
            and active_field == "verify"
            and char == "`"
            and quote in {None, '"'}
        ):
            substitution_end = _posix_backtick_substitution_end(body, index)
            if substitution_end is None:
                # A legacy command substitution may contain shell syntax that
                # resembles the outer AC fields. Fail closed instead of
                # exposing that payload to the field scanner.
                quote = quote or "`"
                index = len(body)
                continue
            shell_tracker.consume_segment()
            index = substitution_end
            continue
        if quote is not None:
            if char == quote:
                if not structured_payload_started and quote_start is not None:
                    quoted_payload = body[quote_start:index]
                    quoted_marker = _find_pipe_led_ac_field_fragment(quoted_payload)
                    if quoted_marker is not None:
                        raise ValueError(
                            f"Quoted {quoted_marker.name} field in acceptance criterion"
                        )
                quote = None
                quote_start = None
            index += 1
            continue
        if (
            structured_payload_started
            and active_field == "verify"
            and _starts_posix_shell_comment(char, word_started=shell_tracker.word_started)
        ):
            unquoted_comment = True
            index += 1
            continue
        if char in {"'", '"'} and not (
            char == "'"
            and _is_word_apostrophe(
                body,
                index,
                shell_context=structured_payload_started and active_field == "verify",
            )
        ):
            if structured_payload_started and active_field == "verify":
                shell_tracker.consume_segment()
            quote = char
            quote_start = index + 1
            index += 1
            continue
        if (
            structured_payload_started
            and active_field == "verify"
            and char.isascii()
            and (char.isalpha() or char == "_")
        ):
            word_end = index + 1
            while word_end < len(body) and (
                body[word_end].isascii() and (body[word_end].isalnum() or body[word_end] == "_")
            ):
                word_end += 1
            shell_tracker.consume_word(
                body[index:word_end],
                token_ends=_ends_posix_shell_word(body, word_end),
            )
            index = word_end
            continue
        if structured_payload_started and active_field == "verify":
            claimed = shell_tracker.consume_case_operator(body, index)
            if claimed:
                index += claimed
                continue
            claimed = shell_tracker.consume_function_header(body, index)
            if claimed:
                index += claimed
                continue
            if char in "(){}":
                shell_tracker.consume_grouping(char)
                index += 1
                continue
        if char != "|":
            if structured_payload_started and active_field == "verify":
                shell_tracker.consume_ordinary(char)
            index += 1
            continue

        if active_field == "verify" and shell_tracker.incomplete:
            # A complete top-level case owns every pipeline until ``esac``.
            # Reserved-looking bytes in its patterns or actions cannot become
            # outer acceptance-contract fields.
            shell_tracker.consume_ordinary("|")
            index += 1
            continue

        fragment = _scan_pipe_led_ac_field_fragment(body, index)
        if fragment is not None and fragment.has_colon and fragment.canonical:
            structured_payload_started = True
            active_field = fragment.name
            shell_tracker = _PosixCaseTracker()
            markers.append(
                _ACFieldMarker(
                    name=fragment.name,
                    start=index,
                    end=fragment.end,
                )
            )
            index = fragment.end
            continue
        if (
            fragment is not None
            and structured_payload_started
            and _is_reserved_verify_pipeline_command(fragment, active_field=active_field)
        ):
            # Once a structured payload has started, a canonical reserved word
            # without ``:`` can be an ordinary verify pipeline command with
            # arguments. Only ``| name:`` is outer AC syntax; obfuscated
            # spellings and ambiguous artifacts/expect fragments stay rejected.
            index += 1
            continue
        if fragment is not None:
            raise ValueError(f"Malformed {fragment.name} field in acceptance criterion")
        if active_field == "verify":
            shell_tracker.consume_ordinary("|")
        index += 1
    if (
        quote is not None or escaped or (active_field == "verify" and shell_tracker.incomplete)
    ) and _find_pipe_led_ac_field_fragment(body):
        raise ValueError("Unterminated quoted or escaped acceptance criterion contract")
    return tuple(markers)


class _JsonNonFiniteToken:
    """Marks a non-standard JSON constant (NaN/Infinity) seen during decoding.

    These tokens are not JSON numbers, so strict extraction must retry them
    while lenient parsing falls back to the default weight — unlike genuine
    numeric overflow, which saturates by sign (review round three).
    """

    def __init__(self, token: str) -> None:
        self.token = token

    def __repr__(self) -> str:
        return self.token


_JSON_INT_SATURATION_DIGITS = 400


def _bounded_json_int(text: str) -> int | float:
    """Decode a JSON integer, saturating past a fixed digit bound.

    CPython raises a plain ``ValueError`` (not ``JSONDecodeError``) when an
    integer literal exceeds its conversion digit limit, which would escape the
    lenient never-raises contract and turn a syntactically valid number into a
    strict retry. Anything beyond the bound saturates to a signed infinity so
    the weight clamp resolves it deterministically in both modes.
    """
    digits = text.lstrip("+-")
    if len(digits) > _JSON_INT_SATURATION_DIGITS:
        return float("-inf") if text.lstrip().startswith("-") else float("inf")
    return int(text)


def _require_object_string(
    entry: dict, key: str, *, field_label: str, aliases: tuple[str, ...] = ()
) -> str:
    """Return a required non-empty string value from an extraction object entry."""
    for candidate in (key, *aliases):
        if candidate in entry:
            value = entry[candidate]
            if isinstance(value, str) and value.strip():
                return value.strip()
            raise ValueError(
                f"{field_label} entry field {candidate!r} must be a non-empty string; "
                f"got {value!r}."
            )
    raise ValueError(f"{field_label} entry is missing required field {key!r}: {entry!r}")


def _clamp_weight(raw_weight: object, *, field_label: str, strict: bool) -> float:
    """Resolve an optional principle weight, clamped to [0.0, 1.0].

    Strict mode requires a JSON number (bools rejected) so malformed weights
    trigger the extraction retry; lenient mode keeps the historical behavior
    of falling back to 1.0 on anything unparseable.
    """
    if raw_weight is None:
        if strict:
            raise ValueError(
                f"{field_label} entry field 'weight' must be omitted or a number; got null."
            )
        return 1.0
    if isinstance(raw_weight, bool) or not isinstance(raw_weight, int | float):
        if strict:
            raise ValueError(
                f"{field_label} entry field 'weight' must be a number; got {raw_weight!r}."
            )
        if isinstance(raw_weight, _JsonNonFiniteToken):
            # Non-standard constants (NaN/Infinity/-Infinity) are not JSON
            # numbers, so they take the documented 1.0 default rather than
            # the sign-saturation reserved for genuine numeric overflow;
            # without this check float(str(...)) below would turn a stored
            # -Infinity literal into a 0.0 clamp (#1766).
            return 1.0
        try:
            numeric = float(str(raw_weight).strip())
        except (ValueError, OverflowError):
            return 1.0
        if math.isnan(numeric):
            return 1.0
        if math.isinf(numeric):
            # Saturate overflow by sign so a negative overflow keeps the
            # historical 0.0 clamp (review round three).
            return 1.0 if numeric > 0 else 0.0
        return min(1.0, max(0.0, numeric))
    if isinstance(raw_weight, int):
        # Clamp in integer space: arbitrary-precision JSON integers would
        # overflow float() (an OverflowError the retry path does not catch),
        # and the clamp result is what any out-of-range number gets anyway.
        return float(min(max(raw_weight, 0), 1))
    if not math.isfinite(raw_weight):
        # A NaN float cannot come from a JSON number (the non-standard tokens
        # decode to _JsonNonFiniteToken and are handled above), so it only
        # appears in pre-decoded input: strict retries it, lenient defaults.
        if math.isnan(raw_weight):
            if strict:
                raise ValueError(
                    f"{field_label} entry field 'weight' must be a finite number; "
                    f"got {raw_weight!r}."
                )
            return 1.0
        # Genuine numeric overflow (e.g. 1e999 or an integer past the decode
        # bound) is a valid JSON number and follows the clamp contract in both
        # modes, saturating by sign (review round three).
        return 1.0 if raw_weight > 0 else 0.0
    return min(1.0, max(0.0, raw_weight))


def _decode_object_array(text: str, *, field_label: str, strict: bool) -> list | None:
    """Decode an extraction field into a JSON list, honoring the strict boundary.

    Strict mode raises unless the text is a valid JSON array — the retry path
    asks the LLM to reformat (#1729). Lenient mode returns the decoded list
    only for valid bracket-led JSON arrays and ``None`` otherwise so callers
    fall back to the historical colon/pipe split.
    """
    if strict:
        try:
            decoded = json.loads(
                text, parse_int=_bounded_json_int, parse_constant=_JsonNonFiniteToken
            )
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{field_label} must be a single-line JSON array of objects: {e}. "
                f"Value: {text[:200]}"
            ) from e
        if not isinstance(decoded, list):
            raise ValueError(
                f"{field_label} must be a JSON array of objects, got "
                f"{type(decoded).__name__}. Value: {text[:200]}"
            )
        return decoded
    if not text.startswith("["):
        return None
    try:
        decoded = json.loads(text, parse_int=_bounded_json_int, parse_constant=_JsonNonFiniteToken)
    except (json.JSONDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, list) else None


def _parse_evaluation_principles(
    raw_value: object, *, strict: bool = False
) -> tuple[EvaluationPrinciple, ...]:
    """Parse EVALUATION_PRINCIPLES from JSON object arrays or the legacy pipe list.

    The extraction format requests a single-line JSON array of
    ``{"name", "description", "weight"}`` objects so colons and pipes inside
    the data survive (#1729 slice 2). Strict mode (extraction time) raises on
    anything else so the retry path can reformat; lenient mode (stored legacy
    requirements) keeps the historical ``name:description:weight`` pipe split
    and never raises.
    """
    field_label = "EVALUATION_PRINCIPLES"

    def _build(entry: object) -> EvaluationPrinciple | None:
        if isinstance(entry, EvaluationPrinciple):
            return entry
        if not isinstance(entry, dict):
            if strict:
                raise ValueError(
                    f"{field_label} entries must be JSON objects; got "
                    f"{type(entry).__name__}: {entry!r}"
                )
            return None
        try:
            return EvaluationPrinciple(
                name=_require_object_string(entry, "name", field_label=field_label),
                description=_require_object_string(entry, "description", field_label=field_label),
                weight=(
                    _clamp_weight(entry["weight"], field_label=field_label, strict=strict)
                    if "weight" in entry
                    else 1.0
                ),
            )
        except (ValueError, TypeError, PydanticValidationError):
            if strict:
                raise
            return None

    def _build_all(entries: list | tuple) -> tuple[EvaluationPrinciple, ...]:
        return tuple(built for built in (_build(entry) for entry in entries) if built)

    if isinstance(raw_value, list | tuple):
        return _build_all(raw_value)
    if not isinstance(raw_value, str):
        return ()
    text = raw_value.strip()
    if not text:
        if strict:
            raise ValueError(f"{field_label} must be an explicit JSON array; got a blank value.")
        return ()
    decoded = _decode_object_array(text, field_label=field_label, strict=strict)
    if decoded is not None:
        return _build_all(decoded)

    principles: list[EvaluationPrinciple] = []
    for principle_str in text.split("|"):
        principle_str = principle_str.strip()
        if not principle_str:
            continue
        parts = principle_str.split(":")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        description = parts[1].strip()
        if not name or not description:
            continue
        principles.append(
            EvaluationPrinciple(
                name=name,
                description=description,
                weight=_clamp_weight(
                    parts[2].strip() if len(parts) >= 3 else None,
                    field_label=field_label,
                    strict=False,
                ),
            )
        )
    return tuple(principles)


def _parse_exit_conditions(raw_value: object, *, strict: bool = False) -> tuple[ExitCondition, ...]:
    """Parse EXIT_CONDITIONS from JSON object arrays or the legacy pipe list.

    Mirrors :func:`_parse_evaluation_principles`: strict extraction demands a
    JSON array of ``{"name", "description", "criteria"}`` objects (the
    ``evaluation_criteria`` spelling is accepted too); lenient parsing keeps
    the historical ``name:description:criteria`` split where the criteria
    segment absorbs any further colons, and never raises.
    """
    field_label = "EXIT_CONDITIONS"

    def _build(entry: object) -> ExitCondition | None:
        if isinstance(entry, ExitCondition):
            return entry
        if not isinstance(entry, dict):
            if strict:
                raise ValueError(
                    f"{field_label} entries must be JSON objects; got "
                    f"{type(entry).__name__}: {entry!r}"
                )
            return None
        try:
            return ExitCondition(
                name=_require_object_string(entry, "name", field_label=field_label),
                description=_require_object_string(entry, "description", field_label=field_label),
                evaluation_criteria=_require_object_string(
                    entry,
                    "criteria",
                    field_label=field_label,
                    aliases=("evaluation_criteria",),
                ),
            )
        except (ValueError, TypeError, PydanticValidationError):
            if strict:
                raise
            return None

    def _build_all(entries: list | tuple) -> tuple[ExitCondition, ...]:
        return tuple(built for built in (_build(entry) for entry in entries) if built)

    if isinstance(raw_value, list | tuple):
        return _build_all(raw_value)
    if not isinstance(raw_value, str):
        return ()
    text = raw_value.strip()
    if not text:
        if strict:
            raise ValueError(f"{field_label} must be an explicit JSON array; got a blank value.")
        return ()
    decoded = _decode_object_array(text, field_label=field_label, strict=strict)
    if decoded is not None:
        return _build_all(decoded)

    conditions: list[ExitCondition] = []
    for condition_str in text.split("|"):
        condition_str = condition_str.strip()
        if not condition_str:
            continue
        parts = condition_str.split(":")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        description = parts[1].strip()
        criteria = ":".join(parts[2:]).strip()
        if not name or not description or not criteria:
            continue
        conditions.append(
            ExitCondition(
                name=name,
                description=description,
                evaluation_criteria=criteria,
            )
        )
    return tuple(conditions)


def _parse_ontology_fields(raw_value: object, *, strict: bool = False) -> tuple[OntologyField, ...]:
    """Parse ONTOLOGY_FIELDS from JSON object arrays or the legacy pipe list.

    Mirrors the evaluation-principle and exit-condition parsers (#1729): the
    extraction format requests a single-line JSON array of ``{"name", "type",
    "description"}`` objects (the model field spelling ``field_type`` is
    accepted too, and an optional boolean ``required`` is honored) so colons
    and pipes inside the data survive. Strict mode raises on anything else so
    the retry path reformats; lenient mode keeps the historical
    ``name:type:description`` split where the description absorbs any further
    colons, skips malformed entries, and never raises.
    """
    field_label = "ONTOLOGY_FIELDS"

    def _build(entry: object) -> OntologyField | None:
        if isinstance(entry, OntologyField):
            return entry
        if not isinstance(entry, dict):
            if strict:
                raise ValueError(
                    f"{field_label} entries must be JSON objects; got "
                    f"{type(entry).__name__}: {entry!r}"
                )
            return None
        try:
            required = entry.get("required", True)
            if not isinstance(required, bool):
                raise ValueError(
                    f"{field_label} entry field 'required' must be a boolean; got {required!r}."
                )
            return OntologyField(
                name=_require_object_string(entry, "name", field_label=field_label),
                field_type=_require_object_string(
                    entry, "type", field_label=field_label, aliases=("field_type",)
                ),
                description=_require_object_string(entry, "description", field_label=field_label),
                required=required,
            )
        except (ValueError, TypeError, PydanticValidationError):
            if strict:
                raise
            return None

    def _build_all(entries: list | tuple) -> tuple[OntologyField, ...]:
        return tuple(built for built in (_build(entry) for entry in entries) if built)

    if isinstance(raw_value, list | tuple):
        return _build_all(raw_value)
    if not isinstance(raw_value, str):
        return ()
    text = raw_value.strip()
    if not text:
        if strict:
            raise ValueError(
                f"{field_label} must be a single-line JSON array of objects; "
                "use [] for an intentionally empty list."
            )
        return ()
    decoded = _decode_object_array(text, field_label=field_label, strict=strict)
    if decoded is not None:
        return _build_all(decoded)

    fields: list[OntologyField] = []
    for field_str in text.split("|"):
        field_str = field_str.strip()
        if not field_str:
            continue
        parts = field_str.split(":")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        field_type = parts[1].strip()
        description = ":".join(parts[2:]).strip()
        if not name or not field_type or not description:
            # Stored legacy data has no retry path; skip malformed entries
            # instead of raising at model construction.
            continue
        fields.append(OntologyField(name=name, field_type=field_type, description=description))
    return tuple(fields)


def _parse_context_references(
    raw_value: object, *, strict: bool = False
) -> tuple[ContextReference, ...]:
    """Parse CONTEXT_REFERENCES from JSON object arrays or the legacy pipe list.

    Mirrors the other #1729 object-array parsers: the extraction format
    requests a single-line JSON array of ``{"path", "role", "summary"}``
    objects — ``summary`` is optional and defaults to empty — so colons in
    paths (e.g. Windows drives) and colons/pipes in summaries survive as
    data. Strict mode raises on anything else so the retry path reformats;
    lenient mode keeps the historical ``path:role:summary`` split where the
    summary absorbs further colons, skips malformed entries, and never
    raises.
    """
    field_label = "CONTEXT_REFERENCES"

    def _build(entry: object) -> ContextReference | None:
        if isinstance(entry, ContextReference):
            return entry
        if not isinstance(entry, dict):
            if strict:
                raise ValueError(
                    f"{field_label} entries must be JSON objects; got "
                    f"{type(entry).__name__}: {entry!r}"
                )
            return None
        try:
            summary = entry.get("summary", "")
            if not isinstance(summary, str):
                raise ValueError(
                    f"{field_label} entry field 'summary' must be a string; got {summary!r}."
                )
            return ContextReference(
                path=_require_object_string(entry, "path", field_label=field_label),
                role=_require_object_string(entry, "role", field_label=field_label),
                summary=summary.strip(),
            )
        except (ValueError, TypeError, PydanticValidationError):
            if strict:
                raise
            return None

    def _build_all(entries: list | tuple) -> tuple[ContextReference, ...]:
        return tuple(built for built in (_build(entry) for entry in entries) if built)

    if isinstance(raw_value, list | tuple):
        return _build_all(raw_value)
    if not isinstance(raw_value, str):
        return ()
    text = raw_value.strip()
    if not text:
        if strict:
            raise ValueError(
                f"{field_label} must be a single-line JSON array of objects; "
                "use [] for an intentionally empty list."
            )
        return ()
    decoded = _decode_object_array(text, field_label=field_label, strict=strict)
    if decoded is not None:
        return _build_all(decoded)

    references: list[ContextReference] = []
    for ref_str in text.split("|"):
        ref_str = ref_str.strip()
        if not ref_str:
            continue
        parts = ref_str.split(":")
        if len(parts) < 2:
            continue
        path = parts[0].strip()
        role = parts[1].strip()
        if not path or not role:
            # Stored legacy data has no retry path; skip malformed entries
            # instead of raising at model construction.
            continue
        references.append(
            ContextReference(
                path=path,
                role=role,
                summary=":".join(parts[2:]).strip() if len(parts) > 2 else "",
            )
        )
    return tuple(references)


def _parse_acceptance_criteria_contracts(
    raw_value: object,
) -> tuple[AcceptanceCriterionSpec | str, ...]:
    """Parse legacy AC prose or structured AC success-contract lines."""
    if isinstance(raw_value, list | tuple):
        parsed_entries: list[AcceptanceCriterionSpec | str] = []
        for item in raw_value:
            if isinstance(item, AcceptanceCriterionSpec):
                parsed_entries.append(item)
                continue
            text = str(item).strip()
            if text:
                parsed_entries.append(text)
        entries = tuple(parsed_entries)
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return ()
        if "\nAC:" in text or text.startswith("AC:"):
            entries = tuple(line.strip() for line in text.splitlines() if line.strip())
        else:
            entries = tuple(item.strip() for item in text.split("|") if item.strip())
    else:
        return ()

    parsed: list[AcceptanceCriterionSpec | str] = []
    for entry in entries:
        if isinstance(entry, AcceptanceCriterionSpec):
            parsed.append(entry)
            continue
        if not entry.startswith("AC:"):
            parsed.append(entry)
            continue
        spec = _parse_acceptance_criterion_contract(entry)
        parsed.append(spec if spec is not None else entry.removeprefix("AC:").strip())
    return tuple(parsed)


def _parse_acceptance_criterion_contract(line: str) -> AcceptanceCriterionSpec | None:
    """Parse one structured AC line, falling back to description-only on gaps."""
    if not line.startswith("AC:"):
        return None
    body = line.removeprefix("AC:").strip()
    matches = _iter_outer_ac_field_markers(body)
    description_end = matches[0].start if matches else len(body)
    description = body[:description_end].strip()
    if not description:
        return None
    fields: dict[str, object] = {"description": description}
    seen_fields: set[str] = set()
    for index, match in enumerate(matches):
        normalized_key = match.name
        if normalized_key in seen_fields:
            raise ValueError(f"Duplicate {normalized_key} field in acceptance criterion")
        seen_fields.add(normalized_key)
        value_start = match.end
        value_end = matches[index + 1].start if index + 1 < len(matches) else len(body)
        normalized_value = body[value_start:value_end].strip(" ")
        if normalized_key == "artifacts" and not normalized_value:
            raise ValueError("Empty artifacts field; use artifacts: NONE")
        if not normalized_value or normalized_value.upper() == "NONE":
            continue
        if normalized_key == "verify":
            fields["verify_command"] = normalized_value
        elif normalized_key == "artifacts":
            fields["expected_artifacts"] = parse_expected_artifact_list(normalized_value)
        elif normalized_key == "expect":
            fields["output_assertion"] = normalized_value
    return AcceptanceCriterionSpec.model_validate(fields)


def _unsupported_verify_command_reason(command: str) -> str | None:
    if "\n" in command or "\r" in command:
        return "verify_command must be a single-line command"
    if _contains_posix_heredoc_operator(command):
        return "verify_command uses heredoc/multiline shell syntax; use python -c or pytest instead"
    return None


def _contains_posix_heredoc_operator(command: str) -> bool:
    """Return whether shell code contains an active ``<<``/``<<-`` operator.

    The delimiter is a POSIX shell word, so quoting may be backslash-based or
    fragmented (``<<\\EOF``, ``<<E"OF"``). Looking for a delimiter spelling
    with one regex is therefore bypassable. Instead, recognize the operator
    in shell lexical context: quoted strings, comments, parameter expansion,
    and arithmetic expansion cannot introduce a heredoc operator themselves.
    Ordinary comparison text embedded in those contexts remains valid.
    """
    quote: str | None = None
    escaped = False
    word_started = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if escaped:
            escaped = False
            word_started = True
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
            if char == "$" and index + 1 < len(command):
                is_parameter = command[index + 1] == "{"
                is_arithmetic = command.startswith("$((", index)
                if is_parameter or is_arithmetic:
                    expansion_end = _posix_expansion_end(command, index)
                    if expansion_end is not None:
                        inner_start = index + (3 if is_arithmetic else 2)
                        inner_end = expansion_end - (2 if is_arithmetic else 1)
                        if _contains_nested_shell_substitution_heredoc(
                            command[inner_start:inner_end]
                        ):
                            return True
                        index = expansion_end
                        continue
            if (
                char == "$"
                and command.startswith("$(", index)
                and not command.startswith("$((", index)
            ):
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    if _contains_posix_heredoc_operator(command[index + 2 : expansion_end - 1]):
                        return True
                    index = expansion_end
                    continue
            if char == "`":
                substitution_end = _posix_backtick_substitution_end(command, index)
                if substitution_end is None:
                    return True
                if _contains_posix_heredoc_operator(command[index + 1 : substitution_end - 1]):
                    return True
                index = substitution_end
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            word_started = True
            index += 1
            continue
        if char == "`":
            substitution_end = _posix_backtick_substitution_end(command, index)
            if substitution_end is None:
                return True
            if _contains_posix_heredoc_operator(command[index + 1 : substitution_end - 1]):
                return True
            word_started = True
            index = substitution_end
            continue
        if char == "#" and not word_started:
            return False
        if char == "$" and index + 1 < len(command):
            is_parameter = command[index + 1] == "{"
            is_arithmetic = command.startswith("$((", index)
            if is_parameter or is_arithmetic:
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    inner_start = index + (3 if is_arithmetic else 2)
                    inner_end = expansion_end - (2 if is_arithmetic else 1)
                    if _contains_nested_shell_substitution_heredoc(command[inner_start:inner_end]):
                        return True
                    word_started = True
                    index = expansion_end
                    continue
            if command[index + 1] == "(":
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    if _contains_posix_heredoc_operator(command[index + 2 : expansion_end - 1]):
                        return True
                    word_started = True
                    index = expansion_end
                    continue
        if char == "<" and index + 1 < len(command) and command[index + 1] == "<":
            return True
        if char.isspace() or char in ";|&()<>":
            word_started = False
        else:
            word_started = True
        index += 1
    return False


def _posix_backtick_substitution_end(value: str, index: int) -> int | None:
    """Return just past the next unescaped legacy command-substitution backtick."""
    if index >= len(value) or value[index] != "`":
        return None
    escaped = False
    cursor = index + 1
    while cursor < len(value):
        char = value[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            return cursor + 1
        cursor += 1
    return None


def _contains_nested_shell_substitution_heredoc(value: str) -> bool:
    """Inspect only executable substitutions inside parameter/arithmetic text.

    Raw ``<<`` in these frames is data or an arithmetic shift, not shell
    redirection. Nested ``$()`` and legacy backticks execute shell code and
    must therefore be scanned with the full heredoc detector.
    """
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == "$" and index + 1 < len(value) and value[index + 1] in "({":
            expansion_end = _posix_expansion_end(value, index)
            if expansion_end is not None:
                is_arithmetic = value.startswith("$((", index)
                is_parameter = value[index + 1] == "{"
                if is_arithmetic or is_parameter:
                    inner_start = index + (3 if is_arithmetic else 2)
                    inner_end = expansion_end - (2 if is_arithmetic else 1)
                    if _contains_nested_shell_substitution_heredoc(value[inner_start:inner_end]):
                        return True
                elif _contains_posix_heredoc_operator(value[index + 2 : expansion_end - 1]):
                    return True
                index = expansion_end
                continue
        if char == "`":
            substitution_end = _posix_backtick_substitution_end(value, index)
            if substitution_end is None:
                return True
            if _contains_posix_heredoc_operator(value[index + 1 : substitution_end - 1]):
                return True
            index = substitution_end
            continue
        index += 1
    return False


@dataclass
class SeedGenerator:
    """Generator for creating immutable Seeds from interview state.

    Transforms completed interviews with low ambiguity scores into
    structured, immutable Seed specifications.

    Example:
        generator = SeedGenerator(llm_adapter=LiteLLMAdapter())

        # Generate seed from interview
        result = await generator.generate(
            state=interview_state,
            ambiguity_score=ambiguity_result,
        )

        if result.is_ok:
            seed = result.value
            # Save to file
            save_result = await generator.save_seed(seed, Path("seed.yaml"))

    Note:
        The model can be configured via OuroborosConfig.clarification.default_model
        or passed directly to the constructor.
    """

    llm_adapter: LLMAdapter
    model: str | None = None
    model_is_explicit: bool = field(default=False, init=False)
    temperature: float = EXTRACTION_TEMPERATURE
    max_tokens: int = 4096
    output_dir: Path = field(default_factory=lambda: Path.home() / ".ouroboros" / "seeds")

    def __post_init__(self) -> None:
        """Ensure output directory exists."""
        self.model_is_explicit = self.model is not None
        if self.model is None:
            self.model = get_llm_model_for_role("seed_generation")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        state: InterviewState,
        ambiguity_score: AmbiguityScore,
        parent_seed: Seed | None = None,
        reflect_output: Any | None = None,
        *,
        force: bool = False,
    ) -> Result[Seed, ValidationError | ProviderError]:
        """Generate an immutable Seed from interview state or reflect output.

        Two modes:
        - Gen 1 (reflect_output=None): Extract from interview, gate on ambiguity.
        - Gen 2+ (reflect_output provided): Use refined ACs and ontology mutations
          from ReflectEngine. Skip ambiguity gating.

        When ``force=True``, the ambiguity threshold gate is bypassed but every
        other validation (initial-context summary, extraction, build) still runs.
        The real ``ambiguity_score.overall_score`` is recorded in metadata so
        forced seeds carry truthful provenance.

        Args:
            state: Completed interview state.
            ambiguity_score: The ambiguity score for the interview.
            parent_seed: Optional parent seed for evolutionary lineage.
            reflect_output: Optional ReflectOutput for Gen 2+ evolution.
            force: When True, bypass the ambiguity threshold gate. The real
                score is still stamped into ``SeedMetadata.ambiguity_score``.
                Defaults to False (gate enforced).

        Returns:
            Result containing the generated Seed or error.
        """
        # Gen 2+ path: use reflect output directly
        if reflect_output is not None and parent_seed is not None:
            return self.generate_from_reflect(parent_seed, reflect_output)

        log.info(
            "seed.generation.started",
            interview_id=state.interview_id,
            ambiguity_score=ambiguity_score.overall_score,
        )

        # Gate on ambiguity score (skipped when force=True)
        if force:
            log.warning(
                "seed.generation.ambiguity_gate_bypassed",
                interview_id=state.interview_id,
                ambiguity_score=ambiguity_score.overall_score,
                threshold=AMBIGUITY_THRESHOLD,
            )
        elif not ambiguity_score.is_ready_for_seed:
            log.warning(
                "seed.generation.ambiguity_too_high",
                interview_id=state.interview_id,
                ambiguity_score=ambiguity_score.overall_score,
                threshold=AMBIGUITY_THRESHOLD,
            )
            return Result.err(
                ValidationError(
                    f"Ambiguity score {ambiguity_score.overall_score:.2f} exceeds "
                    f"threshold {AMBIGUITY_THRESHOLD}. Cannot generate Seed.",
                    field="ambiguity_score",
                    value=ambiguity_score.overall_score,
                    details={
                        "threshold": AMBIGUITY_THRESHOLD,
                        "interview_id": state.interview_id,
                    },
                )
            )

        if initial_context_summary_missing(state):
            return Result.err(
                ValidationError(
                    "Initial context summary required before seed generation",
                    field="initial_context",
                    details={"interview_id": state.interview_id},
                )
            )

        distillation = build_requirement_distillation(state)
        preflight = apply_requirement_distillation({}, distillation)
        reference_aware = is_reference_aware_distillation(distillation)
        readiness = seed_readiness_details(
            preflight.promotion,
            require_promoted_acceptance_criteria=reference_aware,
        )
        if readiness["blockers"]:
            return Result.err(
                ValidationError(
                    "Interview must be reopened before Seed generation",
                    field="requirement_distillation",
                    details=readiness,
                )
            )
        state.requirement_distillation = distillation
        if reference_aware:
            return Result.ok(
                build_promoted_reference_seed(
                    state,
                    distillation,
                    ambiguity_score=ambiguity_score.overall_score,
                    gate_forced=force,
                )
            )

        # Extract structured requirements from interview
        extraction_result = await self._extract_requirements(state)

        if extraction_result.is_err:
            return Result.err(extraction_result.error)

        applied = apply_requirement_distillation(extraction_result.value, distillation)
        if applied.promotion.blockers:
            return Result.err(
                ValidationError(
                    "Interview must be reopened before Seed generation",
                    field="requirement_distillation",
                    details=seed_readiness_details(applied.promotion),
                )
            )
        requirements = applied.requirements

        # Create metadata
        metadata = SeedMetadata(
            ambiguity_score=ambiguity_score.overall_score,
            gate_forced=force,
            interview_id=state.interview_id,
            parent_seed_id=parent_seed.metadata.seed_id if parent_seed else None,
        )

        # Build the seed
        try:
            seed = self._build_seed(requirements, metadata)

            log.info(
                "seed.generation.completed",
                interview_id=state.interview_id,
                seed_id=seed.metadata.seed_id,
                goal_length=len(seed.goal),
                constraint_count=len(seed.constraints),
                criteria_count=len(seed.acceptance_criteria),
            )

            return Result.ok(seed)

        except Exception as e:
            log.exception(
                "seed.generation.build_failed",
                interview_id=state.interview_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to build seed: {e}",
                    details={"interview_id": state.interview_id},
                )
            )

    def generate_from_reflect(
        self,
        parent_seed: Seed,
        reflect_output: Any,
    ) -> Result[Seed, ValidationError | ProviderError]:
        """Generate a successor Seed while preserving structured AC authority.

        Reflect applies ontology and identity-bearing prose deltas without
        Gen-1 ambiguity gating. Mechanical AC contracts cross only explicit
        keeps; description changes require a future replacement-contract patch.
        """
        log.info(
            "seed.generation.from_reflect",
            parent_seed_id=parent_seed.metadata.seed_id,
            mutation_count=len(reflect_output.ontology_mutations),
        )

        try:
            # Apply ontology mutations to parent's schema
            new_ontology = self._apply_mutations(
                parent_seed.ontology_schema,
                reflect_output.ontology_mutations,
            )

            metadata = SeedMetadata(
                ambiguity_score=parent_seed.metadata.ambiguity_score,
                interview_id=parent_seed.metadata.interview_id,
                parent_seed_id=parent_seed.metadata.seed_id,
            )

            refined_patches = (
                reflect_output.ac_patches if reflect_output.ac_patch_identity_explicit else None
            )
            seed = Seed(
                goal=reflect_output.refined_goal,
                task_type=parent_seed.task_type,
                brownfield_context=parent_seed.brownfield_context,
                constraints=reflect_output.refined_constraints,
                ontology_schema=new_ontology,
                evaluation_principles=parent_seed.evaluation_principles,
                exit_conditions=parent_seed.exit_conditions,
                metadata=metadata,
                **evolve_seed_contract_fields(
                    parent_seed, reflect_output.refined_acs, refined_patches
                ),
            )

            log.info(
                "seed.generation.from_reflect.completed",
                seed_id=seed.metadata.seed_id,
                parent_seed_id=parent_seed.metadata.seed_id,
                field_count=len(new_ontology.fields),
            )

            return Result.ok(seed)

        except Exception as e:
            log.exception(
                "seed.generation.from_reflect.failed",
                parent_seed_id=parent_seed.metadata.seed_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to generate seed from reflect: {e}",
                    details={"parent_seed_id": parent_seed.metadata.seed_id},
                )
            )

    def _apply_mutations(
        self,
        schema: OntologySchema,
        mutations: tuple,
    ) -> OntologySchema:
        """Apply ontology mutations to produce a new schema.

        Args:
            schema: The parent ontology schema.
            mutations: Tuple of OntologyMutation instances.

        Returns:
            New OntologySchema with mutations applied.
        """
        fields_by_name = {f.name: f for f in schema.fields}

        for mutation in mutations:
            action = str(mutation.action)
            if action == "add" and mutation.field_name not in fields_by_name:
                fields_by_name[mutation.field_name] = OntologyField(
                    name=mutation.field_name,
                    field_type=mutation.field_type or "string",
                    description=mutation.description or mutation.reason,
                )
            elif action == "modify" and mutation.field_name in fields_by_name:
                old = fields_by_name[mutation.field_name]
                fields_by_name[mutation.field_name] = OntologyField(
                    name=mutation.field_name,
                    field_type=mutation.field_type or old.field_type,
                    description=mutation.description or old.description,
                    required=old.required,
                )
            elif action == "remove" and mutation.field_name in fields_by_name:
                del fields_by_name[mutation.field_name]

        return OntologySchema(
            name=schema.name,
            description=schema.description,
            fields=tuple(fields_by_name.values()),
        )

    async def _extract_requirements(
        self, state: InterviewState
    ) -> Result[dict[str, Any], ProviderError]:
        """Extract structured requirements from interview using LLM.

        Retries once with a clarified prompt on parse failure.

        Args:
            state: The interview state.

        Returns:
            Result containing extracted requirements dict or error.
        """
        context = self._build_interview_context(state)
        is_brownfield = (
            state.is_brownfield
            or bool(state.codebase_context.strip())
            or bool(state.codebase_paths)
        )
        system_prompt = self._build_extraction_system_prompt()
        user_prompt = self._build_extraction_user_prompt(context, is_brownfield=is_brownfield)

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        assert self.model is not None
        config = CompletionConfig(
            model=self.model,
            role="seed_generation",
            model_is_explicit=self.model_is_explicit,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        last_error = ""
        last_response = ""

        for attempt in range(_MAX_EXTRACTION_RETRIES + 1):
            result = await self.llm_adapter.complete(messages, config)

            if result.is_err:
                log.warning(
                    "seed.extraction.failed",
                    interview_id=state.interview_id,
                    error=str(result.error),
                    attempt=attempt + 1,
                )
                return Result.err(result.error)

            last_response = result.value.content

            try:
                requirements = self._parse_extraction_response(last_response)
                if attempt > 0:
                    log.info(
                        "seed.extraction.retry_succeeded",
                        interview_id=state.interview_id,
                        attempt=attempt + 1,
                    )
                return Result.ok(requirements)
            except (ValueError, KeyError) as e:
                last_error = str(e)
                log.warning(
                    "seed.extraction.parse_failed",
                    interview_id=state.interview_id,
                    error=last_error,
                    response=last_response[:500],
                    attempt=attempt + 1,
                )

                if attempt < _MAX_EXTRACTION_RETRIES:
                    # Retry with clarified prompt
                    messages = [
                        Message(role=MessageRole.SYSTEM, content=system_prompt),
                        Message(
                            role=MessageRole.USER,
                            content=self._build_retry_prompt(
                                context,
                                last_response,
                                last_error,
                                is_brownfield=is_brownfield,
                            ),
                        ),
                    ]

        return Result.err(
            ProviderError(
                f"Failed to parse extraction response after "
                f"{_MAX_EXTRACTION_RETRIES + 1} attempts: {last_error}",
                details={"response_preview": last_response[:200]},
            )
        )

    def _build_retry_prompt(
        self,
        context: str,
        failed_response: str,
        error: str,
        *,
        is_brownfield: bool = False,
    ) -> str:
        """Build a retry prompt after extraction parse failure.

        Args:
            context: Original interview context.
            failed_response: The response that failed to parse.
            error: The parse error message.
            is_brownfield: Whether the interview targets an existing codebase.

        Returns:
            Retry prompt string.
        """
        return f"""Your previous response could not be parsed. Error: {error}

Your response was:
---
{failed_response[:1000]}
---

Please try again. Extract requirements from this interview:
---
{context}
---

You MUST respond with ONLY the following format, one field per line, no other text:

ACCEPTANCE_CRITERIA rule: an acceptance criterion names a state of the finished work that a user can see is true; an implementation step names a means of reaching that state. Only the first belongs here — deciding means is the execution engine's work at runtime. A criterion intelligible only as a move toward a sibling is that sibling's means and belongs merged into the outcome it serves. How many criteria a goal has is discovered by that judgment.
ACCEPTANCE_CRITERIA JSON rule: emit exactly one `ACCEPTANCE_CRITERIA:` field containing a non-empty single-line JSON array. Every object must contain `description`, `verify`, `artifacts`, and `expect`, plus `exempt` only when `verify` is NONE (see the exempt rule); no other keys, no aliases.
ACCEPTANCE_CRITERIA verify rule: `verify` must be one complete single-line shell command. Never use heredoc or multiline syntax (`<<`, `<<'PY'`, `cat <<EOF`, line-continuation scripts); use `python -c "..."`, `python3 -c "..."`, or `python -m pytest -q` instead.
ACCEPTANCE_CRITERIA artifacts rule: `artifacts` must be a JSON array of exact portable file or directory paths relative to the run workspace, or the string `NONE`. The runner resolves every path literally and requires it to exist. Prefix a top-level path containing spaces with `./`. Never use a descriptive label.
ACCEPTANCE_CRITERIA expect rule: `expect` is ONLY a literal string printed verbatim in the combined stdout and stderr of `verify`, such as `OK` or `5 passed`. Use `expect: NONE` for exit-code/status conditions like `exit code 0`, `success`, `passed`, or `no errors`; exit-code 0 is already verified separately.
ACCEPTANCE_CRITERIA exempt rule: prefer a real `verify` command for every criterion — a criterion without one can only be judged from the worker's own account of its work. Use `verify: NONE` only when no command could decide the outcome, and then add `"exempt": "<why no command can decide this>"`. Omit `exempt` (or set it to NONE) whenever `verify` is a command.

CONSTRAINTS rule: respond with one single-line JSON array of strings, e.g. ["<constraint 1>", "<constraint 2>"]. Constraint values may contain any characters, including literal | pipes; never use a bare pipe as the list separator.

GOAL: <clear goal statement>
CONSTRAINTS: ["<constraint 1>", "<constraint 2>", ...]
ACCEPTANCE_CRITERIA: [{{"description": "<observable outcome>", "verify": "<single-line command or NONE>", "artifacts": ["<path>"], "expect": "<literal output or NONE>", "exempt": "<why no command can decide this, only when verify is NONE>"}}]
ONTOLOGY_NAME: <name>
ONTOLOGY_DESCRIPTION: <description>
ONTOLOGY_FIELDS: [{{"name": "<name>", "type": "<string|number|boolean|array|object>", "description": "<description>"}}, ...]
EVALUATION_PRINCIPLES: [{{"name": "<name>", "description": "<description>", "weight": <0.0-1.0>}}, ...]
EXIT_CONDITIONS: [{{"name": "<name>", "description": "<description>", "criteria": "<criteria>"}}, ...]
{self._project_type_template(is_brownfield=is_brownfield)}"""

    @staticmethod
    def _project_type_template(*, is_brownfield: bool) -> str:
        """Return the PROJECT_TYPE trailer for the extraction format.

        Greenfield interviews keep today's single ``PROJECT_TYPE: greenfield``
        line. Brownfield interviews declare the type and request the three
        brownfield keys the parser already recognizes so the resulting Seed
        carries a populated ``brownfield_context``.
        """
        if not is_brownfield:
            return "PROJECT_TYPE: greenfield"
        return (
            "PROJECT_TYPE: brownfield\n"
            'CONTEXT_REFERENCES: [{{"path": "<path>", "role": "<primary|reference>", "summary": "<summary>"}}, ...]\n'
            'EXISTING_PATTERNS: ["<pattern 1>", "<pattern 2>", ...]\n'
            'EXISTING_DEPENDENCIES: ["<dependency 1>", "<dependency 2>", ...]\n'
            "CONTEXT_REFERENCES rule: respond with one single-line JSON array of objects. "
            "Path, role, and summary values may contain any characters, including literal "
            ": colons and | pipes; never use a bare pipe as the list separator.\n"
            "EXISTING_PATTERNS rule: respond with one single-line JSON array of strings. "
            "Pattern values may contain any characters, including literal | pipes; "
            "never use a bare pipe as the list separator.\n"
            "EXISTING_DEPENDENCIES rule: respond with one single-line JSON array of strings. "
            "Dependency values may contain any characters, including literal | pipes; "
            "never use a bare pipe as the list separator."
        )

    def _build_interview_context(self, state: InterviewState) -> str:
        """Build context string from interview state.

        Args:
            state: The interview state.

        Returns:
            Formatted context string.
        """
        parts = [f"Initial Context: {prompt_safe_initial_context(state)}"]

        # Brownfield priming: carry the auto-explore codebase summary and the
        # referenced paths into the extraction context so the seed architect
        # can populate CONTEXT_REFERENCES / EXISTING_PATTERNS / EXISTING_DEPENDENCIES
        # instead of defaulting the seed to greenfield.
        if state.codebase_context.strip():
            parts.append(f"\nCodebase Context:\n{state.codebase_context.strip()}")
        if state.codebase_paths:
            rendered_paths = "; ".join(
                f"{entry.get('path', '')} ({entry.get('role', 'reference')})"
                for entry in state.codebase_paths
                if entry.get("path")
            )
            if rendered_paths:
                parts.append(f"\nCodebase Paths: {rendered_paths}")

        # Observation answers render as a fixed note instead of their content
        # (#1755). Question lines are unchanged: an observation reaching a later
        # question is where it was collected to arrive.
        for round_data in extraction_rounds(state):
            if round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION:
                continue
            parts.append(f"\nQ: {round_data.question}")
            if round_data.answer:
                parts.append(f"A: {round_data.answer}")

        return "\n".join(parts)

    def _build_extraction_system_prompt(self) -> str:
        """Build system prompt for requirement extraction.

        Returns:
            System prompt string.
        """
        from ouroboros.agents.loader import load_agent_prompt

        return load_agent_prompt("seed-architect")

    def _build_extraction_user_prompt(self, context: str, *, is_brownfield: bool = False) -> str:
        """Build user prompt with interview context.

        Args:
            context: Formatted interview context.
            is_brownfield: Whether the interview targets an existing codebase.
                When True, the format requests brownfield keys the parser
                already understands; greenfield keeps today's template output.

        Returns:
            User prompt string.
        """
        return f"""Extract structured requirements from the following interview conversation.

---
{context}
---

Respond ONLY with the structured format below. Do NOT add explanations, questions, commentary, or prose. Do NOT wrap in markdown code blocks.

ACCEPTANCE_CRITERIA rule: an acceptance criterion names a state of the finished work that a user can see is true; an implementation step names a means of reaching that state. Only the first belongs here — deciding means is the execution engine's work at runtime. Read each criterion beside its siblings and ask which kind it is: one that stands on its own as something a user would value is an outcome, while one intelligible only as a move toward a sibling is that sibling's means and belongs merged into the outcome it serves. Leaving a means in the list is a defect as severe as a missing requirement, because it commits the seed to a path no one has verified. How many criteria a goal has is discovered by making this judgment.
ACCEPTANCE_CRITERIA JSON rule: emit exactly one `ACCEPTANCE_CRITERIA:` field containing a non-empty single-line JSON array. Every object must contain `description`, `verify`, `artifacts`, and `expect`, plus `exempt` only when `verify` is NONE (see the exempt rule); no other keys, no aliases.
ACCEPTANCE_CRITERIA verify rule: `verify` must be one complete single-line shell command. Never use heredoc or multiline syntax (`<<`, `<<'PY'`, `cat <<EOF`, line-continuation scripts); use `python -c "..."`, `python3 -c "..."`, or `python -m pytest -q` instead.
ACCEPTANCE_CRITERIA artifacts rule: `artifacts` must be a JSON array of exact portable file or directory paths relative to the run workspace, or the string `NONE`. The runner resolves every path literally and requires it to exist. Prefix a top-level path containing spaces with `./`. Never use a descriptive label.
ACCEPTANCE_CRITERIA expect rule: `expect` is ONLY a literal string printed verbatim in the combined stdout and stderr of `verify`, such as `OK` or `5 passed`. Use `expect: NONE` for exit-code/status conditions like `exit code 0`, `success`, `passed`, or `no errors`; exit-code 0 is already verified separately.
ACCEPTANCE_CRITERIA exempt rule: prefer a real `verify` command for every criterion — a criterion without one can only be judged from the worker's own account of its work. Use `verify: NONE` only when no command could decide the outcome, and then add `"exempt": "<why no command can decide this>"`. Omit `exempt` (or set it to NONE) whenever `verify` is a command.

CONSTRAINTS rule: respond with one single-line JSON array of strings, e.g. ["<constraint 1>", "<constraint 2>"]. Constraint values may contain any characters, including literal | pipes; never use a bare pipe as the list separator.

GOAL: <clear goal statement>
CONSTRAINTS: ["<constraint 1>", "<constraint 2>", ...]
ACCEPTANCE_CRITERIA: [{{"description": "<observable outcome>", "verify": "<single-line command or NONE>", "artifacts": ["<path>"], "expect": "<literal output or NONE>", "exempt": "<why no command can decide this, only when verify is NONE>"}}]
ONTOLOGY_NAME: <name>
ONTOLOGY_DESCRIPTION: <description>
ONTOLOGY_FIELDS: [{{"name": "<name>", "type": "<string|number|boolean|array|object>", "description": "<description>"}}, ...]
EVALUATION_PRINCIPLES: [{{"name": "<name>", "description": "<description>", "weight": <0.0-1.0>}}, ...]
EXIT_CONDITIONS: [{{"name": "<name>", "description": "<description>", "criteria": "<criteria>"}}, ...]
{self._project_type_template(is_brownfield=is_brownfield)}"""

    _KNOWN_PREFIXES = (
        "GOAL:",
        "CONSTRAINTS:",
        "ACCEPTANCE_CRITERIA:",
        "ONTOLOGY_NAME:",
        "ONTOLOGY_DESCRIPTION:",
        "ONTOLOGY_FIELDS:",
        "EVALUATION_PRINCIPLES:",
        "EXIT_CONDITIONS:",
        "PROJECT_TYPE:",
        "CONTEXT_REFERENCES:",
        "EXISTING_PATTERNS:",
        "EXISTING_DEPENDENCIES:",
    )

    def _preprocess_response(self, response: str) -> str:
        """Strip markdown code blocks and conversational preamble.

        Args:
            response: Raw LLM response text.

        Returns:
            Cleaned response starting from first recognized prefix.
        """
        text = response.strip()

        # Strip markdown code block markers
        code_block_match = re.search(r"```(?:\w*)\n(.*?)```", text, re.DOTALL)
        if code_block_match:
            text = code_block_match.group(1).strip()

        # Find first recognized prefix and discard preamble
        lines = text.split("\n")
        start_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if any(stripped.startswith(p) for p in self._KNOWN_PREFIXES):
                start_idx = i
                break

        return "\n".join(lines[start_idx:])

    def _parse_extraction_response(self, response: str) -> dict[str, Any]:
        """Parse LLM response into requirements dictionary.

        Args:
            response: Raw LLM response text.

        Returns:
            Parsed requirements dictionary.

        Raises:
            ValueError: If response cannot be parsed.
        """
        cleaned = self._preprocess_response(response)
        lines = cleaned.strip().split("\n")
        requirements: dict[str, Any] = {}
        acceptance_criteria_count = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            matched_prefix = False
            for prefix in self._KNOWN_PREFIXES:
                if line.startswith(prefix):
                    key = prefix[:-1].lower()  # Remove colon and lowercase
                    value = line[len(prefix) :].strip()
                    if key == "acceptance_criteria":
                        acceptance_criteria_count += 1
                        if acceptance_criteria_count > 1:
                            raise ValueError("Duplicate top-level ACCEPTANCE_CRITERIA field")
                    requirements[key] = value
                    matched_prefix = True
                    break
            if matched_prefix:
                continue
            raise ValueError(f"Unrecognized extraction line after structured output began: {line}")

        if "acceptance_criteria" not in requirements:
            raise ValueError("Missing required field: acceptance_criteria")
        requirements["acceptance_criteria"] = _parse_extracted_acceptance_criteria(
            requirements["acceptance_criteria"]
        )

        # Validate required fields
        required_fields = [
            "goal",
            "ontology_name",
            "ontology_description",
        ]

        for field_name in required_fields:
            if field_name not in requirements:
                raise ValueError(
                    f"Missing required field: {field_name}. "
                    f"Found: {list(requirements.keys())}. "
                    f"Response preview: {response[:200]}"
                )

        # Validate list-valued fields at parse time so malformed JSON-intent
        # output triggers the extraction retry path instead of being silently
        # pipe-split downstream (#1696, #1729).
        for field_name, field_label in (
            ("constraints", "CONSTRAINTS"),
            ("existing_patterns", "EXISTING_PATTERNS"),
            ("existing_dependencies", "EXISTING_DEPENDENCIES"),
        ):
            if field_name in requirements:
                requirements[field_name] = _parse_string_array_values(
                    requirements[field_name], field_label=field_label, strict=True
                )
        if "evaluation_principles" in requirements:
            requirements["evaluation_principles"] = _parse_evaluation_principles(
                requirements["evaluation_principles"], strict=True
            )
        if "exit_conditions" in requirements:
            requirements["exit_conditions"] = _parse_exit_conditions(
                requirements["exit_conditions"], strict=True
            )
        if "ontology_fields" in requirements:
            requirements["ontology_fields"] = _parse_ontology_fields(
                requirements["ontology_fields"], strict=True
            )
        if "context_references" in requirements:
            requirements["context_references"] = _parse_context_references(
                requirements["context_references"], strict=True
            )

        return requirements

    def _build_seed(self, requirements: dict[str, Any], metadata: SeedMetadata) -> Seed:
        """Build Seed from extracted requirements.

        Args:
            requirements: Extracted requirements dictionary.
            metadata: Seed metadata.

        Returns:
            Constructed Seed instance.
        """
        # Parse constraints (JSON array preferred; legacy pipe list supported)
        constraints = _parse_string_array_values(
            requirements.get("constraints"), field_label="CONSTRAINTS"
        )

        # Parse acceptance criteria
        acceptance_criteria: tuple[AcceptanceCriterionSpec | str, ...] = ()
        if "acceptance_criteria" in requirements and requirements["acceptance_criteria"]:
            acceptance_criteria = _parse_acceptance_criteria_contracts(
                requirements["acceptance_criteria"]
            )

        # Parse ontology fields (JSON object arrays preferred; legacy
        # colon/pipe lists supported for stored requirements)
        ontology_fields = _parse_ontology_fields(requirements.get("ontology_fields"))

        # Build ontology schema
        ontology_schema = OntologySchema(
            name=requirements["ontology_name"],
            description=requirements["ontology_description"],
            fields=tuple(ontology_fields),
        )

        # Parse evaluation principles and exit conditions (JSON object arrays
        # preferred; legacy colon/pipe lists supported for stored requirements)
        evaluation_principles = _parse_evaluation_principles(
            requirements.get("evaluation_principles")
        )
        exit_conditions = _parse_exit_conditions(requirements.get("exit_conditions"))

        # Parse brownfield context
        brownfield_context = BrownfieldContext()
        project_type = requirements.get("project_type", "greenfield").strip().lower()
        if project_type == "brownfield":
            # Parse context references (JSON object arrays preferred; legacy
            # colon/pipe lists supported for stored requirements)
            context_refs = _parse_context_references(requirements.get("context_references"))

            # Parse existing patterns (lenient: stored data has no retry path)
            existing_patterns = _parse_string_array_values(
                requirements.get("existing_patterns"), field_label="EXISTING_PATTERNS"
            )

            # Parse existing dependencies (lenient: stored data has no retry path)
            existing_deps = _parse_string_array_values(
                requirements.get("existing_dependencies"), field_label="EXISTING_DEPENDENCIES"
            )

            brownfield_context = BrownfieldContext(
                project_type="brownfield",
                context_references=tuple(context_refs),
                existing_patterns=existing_patterns,
                existing_dependencies=existing_deps,
            )

        return Seed(
            goal=requirements["goal"],
            brownfield_context=brownfield_context,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            ontology_schema=ontology_schema,
            evaluation_principles=tuple(evaluation_principles),
            exit_conditions=tuple(exit_conditions),
            metadata=metadata,
        )

    async def save_seed(
        self,
        seed: Seed,
        file_path: Path | None = None,
    ) -> Result[Path, ValidationError]:
        """Save seed to YAML file.

        Args:
            seed: The seed to save.
            file_path: Optional path for the seed file.
                If not provided, uses output_dir/seed_{id}.yaml

        Returns:
            Result containing the file path or error.
        """
        if file_path is None:
            file_path = self.output_dir / f"{seed.metadata.seed_id}.yaml"

        log.info(
            "seed.saving",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
        )

        try:
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Convert to dict for YAML serialization
            seed_dict = seed.to_dict()

            # Write YAML with proper formatting
            content = yaml.dump(
                seed_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

            if not write_owner_only(file_path, content):
                # Reported like every other artifact writer: the file exists
                # but the directory flush was unconfirmed.
                log.warning(
                    "seed.save_durability_uncertain",
                    seed_id=seed.metadata.seed_id,
                    file_path=str(file_path),
                )

            log.info(
                "seed.saved",
                seed_id=seed.metadata.seed_id,
                file_path=str(file_path),
            )

            return Result.ok(file_path)

        except (OSError, yaml.YAMLError) as e:
            log.exception(
                "seed.save_failed",
                seed_id=seed.metadata.seed_id,
                file_path=str(file_path),
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to save seed: {e}",
                    details={
                        "seed_id": seed.metadata.seed_id,
                        "file_path": str(file_path),
                    },
                )
            )


async def load_seed(file_path: Path) -> Result[Seed, ValidationError]:
    """Load seed from YAML file.

    Legacy prose acceptance criteria remain supported by ``Seed.from_dict``.
    Persisted structured contracts are revalidated against current portable
    execution constraints and fail explicitly here when they can no longer be
    materialized; resume must never admit an unsatisfiable verify gate.

    Args:
        file_path: Path to the seed YAML file.

    Returns:
        Result containing the loaded Seed or error.
    """
    if not file_path.exists():
        return Result.err(
            ValidationError(
                f"Seed file not found: {file_path}",
                field="file_path",
                value=str(file_path),
            )
        )

    try:
        content = file_path.read_text(encoding="utf-8")
        seed_dict = yaml.safe_load(content)

        # Validate and create Seed
        seed = Seed.from_dict(seed_dict)

        log.info(
            "seed.loaded",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
        )

        return Result.ok(seed)

    except (OSError, yaml.YAMLError, ValueError) as e:
        log.exception(
            "seed.load_failed",
            file_path=str(file_path),
            error=str(e),
        )
        return Result.err(
            ValidationError(
                f"Failed to load seed: {e}",
                field="file_path",
                value=str(file_path),
                details={"error": str(e)},
            )
        )


def save_seed_sync(seed: Seed, file_path: Path) -> Result[Path, ValidationError]:
    """Synchronous version of save_seed for convenience.

    Args:
        seed: The seed to save.
        file_path: Path for the seed file.

    Returns:
        Result containing the file path or error.
    """
    log.info(
        "seed.saving.sync",
        seed_id=seed.metadata.seed_id,
        file_path=str(file_path),
    )

    try:
        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict for YAML serialization
        seed_dict = seed.to_dict()

        # Write YAML with proper formatting
        content = yaml.dump(
            seed_dict,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

        if not write_owner_only(file_path, content):
            log.warning(
                "seed.save_durability_uncertain",
                seed_id=seed.metadata.seed_id,
                file_path=str(file_path),
            )

        log.info(
            "seed.saved.sync",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
        )

        return Result.ok(file_path)

    except (OSError, yaml.YAMLError) as e:
        log.exception(
            "seed.save_failed.sync",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
            error=str(e),
        )
        return Result.err(
            ValidationError(
                f"Failed to save seed: {e}",
                details={
                    "seed_id": seed.metadata.seed_id,
                    "file_path": str(file_path),
                },
            )
        )
