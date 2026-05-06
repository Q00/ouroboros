"""Unit tests for the Copilot model discovery and name-mapping helpers."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any
from unittest.mock import patch

from ouroboros.copilot import model_discovery as md

_FAKE_API_PAYLOAD: dict[str, Any] = {
    "data": [
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
            "claude-opus-4.6",
            "claude-sonnet-4.5",
            "gpt-5.2",
        ]
        assert md.used_fallback() is False

    def test_falls_back_when_no_token(self) -> None:
        with patch.object(md, "_resolve_token", return_value=None):
            models = md.list_copilot_models(refresh=True)

        assert models, "fallback list should be non-empty"
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

    def test_static_map_handles_anthropic_hyphen_form(self) -> None:
        with patch.object(md, "_resolve_token", side_effect=AssertionError("called")):
            assert md.map_to_copilot_model("claude-opus-4-6") == "claude-opus-4.6"
            assert md.map_to_copilot_model("claude-sonnet-4-6") == "claude-sonnet-4.6"
            assert (
                md.map_to_copilot_model("openrouter/anthropic/claude-opus-4-6") == "claude-opus-4.6"
            )

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
