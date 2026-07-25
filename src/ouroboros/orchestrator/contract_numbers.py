"""Canonical JSON-safe encoding for unbounded contract integers."""

from __future__ import annotations

# CPython rejects decimal conversion above 4,300 digits by default. Keep a
# margin for interpreter differences and encode larger public-config values as
# canonical decimal strings without changing their in-memory integer domain.
_MAX_DIRECT_JSON_DECIMAL_DIGITS = 4_000
_DECIMAL_CHUNK_BASE = 1_000_000_000
_DECIMAL_CHUNK_WIDTH = 9


def _decimal_digits(value: int) -> str:
    """Render a non-negative int without relying on CPython's digit limit."""

    if value == 0:
        return "0"
    chunks: list[int] = []
    while value:
        value, remainder = divmod(value, _DECIMAL_CHUNK_BASE)
        chunks.append(remainder)
    return str(chunks[-1]) + "".join(
        f"{chunk:0{_DECIMAL_CHUNK_WIDTH}d}" for chunk in reversed(chunks[:-1])
    )


def json_safe_nonnegative_int(value: int) -> int | str:
    """Return an int when JSON-safe, otherwise a canonical decimal string."""

    if type(value) is not int or value < 0:
        raise ValueError("contract integer must be a non-negative integer")
    # log10(2) < 0.302; this conservative bit bound avoids converting a value
    # that could cross the interpreter's decimal digit guard.
    if value.bit_length() <= _MAX_DIRECT_JSON_DECIMAL_DIGITS * 3:
        return value
    return _decimal_digits(value)


def parse_nonnegative_contract_int(value: object) -> int | None:
    """Parse the numeric or canonical-string form without a decimal limit."""

    if type(value) is int:
        return value if value >= 0 else None
    if not isinstance(value, str) or not value or (len(value) > 1 and value[0] == "0"):
        return None
    if any(character < "0" or character > "9" for character in value):
        return None
    result = 0
    for start in range(0, len(value), _DECIMAL_CHUNK_WIDTH):
        chunk = value[start : start + _DECIMAL_CHUNK_WIDTH]
        result = result * (10 ** len(chunk)) + int(chunk)
    return result


def parse_positive_contract_int(value: object) -> int | None:
    """Parse a strictly positive contract integer."""

    parsed = parse_nonnegative_contract_int(value)
    return parsed if parsed is not None and parsed >= 1 else None
