"""Tests for MCP shell environment loading."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from ouroboros.cli.commands import mcp


@pytest.fixture(autouse=True)
def _restore_environ():
    """Restore os.environ after every test; _ensure_shell_env writes it directly."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_shell_env_loader_preserves_mcp_stdin(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv("OUROBOROS_TEST_ENV", raising=False)
    # Force a cache miss so the login shell actually runs.
    monkeypatch.setattr(mcp, "_SHELL_ENV_CACHE_FILE", tmp_path / "shell-env.json")

    initialize_message = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    fake_stdin = io.StringIO(initialize_message)
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        if kwargs.get("stdin") != subprocess.DEVNULL:
            sys.stdin.read()
        stdout = json.dumps({"PATH": os.environ["PATH"], "OUROBOROS_TEST_ENV": "loaded"})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)

    mcp._ensure_shell_env(timeout=1.25)

    assert fake_stdin.read() == initialize_message
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert os.environ["OUROBOROS_TEST_ENV"] == "loaded"


def test_shell_env_merge_is_whitelisted(monkeypatch, tmp_path) -> None:
    """Arbitrary login-shell vars must not leak into the server environment."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv("SOME_VENDOR_API_KEY", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(mcp, "_SHELL_ENV_CACHE_FILE", tmp_path / "shell-env.json")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps(
            {
                "PATH": "/usr/bin:/extra/bin",
                "SOME_VENDOR_API_KEY": "sk-secret",
                "PYTHONPATH": "/should/not/leak",
                "VIRTUAL_ENV": "/should/not/leak/either",
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)

    mcp._ensure_shell_env(timeout=1.25)

    assert os.environ["SOME_VENDOR_API_KEY"] == "sk-secret"
    assert "/extra/bin" in os.environ["PATH"]
    assert "PYTHONPATH" not in os.environ
    assert "VIRTUAL_ENV" not in os.environ


def test_shell_env_cache_skips_login_shell(monkeypatch, tmp_path) -> None:
    """A fresh cache must satisfy the load without spawning a login shell."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("CACHED_TEST_API_KEY", raising=False)
    cache_file = tmp_path / "shell-env.json"
    cache_file.write_text(json.dumps({"CACHED_TEST_API_KEY": "cached"}), encoding="utf-8")
    monkeypatch.setattr(mcp, "_SHELL_ENV_CACHE_FILE", cache_file)

    def fail_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("login shell must not run on a cache hit")

    monkeypatch.setattr(mcp.subprocess, "run", fail_run)

    mcp._ensure_shell_env(timeout=1.25)

    assert os.environ["CACHED_TEST_API_KEY"] == "cached"


def test_shell_env_cache_merges_other_provider_key_when_anthropic_is_present(
    monkeypatch,
    tmp_path,
) -> None:
    """Mixed-backend MCP workers must not skip OpenAI env recovery."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-present")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cache_file = tmp_path / "shell-env.json"
    cache_file.write_text(json.dumps({"OPENAI_API_KEY": "openai-cached"}), encoding="utf-8")
    monkeypatch.setattr(mcp, "_SHELL_ENV_CACHE_FILE", cache_file)

    def fail_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("login shell must not run on a cache hit")

    monkeypatch.setattr(mcp.subprocess, "run", fail_run)

    mcp._ensure_shell_env(timeout=1.25)

    assert os.environ["OPENAI_API_KEY"] == "openai-cached"


def test_shell_env_cache_written_with_whitelisted_subset(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    cache_file = tmp_path / "shell-env.json"
    monkeypatch.setattr(mcp, "_SHELL_ENV_CACHE_FILE", cache_file)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps({"PATH": "/usr/bin", "FOO_API_KEY": "x", "RANDOM_VAR": "y"})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)
    monkeypatch.delenv("FOO_API_KEY", raising=False)

    mcp._ensure_shell_env(timeout=1.25)

    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "FOO_API_KEY" in cached
    assert "RANDOM_VAR" not in cached


def test_shell_env_whitelist_admits_proxy_and_gateway_vars(monkeypatch, tmp_path) -> None:
    """Subscription-auth users behind proxies/gateways must keep network config."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for key in ("HTTPS_PROXY", "NO_PROXY", "ANTHROPIC_BASE_URL", "GH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(mcp, "_SHELL_ENV_CACHE_FILE", tmp_path / "shell-env.json")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps(
            {
                "HTTPS_PROXY": "http://proxy:8080",
                "NO_PROXY": "localhost",
                "ANTHROPIC_BASE_URL": "https://gw.example.com",
                "GH_TOKEN": "gho_x",
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)

    mcp._ensure_shell_env(timeout=1.25)

    assert os.environ["HTTPS_PROXY"] == "http://proxy:8080"
    assert os.environ["NO_PROXY"] == "localhost"
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://gw.example.com"
    assert os.environ["GH_TOKEN"] == "gho_x"


_LEAK_CHECK_BASELINE = {
    key: os.environ.get(key)
    for key in ("HTTPS_PROXY", "NO_PROXY", "ANTHROPIC_BASE_URL", "GH_TOKEN", "SOME_VENDOR_API_KEY")
}


def test_environ_mutations_do_not_survive_past_the_test() -> None:
    """Regression test for #1793: no earlier test's env vars survive teardown.

    Compares against the baseline captured at module import (before any test's
    autouse fixture ran) rather than asserting absence, so a legitimately
    ambient proxy/gateway/token value is not mistaken for a leak.
    """
    for key, baseline in _LEAK_CHECK_BASELINE.items():
        assert os.environ.get(key) == baseline
