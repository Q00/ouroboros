"""Tests for stdlib .env loading helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ouroboros.config.loader import _load_env_file


@pytest.fixture(autouse=True)
def _restore_environ():
    """`_load_env_file` writes os.environ directly, bypassing monkeypatch.

    Without an explicit restore these tests leak keys (notably the
    runtime/backend selectors) into the session and break every later
    test that resolves a backend.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_load_env_file_sets_missing_values(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("export FIRST=value\nSECOND='two words'\nTHIRD=three # trailing comment\n")

    monkeypatch.delenv("FIRST", raising=False)
    monkeypatch.delenv("SECOND", raising=False)
    monkeypatch.delenv("THIRD", raising=False)

    _load_env_file(env_file)

    assert os.environ["FIRST"] == "value"
    assert os.environ["SECOND"] == "two words"
    assert os.environ["THIRD"] == "three"


def test_load_env_file_does_not_override_existing_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FIRST=from-file\n")
    monkeypatch.setenv("FIRST", "existing")

    _load_env_file(env_file)

    assert os.environ["FIRST"] == "existing"


def test_load_env_file_ignores_directory_path(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.mkdir()

    _load_env_file(env_path)


def test_load_env_file_skips_template_placeholders(tmp_path: Path, monkeypatch) -> None:
    """Template placeholders should not block later env values from loading."""
    repo_env = tmp_path / "repo.env"
    home_env = tmp_path / "home.env"

    repo_env.write_text("OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY")
    home_env.write_text("OPENROUTER_API_KEY=real-key")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    _load_env_file(repo_env)
    _load_env_file(home_env)

    assert os.environ["OPENROUTER_API_KEY"] == "real-key"


_DENYLISTED_KEYS = (
    # Search PATH used by shutil.which()/bare executable spawning.
    "PATH",
    "OUROBOROS_CLI_PATH",
    "OUROBOROS_CODEX_CLI_PATH",
    "OUROBOROS_COPILOT_CLI_PATH",
    "OUROBOROS_KIRO_CLI_PATH",
    "OUROBOROS_OPENCODE_CLI_PATH",
    "OUROBOROS_HERMES_CLI_PATH",
    "OUROBOROS_GOOSE_CLI_PATH",
    "OUROBOROS_GEMINI_CLI_PATH",
    # Bare provider alias (no OUROBOROS_ prefix) honored + executed by
    # opencode_config._configured_opencode_cli_path.
    "OPENCODE_CLI_PATH",
    # Runtime/backend selectors route to an adapter whose CLI then
    # resolves via a weak PATH lookup — also an RCE sink.
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_RUNTIME",
    "OUROBOROS_LLM_BACKEND",
    # Permission-mode overrides — must not silently disable the
    # user's approval gate from an untrusted repo.
    "OUROBOROS_AGENT_PERMISSION_MODE",
    "OUROBOROS_LLM_PERMISSION_MODE",
    "OUROBOROS_OPENCODE_PERMISSION_MODE",
)


def test_untrusted_env_cannot_set_bare_opencode_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: opencode_config reads bare OPENCODE_CLI_PATH and runs it."""
    env_file = tmp_path / ".env"
    env_file.write_text("OPENCODE_CLI_PATH=./evil\n")
    monkeypatch.delenv("OPENCODE_CLI_PATH", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OPENCODE_CLI_PATH" not in os.environ


def test_untrusted_env_cannot_disable_approval_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: a cloned repo must not force bypassPermissions."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_AGENT_PERMISSION_MODE=bypassPermissions\n")
    monkeypatch.delenv("OUROBOROS_AGENT_PERMISSION_MODE", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OUROBOROS_AGENT_PERMISSION_MODE" not in os.environ


def test_untrusted_env_cannot_set_mixed_case_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Mixed-case PATH variants must not bypass the denylist on Windows."""
    env_file = tmp_path / ".env"
    env_file.write_text("Path=./malicious-bin\n")
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("Path", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "PATH" not in os.environ
    assert "Path" not in os.environ


@pytest.mark.parametrize("key", _DENYLISTED_KEYS)
def test_untrusted_env_cannot_redirect_executable(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """A cloned-repo .env must not set executable-path vars (RCE guard)."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=./malicious_script.sh\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


@pytest.mark.parametrize("key", _DENYLISTED_KEYS)
def test_trusted_env_may_set_executable_path(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """The home .env stays trusted and may set a custom CLI path."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=/usr/local/bin/claude\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=True)

    assert os.environ[key] == "/usr/local/bin/claude"


def test_untrusted_env_does_not_override_process_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Project .env must not replace an existing process PATH."""
    process_path = "/usr/bin:/bin"
    env_file = tmp_path / ".env"
    env_file.write_text(f"PATH=./malicious-bin:{process_path}\n")
    monkeypatch.setenv("PATH", process_path)

    _load_env_file(env_file, trusted=False)

    assert os.environ["PATH"] == process_path


def test_trusted_env_does_not_override_process_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Trusted .env keeps normal process-environment precedence for PATH."""
    process_path = "/usr/bin:/bin"
    env_file = tmp_path / ".env"
    env_file.write_text(f"PATH=/trusted/bin:{process_path}\n")
    monkeypatch.setenv("PATH", process_path)

    _load_env_file(env_file, trusted=True)

    assert os.environ["PATH"] == process_path


def test_untrusted_env_still_loads_non_sensitive_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Denylisting must be surgical: ordinary keys still load untrusted."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_CLI_PATH=./evil.sh\nOPENROUTER_API_KEY=key-123\n")
    monkeypatch.delenv("OUROBOROS_CLI_PATH", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OUROBOROS_CLI_PATH" not in os.environ
    assert os.environ["OPENROUTER_API_KEY"] == "key-123"


def test_load_env_file_defaults_to_untrusted_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Default trusted=False: callers are safe-by-default (fail-closed)."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_CLI_PATH=./evil.sh\n")
    monkeypatch.delenv("OUROBOROS_CLI_PATH", raising=False)

    _load_env_file(env_file)  # no trusted kwarg → must be treated as untrusted

    assert "OUROBOROS_CLI_PATH" not in os.environ
