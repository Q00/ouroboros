"""Tests for the per-backend model catalog and installed-CLI detection (#1412)."""

from __future__ import annotations

import subprocess

import pytest

from ouroboros.backends import model_catalog as mc
from ouroboros.backends import runtime_backend_choices
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL
from ouroboros.config.loader import _default_model_for_backend


@pytest.mark.parametrize("backend", runtime_backend_choices())
def test_every_runtime_backend_has_catalog_with_models(backend: str) -> None:
    catalog = mc.get_model_catalog(backend)
    assert catalog.backend == backend
    assert len(catalog.models) >= 1
    assert catalog.default_model == catalog.models[0]


@pytest.mark.parametrize("backend", runtime_backend_choices())
def test_catalog_default_mirrors_loader_backend_mapping(backend: str) -> None:
    """The static catalog must not drift from the loader's sentinel mapping."""
    loader_default = _default_model_for_backend(DEFAULT_OPUS_MODEL, backend=backend)
    assert mc.get_model_catalog(backend).default_model == loader_default


def test_claude_catalog_lists_shipped_defaults() -> None:
    assert mc.model_choices("claude") == (DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL)


def test_alias_resolves_to_canonical_catalog() -> None:
    assert mc.get_model_catalog("claude_code") is mc.get_model_catalog("claude")
    assert mc.get_model_catalog("codex_cli") is mc.get_model_catalog("codex")


def test_litellm_catalog_is_custom_only() -> None:
    catalog = mc.get_model_catalog("litellm")
    assert catalog.models == ()
    assert catalog.default_model == mc.DEFAULT_MODEL_SENTINEL


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="No model catalog"):
        mc.get_model_catalog("not-a-backend")


def test_refresh_models_without_list_command_degrades_to_none() -> None:
    # No backend ships a verified list_command yet (see module docstring).
    for backend in runtime_backend_choices():
        assert mc.refresh_models(backend) is None


def test_refresh_models_failing_command_degrades_to_none(monkeypatch) -> None:
    catalog = mc.BackendModelCatalog(backend="claude", models=("m",), list_command=("x",))
    monkeypatch.setitem(mc._CATALOGS, "claude", catalog)

    def _boom(*args, **kwargs):
        raise subprocess.SubprocessError("listing failed")

    monkeypatch.setattr(mc.subprocess, "run", _boom)
    assert mc.refresh_models("claude") is None


def test_refresh_models_parses_one_model_per_line(monkeypatch) -> None:
    catalog = mc.BackendModelCatalog(backend="claude", models=("m",), list_command=("x",))
    monkeypatch.setitem(mc._CATALOGS, "claude", catalog)

    class _Result:
        stdout = "model-a\n  model-b  \n\n"

    monkeypatch.setattr(mc.subprocess, "run", lambda *_args, **_kwargs: _Result())
    assert mc.refresh_models("claude") == ("model-a", "model-b")


def test_detect_backend_cli_prefers_configured_path(monkeypatch) -> None:
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_codex_cli_path", lambda: "/opt/bin/codex")
    monkeypatch.setattr(mc.shutil, "which", lambda _name: "/usr/bin/should-not-win")
    assert mc.detect_backend_cli("codex") == "/opt/bin/codex"


def test_detect_backend_cli_falls_back_to_path_lookup(monkeypatch) -> None:
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_hermes_cli_path", lambda: None)
    monkeypatch.setattr(mc.shutil, "which", lambda _name: "/usr/local/bin/hermes")
    assert mc.detect_backend_cli("hermes") == "/usr/local/bin/hermes"


def test_detect_backend_cli_missing_everywhere_returns_none(monkeypatch) -> None:
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_pi_cli_path", lambda: None)
    monkeypatch.setattr(mc.shutil, "which", lambda _name: None)
    assert mc.detect_backend_cli("pi") is None


def test_detect_backend_cli_litellm_has_no_cli() -> None:
    assert mc.detect_backend_cli("litellm") is None


def test_installed_backends_covers_all_runtime_backends(monkeypatch) -> None:
    monkeypatch.setattr(mc, "detect_backend_cli", lambda name: f"/bin/{name}")
    result = mc.installed_backends()
    assert set(result) == set(runtime_backend_choices())
