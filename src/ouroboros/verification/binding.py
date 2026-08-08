"""Deterministic binding between verifier evidence and an acceptance criterion."""

from __future__ import annotations

import re

_TARGET_TOKEN = re.compile(r"--[\w][\w-]*|[\w][\w./:-]*", re.UNICODE)
_QUOTED_TOKEN = re.compile(
    r"(?P<quote>[`'\"])(?P<token>--[\w][\w-]*|[\w][\w./:-]*)(?P=quote)",
    re.UNICODE,
)
_STRUCTURAL_WORDS = frozenset(
    {"class", "constant", "directory", "file", "flag", "function", "interface", "struct", "trait"}
)
_CONSTANT_BINDING_SUFFIX = re.compile(
    r"(?:=|:|\bto\b|\bof\b|\bis\b|\bshould\s+be\b|\bmust\s+be\b)\s*$",
    re.IGNORECASE,
)
_TOKEN_PUNCTUATION = frozenset("._/:-+@#?&=%~")


def _is_identifier_continue(character: str) -> bool:
    r"""Return whether a character can continue a Unicode identifier.

    Python's regex ``\w`` omits valid XID continuations such as combining
    marks, ZWNJ, and the middle dot. Prefixing a known identifier starter asks
    Python's Unicode identifier table directly and keeps literal boundaries
    conservative for every such continuation.
    """
    return bool(character) and (
        character in {"$", "\u200c", "\u200d"} or ("A" + character).isidentifier()
    )


def literal_spans(text: str, literal: str) -> tuple[tuple[int, int], ...]:
    """Return exact-case whole-literal spans with Unicode XID boundaries."""
    literal = literal.strip()
    if not literal:
        return ()
    spans: list[tuple[int, int]] = []
    is_flag = literal.startswith("--")
    boundary_punctuation = (
        _TOKEN_PUNCTUATION
        if any(character in _TOKEN_PUNCTUATION for character in literal)
        else frozenset(".")
        if literal.isdecimal()
        else frozenset()
    )
    for match in re.finditer(re.escape(literal), text):
        if match.start() > 0:
            previous = text[match.start() - 1]
            if (
                (literal[0].isalnum() or literal[0] == "_" or is_flag)
                and _is_identifier_continue(previous)
            ) or previous in boundary_punctuation:
                continue
        if match.end() < len(text):
            following = text[match.end()]
            if (
                (literal[-1].isalnum() or literal[-1] == "_") and _is_identifier_continue(following)
            ) or following in boundary_punctuation:
                continue
        spans.append((match.start(), match.end()))
    return tuple(spans)


def literal_is_bound(text: str, literal: str) -> bool:
    """Whether ``literal`` is present as a complete value in trusted text."""
    return bool(literal_spans(text, literal))


def _looks_code_like(token: str) -> bool:
    """Recognize conservative code literals without interpreting prose nouns."""
    if token.startswith("--"):
        return True
    if any(separator in token for separator in ("_", "/", ".", ":", "-")):
        return True
    if token.isascii() and token.isupper() and token.isalpha() and len(token) <= 3:
        return True
    return bool(re.search(r"[a-z][A-Z]", token))


def _looks_constant_literal(token: str) -> bool:
    """Recognize exact constant keys without admitting arbitrary prose."""
    return _looks_code_like(token) or (token.isascii() and token.isupper() and token.isalpha())


def _token_candidates(ac_text: str) -> tuple[tuple[str, int, int, bool], ...]:
    """Return unquoted and explicitly quoted literals with source spans."""
    quoted = tuple(
        (match.group("token"), match.start(), match.end(), True)
        for match in _QUOTED_TOKEN.finditer(ac_text)
    )
    quoted_inner_spans = tuple(
        (match.start("token"), match.end("token")) for match in _QUOTED_TOKEN.finditer(ac_text)
    )
    plain = tuple(
        (match.group(0), match.start(), match.end(), False)
        for match in _TARGET_TOKEN.finditer(ac_text)
        if not any(
            start <= match.start() and match.end() <= end for start, end in quoted_inner_spans
        )
    )
    return tuple(sorted((*quoted, *plain), key=lambda candidate: candidate[1]))


def _structural_targets(ac_text: str) -> tuple[str, ...]:
    """Derive structural names under conservative, input-only grammar."""
    candidates = _token_candidates(ac_text)
    words = [candidate for candidate in candidates if not candidate[3]]
    selected: list[str] = []

    for word, word_start, word_end, _quoted in words:
        folded = word.casefold()
        if folded not in _STRUCTURAL_WORDS:
            continue
        previous = next((item for item in reversed(words) if item[2] <= word_start), None)
        if previous is not None and not ac_text[previous[2] : word_start].strip():
            token = previous[0]
            if _looks_code_like(token):
                selected.append(token)
                continue
        following = next((item for item in words if item[1] >= word_end), None)
        if following is not None and not ac_text[word_end : following[1]].strip():
            token = following[0]
            if _looks_code_like(token) or (
                token.isascii() and token[:1].isupper() and token[1:].islower()
            ):
                selected.append(token)

    if selected:
        return tuple(dict.fromkeys(selected))

    explicit = [token for token, _start, _end, quoted in candidates if quoted]
    code_literals = [
        token for token, _start, _end, quoted in candidates if quoted or _looks_code_like(token)
    ]
    unique = tuple(dict.fromkeys(explicit or code_literals))
    return unique if len(unique) == 1 else ()


def _constant_target(ac_text: str, expected: str) -> tuple[str, ...]:
    """Bind a scalar only to an explicit code literal in its own clause."""
    value_spans = literal_spans(ac_text, expected)
    if not value_spans:
        return ()
    candidates = [
        candidate
        for candidate in _token_candidates(ac_text)
        if candidate[3] or _looks_constant_literal(candidate[0])
    ]
    for value_start, _value_end in value_spans:
        for token, _start, end, _quoted in reversed(candidates):
            if end > value_start:
                continue
            if _CONSTANT_BINDING_SUFFIX.fullmatch(ac_text[end:value_start].strip()):
                return (token,)
    return ()


def acceptance_targets(
    ac_text: str,
    expected_value: str = "",
    *,
    prefer_expected: bool = False,
) -> tuple[str, ...]:
    """Return exact code literals derived only from caller-authored criterion text.

    Structural targets are selected independently of model ``expected_value``;
    the extractor subsequently requires the model value to equal one of them.
    Constant targets require an explicit code-shaped literal bound to the
    expected scalar's clause. Ambiguous prose has no target and fails closed.
    """
    expected = expected_value.strip()
    if expected and not literal_is_bound(ac_text, expected):
        return ()
    if prefer_expected:
        return _structural_targets(ac_text)
    if expected:
        return _constant_target(ac_text, expected)
    return ()
