"""Per-backend model catalog and installed-CLI detection (#1412).

Settings surfaces (the ``ouroboros config`` GUI, ourocode) need to offer
model choices per runtime backend without hardcoding model ids in UI code.
This module owns that catalog as a sibling of the capability registry.

The static catalog deliberately **mirrors** the backend-default-model
mapping in ``ouroboros.config.loader._default_model_for_backend``: backends
that cannot run Claude model ids get the ``"default"`` sentinel (the CLI's
own configured model), everything else gets the shipped Claude defaults.
A unit test locks the mirror so the two cannot drift silently. The mapping
is duplicated here instead of imported because ``config.loader`` imports
``ouroboros.backends`` — a module-level import in this direction would be
circular.

Dynamic refresh is an explicit opt-in hook: a backend may declare a
``list_command`` argv whose stdout is parsed into callable model ids. OpenCode,
Kiro, and Grok are wired because their listing commands have been verified;
other backends degrade to ``None`` (use the static catalog) until their CLI
support is verified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess

from ouroboros.backends.capabilities import (
    get_backend_capability,
    runtime_backend_choices,
)
from ouroboros.codex.home import resolve_codex_home
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL

# Backends whose runnable model is the CLI's own configured default rather
# than a Claude model id. Mirrors the loader's sentinel frozensets
# (_CODEX_LLM_BACKENDS et al.); the mirror is locked by a unit test.
_SENTINEL_MODEL_BACKENDS = frozenset(
    {
        "codex",
        "opencode",
        "kiro",
        "copilot",
        "hermes",
        "pi",
        "gjc",
        "antigravity",
        "grok",
        "zcode",
    }
)

# The sentinel the loader maps Claude-incapable backends to.
DEFAULT_MODEL_SENTINEL = "default"

_LIST_COMMAND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BackendModelCatalog:
    """Known model choices for one canonical backend.

    Attributes:
        backend: Canonical backend name.
        models: Known model ids, best-default first. May be empty for
            backends whose model space is free-form (e.g. litellm provider
            routes) — UIs must always offer a free-text custom entry on top
            of this tuple regardless of its length.
        list_args: Optional primary CLI subcommand argv (appended to the
            resolved backend binary) whose stdout is parsed into available
            model ids. ``None`` means dynamic listing is unsupported and
            callers must use the static ``models``.
    """

    backend: str
    models: tuple[str, ...]
    list_args: tuple[str, ...] | None = None

    @property
    def default_model(self) -> str:
        """Best default model id, matching the loader's backend mapping."""
        return self.models[0] if self.models else DEFAULT_MODEL_SENTINEL


# Hand-curated additions per backend, appended after the loader-mirroring
# default entry. Keep entries verifiable: the codex ids below were confirmed
# against a live `opencode models` listing of the OpenAI catalog.
_EXTRA_KNOWN_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("claude-haiku-4-5-20251001",),
    "codex": ("gpt-5-codex", "gpt-5", "gpt-5-mini"),
    # Grok Build model slugs, after the CLI-owned "default" sentinel. Verified
    # against a live `grok models` listing (grok-build, grok-composer-2.5-fast).
    "grok": ("grok-build", "grok-composer-2.5-fast"),
}

# Verified model-listing subcommands. The resolved backend binary is invoked
# with these args and the stdout is parsed by the backend's entry in
# `_LIST_PARSERS` (default: one model id per line).
_LIST_ARGS: dict[str, tuple[str, ...]] = {
    "opencode": ("models",),
    "kiro": ("chat", "--listmodels", "-f", "json"),
    "grok": ("models",),
}

# Kiro renamed the long-form listing flags across CLI releases. Prefer the
# compact form verified by current users, then retry the newer spelling before
# degrading to the static catalog.
_LIST_ARG_FALLBACKS: dict[str, tuple[tuple[str, ...], ...]] = {
    "kiro": (("chat", "--list-models", "--format", "json"),),
}


def _parse_models_one_per_line(stdout: str) -> tuple[str, ...]:
    """Default listing parser: one model id per non-blank line."""
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def _parse_kiro_models(stdout: str) -> tuple[str, ...]:
    """Parse Kiro's JSON model listing into callable model ids.

    Kiro releases have emitted either a top-level list or an object containing
    ``models``/``data``. Entries may be bare ids or records. Accept only narrow
    id-like fields so display names and descriptions can never become
    ``--model`` values. Invalid or unknown payloads degrade to an empty tuple.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ()

    entries: object
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("models", payload.get("data"))
    else:
        return ()
    if not isinstance(entries, list):
        return ()

    models: list[str] = []
    for entry in entries:
        model_id: object = entry
        if isinstance(entry, dict):
            model_id = next(
                (
                    entry[key]
                    for key in ("id", "modelId", "model_id", "model", "value")
                    if isinstance(entry.get(key), str)
                ),
                None,
            )
        if isinstance(model_id, str) and (value := model_id.strip()):
            models.append(value)
    return tuple(dict.fromkeys(models))


def _parse_grok_models(stdout: str) -> tuple[str, ...]:
    """Parse ``grok models`` output into callable model ids.

    Grok prints auth / default-model headers and a bulleted "Available models"
    list rather than one id per line, e.g.::

        You are logged in with grok.com.

        Default model: grok-composer-2.5-fast

        Available models:
          - grok-build
          * grok-composer-2.5-fast (default)

    Extract the bulleted ids (dropping the ``-``/``*`` bullet and any trailing
    ``(default)`` marker), preserving order and de-duplicating, so the catalog
    reflects exactly what the user's Grok CLI can call.
    """
    models: list[str] = []
    in_list = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("available models"):
            in_list = True
            continue
        if in_list and stripped[0] in "-*":
            parts = stripped[1:].strip().split()
            if parts:
                models.append(parts[0])
    return tuple(dict.fromkeys(models))


# Backend-specific stdout parsers for `refresh_models`. Backends absent here use
# `_parse_models_one_per_line`.
_LIST_PARSERS: dict[str, Callable[[str], tuple[str, ...]]] = {
    "kiro": _parse_kiro_models,
    "grok": _parse_grok_models,
}


def _build_catalogs() -> dict[str, BackendModelCatalog]:
    catalogs: dict[str, BackendModelCatalog] = {}
    for name in runtime_backend_choices():
        if name in _SENTINEL_MODEL_BACKENDS:
            models: tuple[str, ...] = (DEFAULT_MODEL_SENTINEL,)
        else:
            models = (DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL)
        models = models + _EXTRA_KNOWN_MODELS.get(name, ())
        catalogs[name] = BackendModelCatalog(
            backend=name,
            models=models,
            list_args=_LIST_ARGS.get(name),
        )
    # LLM-only backends: litellm model ids are provider/backend-owned
    # free-form strings, so the catalog is custom-entry-only. ourocode ACP maps
    # known backend selectors only; keep its catalog explicit so settings
    # surfaces do not imply arbitrary model-id support.
    catalogs["litellm"] = BackendModelCatalog(backend="litellm", models=())
    catalogs["ourocode"] = BackendModelCatalog(
        backend="ourocode",
        models=("claude", "claude_api", "codex", "gemini"),
    )
    # dsh's model choice lives in its Cordis composition file; the ACP wire
    # has no per-session model parameter, so the catalog is custom-entry-only.
    catalogs["dsh"] = BackendModelCatalog(backend="dsh", models=())
    return catalogs


_CATALOGS: dict[str, BackendModelCatalog] = _build_catalogs()


def get_model_catalog(backend: str) -> BackendModelCatalog:
    """Return the model catalog for a backend name or alias.

    Raises:
        ValueError: If the backend is unknown.
    """
    capability = get_backend_capability(backend)
    if capability is None or capability.name not in _CATALOGS:
        msg = f"No model catalog for backend: {backend.strip().lower()}"
        raise ValueError(msg)
    return _CATALOGS[capability.name]


def model_choices(backend: str) -> tuple[str, ...]:
    """Known model choices for a backend (UIs append a custom entry)."""
    return get_model_catalog(backend).models


def uses_default_model_sentinel(backend: str) -> bool:
    """Whether ``"default"`` is a safe persisted model value for this backend."""
    capability = get_backend_capability(backend)
    return capability is not None and capability.name in _SENTINEL_MODEL_BACKENDS


def refresh_models(
    backend: str,
    *,
    timeout_seconds: float = _LIST_COMMAND_TIMEOUT_SECONDS,
) -> tuple[str, ...] | None:
    """Dynamically list models for a backend, or ``None`` to use the static catalog.

    Runs the backend's verified listing subcommand against its resolved CLI
    binary (configured path first, then PATH). Backends may declare compatible
    fallback spellings for CLI-version transitions. Degrades to ``None`` (never
    raises) when listing is unsupported, the CLI is not installed, every
    command fails or times out, or every output is unparseable.
    """
    catalog = get_model_catalog(backend)
    if catalog.list_args is None:
        return None
    cli_path = detect_backend_cli(backend)
    if cli_path is None:
        return None

    parser = _LIST_PARSERS.get(catalog.backend, _parse_models_one_per_line)
    commands = (catalog.list_args, *_LIST_ARG_FALLBACKS.get(catalog.backend, ()))
    for list_args in commands:
        try:
            result = subprocess.run(  # noqa: S603 - resolved binary + code-owned args
                (cli_path, *list_args),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=True,
            )
        except OSError:
            return None
        except subprocess.SubprocessError:
            continue
        models = parser(result.stdout)
        if models:
            return models
    return None


# Maps canonical backend name → loader getter for its configured CLI path.
# claude's getter is get_cli_path (the SDK-bundled CLI override).
_CLI_PATH_GETTERS: dict[str, str] = {
    "claude": "get_cli_path",
    "codex": "get_codex_cli_path",
    "copilot": "get_copilot_cli_path",
    "gemini": "get_gemini_cli_path",
    "hermes": "get_hermes_cli_path",
    "kiro": "get_kiro_cli_path",
    "opencode": "get_opencode_cli_path",
    "goose": "get_goose_cli_path",
    "pi": "get_pi_cli_path",
    "gjc": "get_gjc_cli_path",
    "antigravity": "get_antigravity_cli_path",
    "grok": "get_grok_cli_path",
    "ourocode": "get_ourocode_cli_path",
    # Zcode is runtime-only and Claude-incapable (runs Z.ai GLM-5). Its CLI
    # path is resolved exactly like the other runtime-only CLIs above: env
    # / config / macOS app-bundle default, via ``get_zcode_cli_path``.
    # Without this entry, ``detect_backend_cli("zcode")`` ignored the
    # configured path and Zcode was invisible to ``installed_backends()``.
    "zcode": "get_zcode_cli_path",
    "dsh": "get_dsh_cli_path",
}


def detect_backend_cli(backend: str) -> str | None:
    """Return the resolved CLI path for a backend, or ``None`` if not installed.

    Resolution mirrors runtime construction: the explicitly configured path
    (env var / config.yaml, via the loader getter) wins, then ``PATH``
    lookup of the capability's ``cli_name``. Backends without a CLI surface
    (litellm) return ``None``.
    """
    capability = get_backend_capability(backend)
    if capability is None:
        msg = f"Unsupported backend: {backend.strip().lower()}"
        raise ValueError(msg)
    getter_name = _CLI_PATH_GETTERS.get(capability.name)
    if getter_name is not None:
        # Deferred import: config.loader imports ouroboros.backends, so a
        # module-level import here would be circular.
        from ouroboros.config import loader as config_loader

        configured = getattr(config_loader, getter_name)()
        if configured:
            configured_path = _resolve_executable_candidate(configured)
            if configured_path is not None:
                return configured_path
    if capability.cli_name:
        return shutil.which(capability.cli_name)
    return None


def _resolve_executable_candidate(candidate: str) -> str | None:
    """Return an executable path for a configured CLI candidate, if usable."""
    value = candidate.strip()
    if not value:
        return None
    if os.sep not in value and (os.altsep is None or os.altsep not in value):
        return shutil.which(value)
    path = Path(value).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def installed_backends() -> dict[str, str | None]:
    """Map every runtime-capable backend to its CLI path (``None`` = not installed)."""
    return {name: detect_backend_cli(name) for name in runtime_backend_choices()}


def configured_default_model(backend: str) -> str | None:
    """Resolve what the ``"default"`` sentinel currently means for a backend.

    Sentinel backends defer model choice to the CLI's own user config; this
    reads only the model field from that file so settings UIs can render
    "default — currently <model>" instead of an opaque sentinel. Returns
    ``None`` when the backend keeps no such file, the file is missing, or
    parsing fails — never raises.
    """
    capability = get_backend_capability(backend)
    if capability is None:
        return None
    try:
        if capability.name == "hermes":
            import yaml

            config_path = Path.home() / ".hermes" / "config.yaml"
            if not config_path.exists():
                return None
            data = yaml.safe_load(config_path.read_text()) or {}
            model = data.get("model")
            if isinstance(model, dict):
                value = model.get("default")
                return str(value) if value else None
            return None
        if capability.name == "codex":
            import tomllib

            config_path = resolve_codex_home() / "config.toml"
            if not config_path.exists():
                return None
            data = tomllib.loads(config_path.read_text())
            value = data.get("model")
            return str(value) if value else None
    except Exception:  # noqa: BLE001 - a hint must never break the caller
        return None
    return None


__all__ = [
    "DEFAULT_MODEL_SENTINEL",
    "BackendModelCatalog",
    "configured_default_model",
    "detect_backend_cli",
    "get_model_catalog",
    "installed_backends",
    "model_choices",
    "refresh_models",
    "uses_default_model_sentinel",
]
