"""Unit tests for the Copilot model discovery and name-mapping helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
)
from ouroboros.copilot import model_discovery as md

_FAKE_API_PAYLOAD: dict[str, Any] = {
    "data": [
        {
            "id": "claude-opus-4.8",
            "name": "Claude Opus 4.8",
            "vendor": "Anthropic",
            "capabilities": {"family": "claude-opus-4.8"},
        },
        {
            "id": "claude-opus-4.6",
            "name": "Claude Opus 4.6",
            "vendor": "Anthropic",
            "capabilities": {"family": "claude-opus-4.6"},
        },
        {
            "id": "claude-sonnet-4.5",
            "name": "Claude Sonnet 4.5",
            "vendor": "Anthropic",
            "capabilities": {"family": "claude-sonnet-4.5"},
        },
        {
            "id": "gpt-5.2",
            "name": "GPT-5.2",
            "vendor": "OpenAI",
            "capabilities": {"family": "gpt-5.2"},
        },
    ]
}

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "guide_path",
    (
        "docs/runtime-guides/copilot.md",
        "docs/runtime-guides/copilot.ko.md",
    ),
)
def test_runtime_guide_documents_current_anthropic_default_mappings(guide_path: str) -> None:
    guide = (_REPO_ROOT / guide_path).read_text(encoding="utf-8")

    assert f"`{DEFAULT_OPUS_MODEL}`" in guide
    assert "`claude-opus-4.8`" in guide
    assert f"`{DEFAULT_SONNET_MODEL}`" in guide
    assert "`claude-sonnet-4.6`" in guide
    assert "`claude-sonnet-4-5`" not in guide
    assert "`claude-sonnet-4.5`" not in guide


class _FakeUrlResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeUrlResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


class TestListCopilotModels:
    def setup_method(self) -> None:
        md.reset_cache()

    def teardown_method(self) -> None:
        md.reset_cache()

    def test_uses_api_response_when_token_resolves(self) -> None:
        with (
            patch.object(md, "_resolve_token", return_value="ghs_fake"),
            patch(
                "urllib.request.urlopen",
                return_value=_FakeUrlResponse(_FAKE_API_PAYLOAD),
            ),
        ):
            models = md.list_copilot_models(refresh=True)

        assert [m.id for m in models] == [
            "claude-opus-4.8",
            "claude-opus-4.6",
            "claude-sonnet-4.5",
            "gpt-5.2",
        ]
        assert md.used_fallback() is False

    def test_falls_back_when_no_token(self) -> None:
        with patch.object(md, "_resolve_token", return_value=None):
            models = md.list_copilot_models(refresh=True)

        assert models, "fallback list should be non-empty"
        assert any(m.id == "claude-opus-4.8" for m in models)
        assert any(m.id == "claude-opus-4.6" for m in models)
        assert md.used_fallback() is True

    def test_falls_back_on_http_error(self) -> None:
        import urllib.error

        with (
            patch.object(md, "_resolve_token", return_value="ghs_fake"),
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("boom"),
            ),
        ):
            models = md.list_copilot_models(refresh=True)

        assert models, "fallback list should be non-empty on URLError"
        assert md.used_fallback() is True

    def test_in_process_cache_avoids_second_fetch(self) -> None:
        call_count = {"n": 0}

        def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeUrlResponse:
            call_count["n"] += 1
            return _FakeUrlResponse(_FAKE_API_PAYLOAD)

        with (
            patch.object(md, "_resolve_token", return_value="ghs_fake"),
            patch(
                "urllib.request.urlopen",
                side_effect=fake_urlopen,
            ),
        ):
            md.list_copilot_models(refresh=True)
            md.list_copilot_models()
            md.list_copilot_models()

        assert call_count["n"] == 1


class TestMapToCopilotModel:
    def setup_method(self) -> None:
        md.reset_cache()

    def teardown_method(self) -> None:
        md.reset_cache()

    def test_dotted_passthrough_skips_discovery(self) -> None:
        # Patch _resolve_token to raise if called — proves no discovery happened.
        with patch.object(md, "_resolve_token", side_effect=AssertionError("called")):
            assert md.map_to_copilot_model("claude-sonnet-4.5") == "claude-sonnet-4.5"
            assert md.map_to_copilot_model("gpt-5.2") == "gpt-5.2"

    def test_default_marker_passes_through(self) -> None:
        with patch.object(md, "_resolve_token", side_effect=AssertionError("called")):
            assert md.map_to_copilot_model("default") == "default"

    def test_static_map_handles_anthropic_hyphen_form_when_available(self) -> None:
        pool = [
            md.CopilotModel(id="claude-opus-4.6", family="claude-opus-4.6"),
            md.CopilotModel(id="claude-sonnet-4.6", family="claude-sonnet-4.6"),
        ]
        with patch.object(md, "_resolve_token", side_effect=AssertionError("called")):
            assert md.map_to_copilot_model("claude-opus-4-6", available=pool) == "claude-opus-4.6"
            assert (
                md.map_to_copilot_model("claude-sonnet-4-6", available=pool) == "claude-sonnet-4.6"
            )
            assert (
                md.map_to_copilot_model(
                    "openrouter/anthropic/claude-opus-4-6",
                    available=pool,
                )
                == "claude-opus-4.6"
            )

    def test_current_default_maps_to_exact_catalog_id(self) -> None:
        pool = [md.CopilotModel(id="claude-opus-5", family="claude-opus-5")]

        assert md.map_to_copilot_model(DEFAULT_OPUS_MODEL, available=pool) == "claude-opus-5"
        assert (
            md.map_to_copilot_model(DEFAULT_CONSENSUS_OPUS_MODEL, available=pool) == "claude-opus-5"
        )

    def test_current_default_maps_from_bundled_catalog_when_discovery_fails(self) -> None:
        with patch.object(md, "_resolve_token", return_value=None):
            assert md.map_to_copilot_model(DEFAULT_OPUS_MODEL) == "claude-opus-5"
            assert md.used_fallback() is True

    def test_future_anthropic_version_uses_catalog_driven_candidate(self) -> None:
        pool = [md.CopilotModel(id="claude-sonnet-5.1", family="claude-sonnet-5.1")]

        assert md.map_to_copilot_model("claude-sonnet-5-1", available=pool) == "claude-sonnet-5.1"
        assert (
            md.map_to_copilot_model(
                "openrouter/anthropic/claude-sonnet-5-1",
                available=pool,
            )
            == "claude-sonnet-5.1"
        )

    def test_candidate_must_be_available_in_catalog(self) -> None:
        pool = [md.CopilotModel(id="claude-opus-4.7", family="claude-opus-4.7")]

        assert md.map_to_copilot_model(DEFAULT_OPUS_MODEL, available=pool) == DEFAULT_OPUS_MODEL
        assert (
            md.map_to_copilot_model(DEFAULT_CONSENSUS_OPUS_MODEL, available=pool)
            == DEFAULT_CONSENSUS_OPUS_MODEL
        )

    def test_openrouter_prefix_is_removed_only_for_an_exact_catalog_id(self) -> None:
        model = "openrouter/anthropic/claude-fable-5"
        exact_pool = [md.CopilotModel(id="claude-fable-5", family="claude-fable-5")]
        family_only_pool = [md.CopilotModel(id="claude-fable-5-fast", family="claude-fable-5")]

        assert md.map_to_copilot_model(model, available=exact_pool) == "claude-fable-5"
        assert md.map_to_copilot_model(model, available=family_only_pool) == model

    def test_static_alias_is_catalog_gated_by_exact_id(self) -> None:
        alias = "legacy-opus"
        target = "claude-opus-4.6"
        exact_pool = [md.CopilotModel(id=target, family=target)]
        family_only_pool = [md.CopilotModel(id="claude-opus-fast", family=target)]

        with patch.dict(md._STATIC_NAME_MAP, {alias: target}):
            assert md.map_to_copilot_model(alias, available=exact_pool) == target
            assert md.map_to_copilot_model(alias, available=family_only_pool) == alias

    def test_fallback_never_replaces_every_hyphen(self) -> None:
        malformed_old_candidate = "claude.opus.beta.4.8"
        pool = [md.CopilotModel(id=malformed_old_candidate, family=malformed_old_candidate)]

        assert (
            md.map_to_copilot_model("claude-opus-beta-4-8", available=pool)
            == "claude-opus-beta-4-8"
        )

    def test_non_anthropic_unknown_id_is_preserved_even_if_dotted_shape_exists(self) -> None:
        pool = [md.CopilotModel(id="acme-future-9.0", family="acme-future-9.0")]

        assert md.map_to_copilot_model("acme-future-9-0", available=pool) == "acme-future-9-0"

    def test_unknown_hyphen_id_returns_unchanged(self) -> None:
        with patch.object(md, "_resolve_token", return_value=None):
            # No static map entry, no dotted equivalent in the fallback list.
            assert md.map_to_copilot_model("acme-future-9-0") == "acme-future-9-0"

    def test_explicit_available_overrides_discovery(self) -> None:
        custom_pool = [
            md.CopilotModel(id="claude-opus-4.6", family="claude-opus-4.6"),
        ]
        # Even with a side_effect that would fail, explicit pool wins.
        with patch.object(md, "_resolve_token", side_effect=AssertionError("called")):
            assert (
                md.map_to_copilot_model("claude-opus-4-6", available=custom_pool)
                == "claude-opus-4.6"
            )


class TestResolveToken:
    """Verify the env-then-gh fallback chain in :func:`_resolve_token`.

    The chain is security-sensitive because the resolved token is sent as a
    bearer credential to ``api.githubcopilot.com``. Misordering or accidental
    leakage between sources would surprise users.
    """

    _ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_TOKEN")

    def setup_method(self) -> None:
        self._saved_env = {key: os.environ.pop(key, None) for key in self._ENV_KEYS}

    def teardown_method(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_gh_token_env_wins_over_github_token(self) -> None:
        os.environ["GH_TOKEN"] = "gh_value"
        os.environ["GITHUB_TOKEN"] = "github_value"
        os.environ["COPILOT_TOKEN"] = "copilot_value"
        with patch("shutil.which", side_effect=AssertionError("gh not consulted")):
            assert md._resolve_token() == "gh_value"

    def test_github_token_used_when_gh_token_missing(self) -> None:
        os.environ["GITHUB_TOKEN"] = "github_value"
        os.environ["COPILOT_TOKEN"] = "copilot_value"
        with patch("shutil.which", side_effect=AssertionError("gh not consulted")):
            assert md._resolve_token() == "github_value"

    def test_copilot_token_used_when_gh_and_github_missing(self) -> None:
        os.environ["COPILOT_TOKEN"] = "copilot_value"
        with patch("shutil.which", side_effect=AssertionError("gh not consulted")):
            assert md._resolve_token() == "copilot_value"

    def test_falls_back_to_gh_cli_when_env_empty(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh", "auth", "token"],
            returncode=0,
            stdout="ghs_from_cli\n",
            stderr="",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", return_value=completed) as run,
        ):
            assert md._resolve_token() == "ghs_from_cli"
            run.assert_called_once()

    def test_returns_none_when_gh_missing_from_path(self) -> None:
        with patch("shutil.which", return_value=None):
            assert md._resolve_token() is None

    def test_returns_none_when_gh_auth_token_returns_nonzero(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh", "auth", "token"],
            returncode=1,
            stdout="",
            stderr="not logged in",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", return_value=completed),
        ):
            assert md._resolve_token() is None

    def test_returns_none_when_gh_auth_token_returns_empty_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh", "auth", "token"],
            returncode=0,
            stdout="   \n",
            stderr="",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", return_value=completed),
        ):
            assert md._resolve_token() is None

    def test_returns_none_when_gh_subprocess_raises(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=5)),
        ):
            assert md._resolve_token() is None
