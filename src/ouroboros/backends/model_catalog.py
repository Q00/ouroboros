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
``list_command`` argv that prints one model id per line. No backend ships
one yet — per-CLI support must be verified individually before wiring it —
so ``refresh_models`` currently degrades to ``None`` (use the static
catalog) for every backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess

from ouroboros.backends.capabilities import (
    get_backend_capability,
    runtime_backend_choices,
)
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL

# Backends whose runnable model is the CLI's own configured default rather
# than a Claude model id. Mirrors the loader's sentinel frozensets
# (_CODEX_LLM_BACKENDS et al.); the mirror is locked by a unit test.
_SENTINEL_MODEL_BACKENDS = frozenset({"codex", "opencode", "kiro", "copilot", "hermes", "pi"})

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
        list_command: Optional argv that prints one available model id per
            line. ``None`` means dynamic listing is not verified for this
            backend and callers must use the static ``models``.
    """

    backend: str
    models: tuple[str, ...]
    list_command: tuple[str, ...] | None = None

    @property
    def default_model(self) -> str:
        """Best default model id, matching the loader's backend mapping."""
        return self.models[0] if self.models else DEFAULT_MODEL_SENTINEL


def _build_catalogs() -> dict[str, BackendModelCatalog]:
    catalogs: dict[str, BackendModelCatalog] = {}
    for name in runtime_backend_choices():
        if name in _SENTINEL_MODEL_BACKENDS:
            models: tuple[str, ...] = (DEFAULT_MODEL_SENTINEL,)
        else:
            models = (DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL)
        catalogs[name] = BackendModelCatalog(backend=name, models=models)
    # LLM-only backend: model ids are provider-prefixed free-form strings,
    # so the catalog is custom-entry-only.
    catalogs["litellm"] = BackendModelCatalog(backend="litellm", models=())
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


def refresh_models(
    backend: str,
    *,
    timeout_seconds: float = _LIST_COMMAND_TIMEOUT_SECONDS,
) -> tuple[str, ...] | None:
    """Dynamically list models for a backend, or ``None`` to use the static catalog.

    Degrades to ``None`` (never raises) when the backend declares no
    verified ``list_command``, the command fails, times out, or produces
    no parseable output.
    """
    catalog = get_model_catalog(backend)
    if catalog.list_command is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - argv is a code-owned constant
            catalog.list_command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    models = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    return models or None


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
            return configured
    if capability.cli_name:
        return shutil.which(capability.cli_name)
    return None


def installed_backends() -> dict[str, str | None]:
    """Map every runtime-capable backend to its CLI path (``None`` = not installed)."""
    return {name: detect_backend_cli(name) for name in runtime_backend_choices()}


__all__ = [
    "DEFAULT_MODEL_SENTINEL",
    "BackendModelCatalog",
    "detect_backend_cli",
    "get_model_catalog",
    "installed_backends",
    "model_choices",
    "refresh_models",
]
