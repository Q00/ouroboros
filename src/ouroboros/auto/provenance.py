"""Resolve optional gateway-provenance metadata for ``ooo auto``.

External gateways (Discord/Hermes deterministic command rewrite, etc.) attach
context describing how a natural-language request became an ``ooo auto``
invocation. The contract is intentionally gateway-internal: the gateway sets
``OUROBOROS_AUTO_PROVENANCE_JSON`` in the child environment before exec'ing
``ooo auto``. There is no user-facing CLI flag — this metadata is not part of
the direct CLI UX and we do not want to surface it as such.

Direct CLI invocations leave the env var unset and observe the historical
behaviour: no provenance is recorded and downstream code paths are unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from typing import Any

from ouroboros.auto.state import redact_provenance

PROVENANCE_ENV_VAR = "OUROBOROS_AUTO_PROVENANCE_JSON"
# Allowlisted provenance is hash- and label-sized; 4 KiB is generous for the
# entire JSON envelope and bounds memory if a misconfigured gateway sets a
# pathological env value.
_MAX_PROVENANCE_BYTES = 4096


def _decode(payload: str) -> dict[str, Any]:
    text = payload.strip()
    if not text:
        msg = f"{PROVENANCE_ENV_VAR} provenance payload is empty"
        raise ValueError(msg)
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{PROVENANCE_ENV_VAR} provenance payload is not valid JSON: {exc.msg}"
        raise ValueError(msg) from exc
    if not isinstance(loaded, dict):
        msg = f"{PROVENANCE_ENV_VAR} provenance payload must be a JSON object"
        raise ValueError(msg)
    return loaded


def resolve_provenance(
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return a redacted provenance dict from the gateway env var, or None.

    ``env`` defaults to ``os.environ`` so the standard call site (the auto
    CLI) picks up gateway-supplied configuration without ceremony; pass an
    explicit mapping (including ``{}``) to suppress that lookup in tests.
    Returning ``None`` covers both "no input provided" and "input only had
    keys that were dropped by redaction" — callers must treat the two
    identically.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    env_value = env_map.get(PROVENANCE_ENV_VAR)
    if env_value is None or not env_value.strip():
        return None
    if len(env_value) > _MAX_PROVENANCE_BYTES:
        msg = (
            f"{PROVENANCE_ENV_VAR} exceeds {_MAX_PROVENANCE_BYTES}-byte cap (got {len(env_value)})"
        )
        raise ValueError(msg)
    raw = _decode(env_value)
    return redact_provenance(raw)


__all__ = ["PROVENANCE_ENV_VAR", "resolve_provenance"]
