"""External invocation provenance for ``ooo auto`` sessions.

When ``ooo auto`` is launched by an external rewrite gateway (e.g. a Discord
bot translating a natural-language request into a shell command) the durable
auto state should record *that fact* — not the raw user message, not channel
identifiers, not credentials — so post-hoc incident analysis can distinguish
direct CLI use from rewritten invocations.

This module defines the narrow contract:

* The runtime accepts provenance via the ``OUROBOROS_AUTO_PROVENANCE_JSON``
  environment variable. The payload MUST be a JSON object.
* :func:`load_provenance_from_env` returns a redacted dict suitable for
  persistence into ``AutoPipelineState.provenance``.
* :func:`redact_provenance` enforces an allowlist + length cap and returns a
  shallow copy. Nothing outside the allowlist is ever persisted.
* The default invocation (no env var, no flag) records nothing — the field is
  an empty dict and the CLI continues to behave as before.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from typing import Any, Final

PROVENANCE_ENV_VAR: Final[str] = "OUROBOROS_AUTO_PROVENANCE_JSON"

#: Allowlist of provenance keys that may be persisted. Anything outside this
#: set is dropped silently by :func:`redact_provenance`. Keep this list short
#: and review every addition for sensitive-data risk.
ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "invoked_by",  # "direct" | "gateway" | "unknown"
        "source_platform",  # high-level system name, e.g. "discord-hermes"
        "command_kind",  # "rewrite" | "direct" | other low-card label
        "requested_at",  # ISO-8601 timestamp string
        "request_correlation_id",  # opaque id (NOT a token)
        "notes",  # short free-form note (truncated)
    }
)

#: Recognized values for ``invoked_by``. Other values are mapped to
#: ``"unknown"`` so persisted state stays a closed set.
KNOWN_INVOKED_BY: Final[frozenset[str]] = frozenset({"direct", "gateway", "unknown"})

#: Per-string truncation cap to keep arbitrarily long fields out of state JSON.
MAX_STRING_LEN: Final[int] = 256


def redact_provenance(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a redacted, allowlisted, length-capped copy of ``raw``.

    Keys outside :data:`ALLOWED_KEYS` are dropped. Non-string values for
    string-typed keys are dropped rather than coerced. Strings longer than
    :data:`MAX_STRING_LEN` are truncated. Empty/whitespace-only strings are
    dropped. ``invoked_by`` outside :data:`KNOWN_INVOKED_BY` is mapped to
    ``"unknown"``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in ALLOWED_KEYS:
            continue
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if len(cleaned) > MAX_STRING_LEN:
            cleaned = cleaned[:MAX_STRING_LEN]
        if key == "invoked_by" and cleaned not in KNOWN_INVOKED_BY:
            cleaned = "unknown"
        out[key] = cleaned
    return out


def load_provenance_from_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Parse provenance metadata from ``OUROBOROS_AUTO_PROVENANCE_JSON``.

    Returns ``{}`` when the env var is unset, empty, malformed, or non-object;
    never raises. The returned dict is already redacted via
    :func:`redact_provenance`.
    """
    source = env if env is not None else os.environ
    raw = source.get(PROVENANCE_ENV_VAR)
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return redact_provenance(parsed)


def invoked_by_label(provenance: Mapping[str, Any] | None) -> str:
    """Return the human label for ``invoked_by``; ``"direct"`` when unknown.

    Used by CLI/blocker output to make rewrite-vs-direct provenance visible
    without exposing any other metadata.
    """
    if not provenance:
        return "direct"
    value = provenance.get("invoked_by")
    if isinstance(value, str) and value in KNOWN_INVOKED_BY:
        return value
    return "direct"


__all__ = [
    "ALLOWED_KEYS",
    "KNOWN_INVOKED_BY",
    "MAX_STRING_LEN",
    "PROVENANCE_ENV_VAR",
    "invoked_by_label",
    "load_provenance_from_env",
    "redact_provenance",
]
