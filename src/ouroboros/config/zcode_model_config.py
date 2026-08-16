"""Readiness probe for the Zcode CLI's model provider configuration.

The Zcode CLI resolves its model provider from ``~/.zcode/cli/config.json``
before every run, including the headless ``--prompt --json`` invocations
Ouroboros spawns. When the file is missing or carries no usable
``model.main`` reference, the CLI exits immediately with
``Model config is missing.`` — after Ouroboros has already assembled the
prompt and session, so the failure only surfaces mid-execution.

Two config shapes select the main model (measured from Zcode CLI 0.15.x and
0.16.x):

* ``{"model": "provider-id/model-id", "provider": {...}}``
* ``{"model": {"main": "provider-id/model-id"}, "provider": {...}}``

``model.main`` must be the ``"provider-id/model-id"`` *string*. An object
entry is silently dropped by the CLI's config parser, which then reports the
same ``Model config is missing`` error. The provider id referenced by
``model.main`` must have an entry under the same file's ``provider``
registry; that entry carries the ``kind``, ``options.baseURL`` and
``options.apiKey`` values the CLI needs. The desktop app's
``~/.zcode/v2/config.json`` uses the same registry shape and is the usual
copy source for a CLI-side setup.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class ZcodeModelConfigStatus:
    """Outcome of probing ``~/.zcode/cli/config.json`` for a main model."""

    ok: bool
    detail: str
    config_path: Path


def default_zcode_cli_config_path() -> Path:
    """Return the Zcode CLI config path the CLI itself names in its errors."""

    home = os.environ.get("HOME")
    base = Path(home).expanduser() if home else Path.home()
    return base / ".zcode" / "cli" / "config.json"


def _split_model_ref(ref: str) -> tuple[str, str] | None:
    if "/" not in ref:
        return None
    provider_id, _, model_id = ref.partition("/")
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not provider_id or not model_id:
        return None
    return provider_id, model_id


def inspect_zcode_model_config(
    config_path: Path | None = None,
) -> ZcodeModelConfigStatus:
    """Check that the Zcode CLI has a resolvable main model provider.

    The probe mirrors the CLI's own acceptance rules: a string
    ``"provider-id/model-id"`` reference under ``model`` or ``model.main``,
    plus a matching entry in the file's ``provider`` registry carrying
    non-empty ``kind``, ``options.baseURL``, and ``options.apiKey``. Anything
    else reproduces the CLI's ``Model config is missing`` failure, so the
    returned status explains the exact gap instead of letting setup and health
    checks report success.
    """

    path = config_path or default_zcode_cli_config_path()
    if not path.is_file():
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"{path} not found; the headless Zcode CLI exits with "
            "'Model config is missing' until it defines model.main",
            config_path=path,
        )

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"{path} is not readable JSON: {exc}",
            config_path=path,
        )

    if not isinstance(data, dict):
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"{path.name} top-level JSON value is not an object",
            config_path=path,
        )

    model = data.get("model")
    main_ref: str | None = None
    if isinstance(model, str):
        main_ref = model
    elif isinstance(model, dict):
        main = model.get("main")
        if isinstance(main, dict):
            return ZcodeModelConfigStatus(
                ok=False,
                detail="model.main is an object; the Zcode CLI only accepts the "
                '"provider-id/model-id" string form and silently drops object entries',
                config_path=path,
            )
        if isinstance(main, str):
            main_ref = main

    if not main_ref:
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f'{path.name} has no model or model.main "provider-id/model-id" reference',
            config_path=path,
        )

    parsed = _split_model_ref(main_ref)
    if parsed is None:
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"model reference {main_ref!r} is not in 'provider-id/model-id' form",
            config_path=path,
        )
    provider_id, model_id = parsed

    registry = data.get("provider")
    entry = registry.get(provider_id) if isinstance(registry, dict) else None
    if not isinstance(entry, dict):
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"provider {provider_id!r} referenced by model.main has no entry "
            f"in {path.name}'s provider registry (kind, options.baseURL, options.apiKey)",
            config_path=path,
        )

    options = entry.get("options")
    kind = entry.get("kind")
    base_url = options.get("baseURL") if isinstance(options, dict) else None
    api_key = options.get("apiKey") if isinstance(options, dict) else None
    if not isinstance(kind, str) or not kind.strip():
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"provider {provider_id!r} entry has no non-empty kind",
            config_path=path,
        )
    if not isinstance(base_url, str) or not base_url.strip():
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"provider {provider_id!r} entry has no non-empty options.baseURL",
            config_path=path,
        )
    if not isinstance(api_key, str) or not api_key.strip():
        return ZcodeModelConfigStatus(
            ok=False,
            detail=f"provider {provider_id!r} entry has no non-empty options.apiKey",
            config_path=path,
        )

    return ZcodeModelConfigStatus(
        ok=True,
        detail=f"model.main -> {provider_id}/{model_id} (provider entry present)",
        config_path=path,
    )
