"""Shared guards applied to raw LLM responses before an adapter returns them."""

from __future__ import annotations

import structlog

from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator

log = structlog.get_logger()


def truncate_llm_response_if_oversized(content: str, *, model: str) -> str:
    """Clamp an oversized LLM response to :data:`MAX_LLM_RESPONSE_LENGTH`.

    Two CLI adapters carried byte-identical copies of this guard (#1787). A
    response-size limit is the kind of hardening that must not live in two
    places: a fix to one copy leaves the other unprotected, which is exactly
    how the ``response_format`` validators drifted in #1785.

    Lives in ``providers/`` rather than ``core.security`` because it logs, and
    ``core.security`` is a pure validation module with no logger. It sits
    alongside ``providers/retry.py``, the established home for behaviour shared
    across adapters.
    """
    is_valid, _ = InputValidator.validate_llm_response(content)
    if is_valid:
        return content

    log.warning(
        "llm.response.truncated",
        model=model,
        original_length=len(content),
        max_length=MAX_LLM_RESPONSE_LENGTH,
    )
    return content[:MAX_LLM_RESPONSE_LENGTH]


__all__ = ["truncate_llm_response_if_oversized"]
