"""Terminal-error reading for Pi-family (``pi`` / ``omp``) assistant messages.

Both CLIs report provider and auth failures as an assistant message with
``stopReason: "error"`` while the process still exits 0. The LLM adapters in
this package and the orchestrator runtimes read the same message shape, so the
decision "does this message declare a failure, and with what reason" lives here
once instead of in four copies of the same closure.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

UNKNOWN_ERROR = "Unknown error"


def assistant_message_error(
    message: Any,
    extract_text: Callable[[dict[str, Any]], str | None],
) -> str | None:
    """Return the failure reason an assistant message declares, or ``None``.

    ``None`` means the message does not declare a failure. Once it does
    (``stopReason == "error"``) the result is always a non-empty string: the
    stated ``errorMessage``/``error``, else the message's own text, else
    :data:`UNKNOWN_ERROR`.
    """
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    if message.get("stopReason") != "error":
        return None
    error = message.get("errorMessage") or message.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    # A message that declares an error is still an error when it withholds the
    # reason. Callers test the result for truthiness, so an empty string here
    # handed back a failed run as a successful one, carrying whatever partial
    # text had been streamed before the failure.
    return extract_text(message) or UNKNOWN_ERROR


__all__ = ["UNKNOWN_ERROR", "assistant_message_error"]
