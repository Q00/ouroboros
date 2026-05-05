"""GitHub Copilot CLI model discovery.

Copilot CLI accepts model IDs in dotted form (``claude-opus-4.6``,
``gpt-5.2``) which differ from the hyphenated Anthropic SDK form Ouroboros
uses internally as defaults (``claude-opus-4-6``). The Copilot CLI itself has
no ``models list`` subcommand, so the canonical source is GitHub's Copilot
models endpoint at ``https://api.githubcopilot.com/models``.

This module:

* Calls that endpoint with the user's ``gh auth token`` (or ``GH_TOKEN`` /
  ``GITHUB_TOKEN`` env vars) and parses the response into typed records.
* Caches the result in process so setup wizards and adapters don't re-hit the
  API on every call.
* Falls back to a hardcoded snapshot when the API or auth fails so offline
  setup still produces a usable list.
* Exposes :func:`map_to_copilot_model` which translates an internal Ouroboros
  model name (typically the Anthropic hyphen form) to a Copilot-valid ID by
  consulting the discovered list, falling back to a static map of well-known
  Anthropic IDs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import json
import os
import shutil
import subprocess
from typing import Any
import urllib.error
import urllib.request

import structlog

log = structlog.get_logger(__name__)

_MODELS_URL = "https://api.githubcopilot.com/models"
_REQUEST_TIMEOUT_SECONDS = 5.0
_INTEGRATION_ID = "copilot-cli"
_EDITOR_VERSION = "ouroboros-discovery/1.0"


@dataclass(frozen=True)
class CopilotModel:
    """One model entry as returned by the Copilot models API."""

    id: str
    family: str = ""
    vendor: str = ""
    name: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)


_FALLBACK_MODELS: tuple[CopilotModel, ...] = (
    CopilotModel(id="claude-opus-4.7", family="claude-opus-4.7", vendor="Anthropic", name="Claude Opus 4.7"),
    CopilotModel(id="claude-opus-4.6", family="claude-opus-4.6", vendor="Anthropic", name="Claude Opus 4.6"),
    CopilotModel(id="claude-opus-4.5", family="claude-opus-4.5", vendor="Anthropic", name="Claude Opus 4.5"),
    CopilotModel(id="claude-sonnet-4.6", family="claude-sonnet-4.6", vendor="Anthropic", name="Claude Sonnet 4.6"),
    CopilotModel(id="claude-sonnet-4.5", family="claude-sonnet-4.5", vendor="Anthropic", name="Claude Sonnet 4.5"),
    CopilotModel(id="claude-sonnet-4", family="claude-sonnet-4", vendor="Anthropic", name="Claude Sonnet 4"),
    CopilotModel(id="claude-haiku-4.5", family="claude-haiku-4.5", vendor="Anthropic", name="Claude Haiku 4.5"),
    CopilotModel(id="gpt-5.4", family="gpt-5.4", vendor="OpenAI", name="GPT-5.4"),
    CopilotModel(id="gpt-5.3-codex", family="gpt-5.3-codex", vendor="OpenAI", name="GPT-5.3 Codex"),
    CopilotModel(id="gpt-5.2", family="gpt-5.2", vendor="OpenAI", name="GPT-5.2"),
    CopilotModel(id="gpt-5-mini", family="gpt-5-mini", vendor="OpenAI", name="GPT-5 mini"),
    CopilotModel(id="gpt-4.1", family="gpt-4.1", vendor="OpenAI", name="GPT-4.1"),
)

_STATIC_NAME_MAP: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-7": "claude-opus-4.7",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-haiku-4-5": "claude-haiku-4.5",
    "openrouter/anthropic/claude-opus-4-6": "claude-opus-4.6",
    "openrouter/anthropic/claude-sonnet-4-6": "claude-sonnet-4.6",
}

_cached_models: tuple[CopilotModel, ...] | None = None
_cached_used_fallback: bool = False


def reset_cache() -> None:
    """Clear the in-process discovery cache. Intended for tests and setup retries."""
    global _cached_models, _cached_used_fallback
    _cached_models = None
    _cached_used_fallback = False


def _resolve_token() -> str | None:
    """Resolve a Copilot-capable bearer token from env or ``gh auth token``."""
    for env_key in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_TOKEN"):
        value = os.environ.get(env_key)
        if value:
            return value.strip()

    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        completed = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("copilot.discovery.gh_token_failed", error=str(exc))
        return None
    if completed.returncode != 0:
        log.debug(
            "copilot.discovery.gh_token_nonzero",
            returncode=completed.returncode,
            stderr=completed.stderr.strip()[:200],
        )
        return None
    token = completed.stdout.strip()
    return token or None


def _fetch_models_json(token: str) -> dict[str, Any] | None:
    """Hit the Copilot models endpoint. Returns parsed JSON or ``None`` on failure."""
    request = urllib.request.Request(  # noqa: S310 - fixed scheme/host
        _MODELS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Editor-Version": _EDITOR_VERSION,
            "Copilot-Integration-Id": _INTEGRATION_ID,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.info("copilot.discovery.fetch_failed", error=str(exc))
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.info("copilot.discovery.parse_failed", error=str(exc))
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _parse_models_payload(payload: dict[str, Any]) -> list[CopilotModel]:
    raw_entries = payload.get("data")
    if not isinstance(raw_entries, list):
        return []
    out: list[CopilotModel] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
        family = ""
        if isinstance(capabilities, dict):
            family_value = capabilities.get("family")
            if isinstance(family_value, str):
                family = family_value
        vendor = entry.get("vendor") if isinstance(entry.get("vendor"), str) else ""
        name = entry.get("name") if isinstance(entry.get("name"), str) else ""
        out.append(
            CopilotModel(
                id=model_id,
                family=family,
                vendor=vendor or "",
                name=name or "",
                capabilities=capabilities if isinstance(capabilities, dict) else {},
            )
        )
    return out


def list_copilot_models(*, refresh: bool = False) -> list[CopilotModel]:
    """Return the available Copilot models, preferring the live API.

    Args:
        refresh: When ``True``, ignore the in-process cache and re-query.

    Returns:
        Discovered models, or the hardcoded fallback snapshot if discovery
        fails. Never raises.
    """
    global _cached_models, _cached_used_fallback
    if not refresh and _cached_models is not None:
        return list(_cached_models)

    token = _resolve_token()
    if token:
        payload = _fetch_models_json(token)
        if payload is not None:
            parsed = _parse_models_payload(payload)
            if parsed:
                _cached_models = tuple(parsed)
                _cached_used_fallback = False
                return list(_cached_models)

    log.info("copilot.discovery.using_fallback", count=len(_FALLBACK_MODELS))
    _cached_models = _FALLBACK_MODELS
    _cached_used_fallback = True
    return list(_cached_models)


def used_fallback() -> bool:
    """Return True if the last :func:`list_copilot_models` call used the snapshot."""
    return _cached_used_fallback


def map_to_copilot_model(
    model: str,
    *,
    available: Iterable[CopilotModel] | None = None,
) -> str:
    """Translate an internal model name to a Copilot-valid ID.

    Resolution order:

    1. If ``model`` already matches one of ``available`` model IDs verbatim,
       return it unchanged.
    2. If ``model`` is in the static name map (well-known Anthropic IDs),
       return the mapped form.
    3. If ``model`` strips to a known dotted family present in ``available``,
       return that ID.
    4. Otherwise return ``model`` unchanged so the caller can decide whether
       to fail or pass it through.
    """
    if not model:
        return model

    model_clean = model.strip()
    if not model_clean:
        return model

    # Fast path: dotted forms (the Copilot canonical shape) and explicit
    # passthrough markers skip discovery entirely. This keeps the adapter
    # zero-cost in unit tests and avoids spawning ``gh auth token`` for
    # callers that already supply a Copilot-valid ID.
    if model_clean == "default" or "." in model_clean:
        return model_clean

    mapped_static = _STATIC_NAME_MAP.get(model_clean)
    if mapped_static is not None and available is None:
        return mapped_static

    pool = list(available) if available is not None else list_copilot_models()
    available_ids = {entry.id for entry in pool}
    families = {entry.family for entry in pool if entry.family}

    if model_clean in available_ids:
        return model_clean

    if mapped_static and (not available_ids or mapped_static in available_ids or mapped_static in families):
        return mapped_static

    # Last attempt: hyphen-to-dot conversion when the dotted variant exists.
    if "-" in model_clean:
        dotted_candidate = model_clean.replace("-", ".")
        if dotted_candidate in available_ids:
            return dotted_candidate

    return model_clean
