"""Shared redaction for harness-owned verifier values."""

from __future__ import annotations

from collections.abc import Iterable
import html
import json
import re
import shlex
import unicodedata


def hidden_contract_variants(values: Iterable[str | None]) -> tuple[str, ...]:
    """Return longest-first raw, quoted, and escaped hidden values."""

    variants: set[str] = set()
    for hidden in values:
        if not hidden:
            continue
        json_quoted = json.dumps(hidden, ensure_ascii=False)
        variants.update(
            {
                hidden,
                repr(hidden),
                shlex.quote(hidden),
                json_quoted,
                json_quoted[1:-1],
            }
        )
    return tuple(
        sorted((value for value in variants if value), key=lambda value: (-len(value), value))
    )


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;:]*m")
_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESCAPED_TERMINAL_CONTROL_RE = re.compile(
    r"(?ix)(?:"
    r"\\(?:x(?:1b|08|9[08bef])|u(?:001b|0008|009[08bef])|U(?:0000001b|00000008|0000009[08bef])|0(?:33|10)|e|b)"
    r"|\\?\^\["
    r")"
)
_ESCAPED_WHITESPACE_RE = re.compile(
    r"(?ix)\\(?:n|r|t|x0[9ad]|u000[9ad]|U0000000[9ad]|0(?:11|12|15))"
)
_ESCAPED_UNICODE_RE = re.compile(
    r"\\(?:x(?P<byte>[0-9a-fA-F]{2})|u(?P<short>[0-9a-fA-F]{4})|U(?P<long>[0-9a-fA-F]{8}))"
)
_ESCAPED_SURROGATE_PAIR_RE = re.compile(
    r"\\u(?P<high>[dD][89aAbB][0-9a-fA-F]{2})\\u(?P<low>[dD][c-fC-F][0-9a-fA-F]{2})"
)
_NUMERIC_ENTITY_RE = re.compile(r"&#(?:(?P<decimal>\d+)|[xX](?P<hex>[0-9a-fA-F]+));?")
_ESCAPED_BYTE_RUN_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")
_ESCAPED_OCTAL_RUN_RE = re.compile(r"(?:\\[0-7]{1,3})+")
_PERCENT_BYTE_RUN_RE = re.compile(r"(?:%[0-9a-fA-F]{2})+")
_LINE_PREFIX_RE = re.compile(r"(?m)^[ \t]*(?:[EIWF][ \t]+|[+>~-][ \t]?)")
_MALFORMED_ENCODING_RE = re.compile(
    r"(?ix)(?:"
    r"\\x(?![0-9a-f]{2})"
    r"|\\u(?![0-9a-f]{4})"
    r"|\\U(?![0-9a-f]{8})"
    r"|%(?=[0-9a-z]{2})(?=[0-9a-z]?\d)(?![0-9a-f]{2})"
    r"|&\#x(?![0-9a-f]+;?)"
    r"|&\#(?!x?[0-9a-f]+;?)"
    r")"
)
_UNSUPPORTED_TERMINAL_CONTROL_RE = re.compile(
    r"(?:\x1b(?:P|_|\^|X)|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c)",
    re.DOTALL,
)


_MAX_HTML_ENTITY_DECODE_PASSES = 64


def _decode_html_entities(text: str) -> str | None:
    invalid = False

    def decode_numeric(match: re.Match[str]) -> str:
        nonlocal invalid
        encoded = match.group("decimal") or match.group("hex")
        assert encoded is not None
        base = 10 if match.group("decimal") is not None else 16
        try:
            codepoint = int(encoded, base)
            if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                invalid = True
                return ""
            return chr(codepoint)
        except (ValueError, OverflowError):
            invalid = True
            return ""

    decoded_text = text
    for _ in range(_MAX_HTML_ENTITY_DECODE_PASSES):
        numeric_decoded = _NUMERIC_ENTITY_RE.sub(decode_numeric, decoded_text)
        if invalid:
            return None
        decoded = html.unescape(numeric_decoded)
        if decoded == decoded_text:
            return decoded_text
        decoded_text = decoded
    return None


def _decode_escaped_unicode(text: str) -> str | None:
    invalid = False

    def replace_pair(match: re.Match[str]) -> str:
        high = int(match.group("high"), 16)
        low = int(match.group("low"), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid
        encoded = match.group("byte") or match.group("short") or match.group("long")
        assert encoded is not None
        try:
            codepoint = int(encoded, 16)
            if 0xD800 <= codepoint <= 0xDFFF:
                invalid = True
                return ""
            return chr(codepoint)
        except (ValueError, OverflowError):
            invalid = True
            return ""

    decoded = _ESCAPED_SURROGATE_PAIR_RE.sub(replace_pair, text)
    decoded = _ESCAPED_UNICODE_RE.sub(replace, decoded)
    return None if invalid else decoded


def _decode_escaped_byte_runs(text: str) -> str | None:
    invalid = False

    def decode_bytes(raw: bytes) -> str:
        nonlocal invalid
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            invalid = True
            return ""

    def replace_hex(match: re.Match[str]) -> str:
        return decode_bytes(bytes.fromhex(match.group(0).replace("\\x", "")))

    def replace_octal(match: re.Match[str]) -> str:
        nonlocal invalid
        values = [int(value, 8) for value in re.findall(r"\\([0-7]{1,3})", match.group(0))]
        if any(value > 0xFF for value in values):
            invalid = True
            return ""
        return decode_bytes(bytes(values))

    decoded = _ESCAPED_BYTE_RUN_RE.sub(replace_hex, text)
    decoded = _ESCAPED_OCTAL_RUN_RE.sub(replace_octal, decoded)
    return None if invalid else decoded


def _decode_escaped_whitespace(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        if token in {r"\n", r"\x0a", r"\u000a", r"\u0000000a", r"\012"}:
            return "\n"
        if token in {r"\r", r"\x0d", r"\u000d", r"\u0000000d", r"\015"}:
            return "\r"
        return "\t"

    return _ESCAPED_WHITESPACE_RE.sub(replace, text)


def _decode_percent_runs(text: str) -> str | None:
    invalid = False

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid
        raw = bytes.fromhex(match.group(0).replace("%", ""))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            invalid = True
            return ""

    decoded = _PERCENT_BYTE_RUN_RE.sub(replace, text)
    return None if invalid else decoded


def _decode_contract_encodings(text: str) -> str | None:
    if _MALFORMED_ENCODING_RE.search(text):
        return None
    decoded_text = text
    for _ in range(_MAX_HTML_ENTITY_DECODE_PASSES):
        normalized_text = unicodedata.normalize("NFKC", decoded_text)
        html_decoded = _decode_html_entities(normalized_text)
        if html_decoded is None:
            return None
        byte_decoded = _decode_escaped_byte_runs(html_decoded)
        if byte_decoded is None:
            return None
        unicode_decoded = _decode_escaped_unicode(byte_decoded)
        if unicode_decoded is None:
            return None
        percent_decoded = _decode_percent_runs(unicode_decoded)
        if percent_decoded is None:
            return None
        decoded = _decode_escaped_whitespace(percent_decoded)
        if decoded == decoded_text:
            return decoded
        if _MALFORMED_ENCODING_RE.search(decoded):
            return None
        decoded_text = decoded
    return None


def _normalized_contract_text(
    text: str,
    *,
    preserve_punctuation: bool,
    strip_line_prefixes: bool = True,
) -> str | None:
    """Normalize routine verifier-output transformations for leak detection."""
    unescaped = _decode_contract_encodings(text)
    if unescaped is None:
        return None
    unescaped = unicodedata.normalize("NFKC", unescaped)
    without_ansi = _ANSI_ESCAPE_RE.sub("", unescaped)
    without_ansi = _OSC_ESCAPE_RE.sub("", without_ansi)
    without_ansi = "".join(
        char
        for char in without_ansi
        if char in "\n\r\t" or unicodedata.category(char) not in {"Cc", "Cf", "Mn", "Me"}
    )
    without_prefixes = (
        _LINE_PREFIX_RE.sub("", without_ansi) if strip_line_prefixes else without_ansi
    )
    folded = "".join(char.casefold() for char in without_prefixes)
    if preserve_punctuation:
        return "".join(
            char
            for char in folded
            if not char.isspace() and unicodedata.category(char) not in {"Cf", "Mn", "Me"}
        )
    return "".join(char for char in folded if char.isalnum())


def contains_unsupported_terminal_control(text: str) -> bool:
    """Return whether output carries controls outside normalized CSI/OSC."""
    decoded = _decode_contract_encodings(text)
    if decoded is None:
        return True
    if _ESCAPED_TERMINAL_CONTROL_RE.search(decoded):
        return True
    if _OSC_ESCAPE_RE.search(decoded):
        return True
    without_known = _ANSI_ESCAPE_RE.sub("", decoded)
    if _UNSUPPORTED_TERMINAL_CONTROL_RE.search(without_known):
        return True
    return any(
        (unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\n\r\t")
        or 0x80 <= ord(char) <= 0x9F
        for char in without_known
    )


def contains_transformed_hidden_contract_value(
    text: str,
    values: Iterable[str | None],
) -> bool:
    """Return whether a non-exact normalized copy carries a hidden value."""
    for hidden in values:
        if not hidden:
            continue
        remaining = text
        for variant in hidden_contract_variants((hidden,)):
            remaining = remaining.replace(variant, "")
        normalized_hidden = _normalized_contract_text(hidden, preserve_punctuation=False)
        if normalized_hidden is None:
            return True
        for strip_prefixes in (True, False):
            normalized_remaining = _normalized_contract_text(
                remaining,
                preserve_punctuation=False,
                strip_line_prefixes=strip_prefixes,
            )
            if normalized_remaining is None:
                return True
            if normalized_hidden and normalized_hidden in normalized_remaining:
                return True
        if normalized_hidden:
            continue
        compact_hidden = _normalized_contract_text(hidden, preserve_punctuation=True)
        if compact_hidden is None:
            return True
        for strip_prefixes in (True, False):
            compact_remaining = _normalized_contract_text(
                remaining,
                preserve_punctuation=True,
                strip_line_prefixes=strip_prefixes,
            )
            if compact_remaining is None:
                return True
            if compact_hidden and compact_hidden in compact_remaining:
                return True
    return False


def redact_hidden_contract_values(
    text: str,
    values: Iterable[str | None],
    *,
    replacement: str = "[REDACTED CONTRACT VALUE]",
) -> str:
    """Remove every supported encoding of harness-owned values from text."""

    redacted = text
    for hidden in hidden_contract_variants(values):
        redacted = redacted.replace(hidden, replacement)
    return redacted
