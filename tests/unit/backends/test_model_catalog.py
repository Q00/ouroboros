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


def test_claude_catalog_lists_shipped_defaults_first() -> None:
    choices = mc.model_choices("claude")
    assert choices[:2] == (DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL)
    assert "claude-haiku-4-5-20251001" in choices


def test_codex_catalog_offers_known_models_after_sentinel() -> None:
    choices = mc.model_choices("codex")
    assert choices[0] == mc.DEFAULT_MODEL_SENTINEL
    assert "gpt-5-codex" in choices


def test_default_model_sentinel_support_follows_backend_contract() -> None:
    assert mc.uses_default_model_sentinel("codex") is True
    assert mc.uses_default_model_sentinel("codex_cli") is True
    assert mc.uses_default_model_sentinel("zcode") is True
    assert mc.uses_default_model_sentinel("zcode_cli") is True
    assert mc.uses_default_model_sentinel("claude") is False
    assert mc.uses_default_model_sentinel("claude_code") is False


def test_alias_resolves_to_canonical_catalog() -> None:
    assert mc.get_model_catalog("claude_code") is mc.get_model_catalog("claude")
    assert mc.get_model_catalog("codex_cli") is mc.get_model_catalog("codex")


def test_litellm_catalog_is_custom_only() -> None:
    catalog = mc.get_model_catalog("litellm")
    assert catalog.models == ()
    assert catalog.default_model == mc.DEFAULT_MODEL_SENTINEL


def test_ourocode_catalog_lists_supported_acp_selectors() -> None:
    catalog = mc.get_model_catalog("ourocode")
    assert catalog.models == ("claude", "claude_api", "codex", "gemini")
    assert catalog.default_model == "claude"


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="No model catalog"):
        mc.get_model_catalog("not-a-backend")


def test_refresh_models_without_list_args_degrades_to_none() -> None:
    for backend in runtime_backend_choices():
        if mc.get_model_catalog(backend).list_args is None:
            assert mc.refresh_models(backend) is None


def test_opencode_ships_verified_list_args() -> None:
    assert mc.get_model_catalog("opencode").list_args == ("models",)


def test_grok_ships_verified_list_args() -> None:
    assert mc.get_model_catalog("grok").list_args == ("models",)


def test_grok_static_catalog_lists_known_models_after_sentinel() -> None:
    choices = mc.model_choices("grok")
    assert choices[0] == mc.DEFAULT_MODEL_SENTINEL
    assert "grok-build" in choices
    assert "grok-composer-2.5-fast" in choices


def test_grok_models_parser_extracts_bulleted_ids() -> None:
    raw = (
        "You are logged in with grok.com.\n\n"
        "Default model: grok-composer-2.5-fast\n\n"
        "Available models:\n"
        "  - grok-build\n"
        "  * grok-composer-2.5-fast (default)\n"
    )
    assert mc._parse_grok_models(raw) == ("grok-build", "grok-composer-2.5-fast")


def test_grok_models_parser_ignores_headers_and_blanks() -> None:
    assert mc._parse_grok_models("You are logged in.\nDefault model: x\n") == ()


def test_refresh_models_grok_uses_custom_parser(monkeypatch) -> None:
    monkeypatch.setattr(mc, "detect_backend_cli", lambda _name: "/bin/grok")

    class _Result:
        stdout = "Available models:\n  - grok-build\n  * grok-composer-2.5-fast (default)\n"

    monkeypatch.setattr(mc.subprocess, "run", lambda *_a, **_k: _Result())
    assert mc.refresh_models("grok") == ("grok-build", "grok-composer-2.5-fast")


def test_refresh_models_uninstalled_cli_degrades_to_none(monkeypatch) -> None:
    monkeypatch.setattr(mc, "detect_backend_cli", lambda _name: None)
    assert mc.refresh_models("opencode") is None


def test_refresh_models_failing_command_degrades_to_none(monkeypatch) -> None:
    monkeypatch.setattr(mc, "detect_backend_cli", lambda _name: "/bin/opencode")

    def _boom(*args, **kwargs):
        raise subprocess.SubprocessError("listing failed")

    monkeypatch.setattr(mc.subprocess, "run", _boom)
    assert mc.refresh_models("opencode") is None


def test_refresh_models_parses_one_model_per_line(monkeypatch) -> None:
    monkeypatch.setattr(mc, "detect_backend_cli", lambda _name: "/bin/opencode")
    captured: dict[str, object] = {}

    class _Result:
        stdout = "model-a\n  model-b  \n\n"

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Result()

    monkeypatch.setattr(mc.subprocess, "run", _fake_run)
    assert mc.refresh_models("opencode") == ("model-a", "model-b")
    assert captured["argv"] == ("/bin/opencode", "models")


def test_detect_backend_cli_prefers_configured_path(monkeypatch) -> None:
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_codex_cli_path", lambda: "/opt/bin/codex")
    monkeypatch.setattr(mc.shutil, "which", lambda _name: "/usr/bin/should-not-win")
    assert mc.detect_backend_cli("codex") == "/opt/bin/codex"


def test_detect_backend_cli_uses_configured_ourocode_path(monkeypatch) -> None:
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_ourocode_cli_path", lambda: "/opt/bin/ourocode")
    monkeypatch.setattr(mc.shutil, "which", lambda _name: "/usr/bin/should-not-win")
    assert mc.detect_backend_cli("ourocode") == "/opt/bin/ourocode"


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


def test_configured_default_model_hermes(monkeypatch, tmp_path) -> None:
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "model:\n  default: gpt-9-hermes\n  provider: openai-codex\n"
    )
    monkeypatch.setattr(mc.Path, "home", classmethod(lambda _cls: tmp_path))
    assert mc.configured_default_model("hermes") == "gpt-9-hermes"


def test_configured_default_model_codex(monkeypatch, tmp_path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-9-codex"\n')
    monkeypatch.setattr(mc.Path, "home", classmethod(lambda _cls: tmp_path))
    assert mc.configured_default_model("codex") == "gpt-9-codex"


def test_configured_default_model_codex_honors_literal_custom_home(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", "~/custom-codex")
    config_path = tmp_path / "~" / "custom-codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('model = "gpt-9-custom"\n')

    assert mc.configured_default_model("codex") == "gpt-9-custom"


def test_configured_default_model_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mc.Path, "home", classmethod(lambda _cls: tmp_path))
    assert mc.configured_default_model("hermes") is None
    assert mc.configured_default_model("codex") is None


def test_configured_default_model_malformed_never_raises(monkeypatch, tmp_path) -> None:
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text("model: [broken\n")
    monkeypatch.setattr(mc.Path, "home", classmethod(lambda _cls: tmp_path))
    assert mc.configured_default_model("hermes") is None


def test_configured_default_model_non_sentinel_backend() -> None:
    assert mc.configured_default_model("claude") is None
