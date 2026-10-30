"""Real-child proof of the Megacode executable selection boundary."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from ouroboros.cli.commands.megacode_setup import (
    detect_megacode_runtime,
    resolve_harness_config_dir,
)
from ouroboros.orchestrator.megacode_runtime import MegacodeRuntime
from ouroboros.orchestrator.runtime_factory import create_agent_runtime
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.factory import create_llm_adapter
from ouroboros.providers.megacode_llm_adapter import MegacodeLLMAdapter


def _executable(path: Path, marker: Path, config_dir: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('invoked')\n"
        "if sys.argv[1:] == ['paths']:\n"
        f"    print('CONFIG ' + {str(config_dir)!r} + '  (default)')\n"
        "else:\n"
        "    sys.stdin.read()\n"
        "    print(json.dumps({'type': 'result', 'outcome': 'completed', "
        "'is_error': False, 'result': 'trusted answer'}))\n"
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def executable_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".ouroboros").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "project-invoked"
    trusted_marker = tmp_path / "trusted-invoked"
    _executable(project / "bin/harness", marker, tmp_path / "bad-config")
    trusted = _executable(home / "bin/harness", trusted_marker, tmp_path / "good-config")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OUROBOROS_MEGACODE_CLI_PATH", raising=False)
    monkeypatch.setenv("PATH", ":.:bin")
    monkeypatch.chdir(project)
    return home, project, marker, trusted, trusted_marker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    ["setup", "setup-direct", "worker", "provider", "worker-direct", "provider-direct", "catalog"],
)
@pytest.mark.parametrize("source", ["config", "env", "explicit"])
async def test_relative_path_never_invokes_project_executable(
    executable_boundary, monkeypatch, entrypoint, source
):
    home, project, marker, _, _ = executable_boundary
    kwargs = {}
    if source == "config":
        (home / ".ouroboros/config.yaml").write_text(
            "orchestrator:\n  megacode_cli_path: bin/harness\n"
        )
    elif source == "env":
        monkeypatch.setenv("OUROBOROS_MEGACODE_CLI_PATH", "bin/harness")
    else:
        kwargs["cli_path"] = "bin/harness"
    try:
        if entrypoint == "setup":
            selected = detect_megacode_runtime()
            if selected:
                resolve_harness_config_dir(selected)
        elif entrypoint == "setup-direct":
            resolve_harness_config_dir("bin/harness")
        elif entrypoint == "catalog":
            from ouroboros.backends.model_catalog import detect_backend_cli

            selected = detect_backend_cli("megacode")
            if selected:
                resolve_harness_config_dir(selected)
        elif entrypoint.startswith("worker"):
            runtime = (
                MegacodeRuntime(cwd=project, **kwargs)
                if entrypoint.endswith("direct")
                else create_agent_runtime(backend="megacode", cwd=project, **kwargs)
            )
            _ = [message async for message in runtime.execute_task("probe")]
        else:
            adapter = (
                MegacodeLLMAdapter(cwd=project, **kwargs)
                if entrypoint.endswith("direct")
                else create_llm_adapter(backend="megacode", cwd=project, **kwargs)
            )
            await adapter.complete(
                [Message(role=MessageRole.USER, content="probe")], CompletionConfig(model="default")
            )
    except (FileNotFoundError, ValueError):
        pass
    assert not marker.exists(), f"{source}/{entrypoint} executed the project's bin/harness"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    ["setup", "setup-direct", "worker", "provider", "worker-direct", "provider-direct", "catalog"],
)
@pytest.mark.parametrize("source", ["config", "env", "explicit", "path"])
async def test_trusted_executable_works_at_every_entrypoint(
    executable_boundary, monkeypatch, entrypoint, source
):
    home, project, marker, trusted, trusted_marker = executable_boundary
    kwargs = {}
    if source == "config":
        (home / ".ouroboros/config.yaml").write_text(
            "orchestrator:\n  megacode_cli_path: ~/bin/harness\n"
        )
    elif source == "env":
        monkeypatch.setenv("OUROBOROS_MEGACODE_CLI_PATH", str(trusted))
    elif source == "explicit" and entrypoint in {
        "worker",
        "provider",
        "worker-direct",
        "provider-direct",
    }:
        kwargs["cli_path"] = trusted
    else:
        monkeypatch.setenv("PATH", f":.:bin:{trusted.parent}")
    if entrypoint == "setup":
        selected = detect_megacode_runtime()
        assert selected == str(trusted)
        assert resolve_harness_config_dir(selected) == home.parent / "good-config"
    elif entrypoint == "setup-direct":
        assert resolve_harness_config_dir(str(trusted)) == home.parent / "good-config"
    elif entrypoint == "catalog":
        from ouroboros.backends.model_catalog import detect_backend_cli

        selected = detect_backend_cli("megacode")
        assert selected == str(trusted)
        assert resolve_harness_config_dir(selected) == home.parent / "good-config"
    elif entrypoint.startswith("worker"):
        runtime = (
            MegacodeRuntime(cwd=project, **kwargs)
            if entrypoint.endswith("direct")
            else create_agent_runtime(backend="megacode", cwd=project, **kwargs)
        )
        messages = [message async for message in runtime.execute_task("probe")]
        assert messages[-1].content == "trusted answer"
        assert messages[-1].data["subtype"] == "success"
    else:
        adapter = (
            MegacodeLLMAdapter(cwd=project, **kwargs)
            if entrypoint.endswith("direct")
            else create_llm_adapter(backend="megacode", cwd=project, **kwargs)
        )
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="probe")], CompletionConfig(model="default")
        )
        assert result.is_ok
        assert result.value.content == "trusted answer"
    assert trusted_marker.exists()
    assert not marker.exists()


@pytest.mark.parametrize(
    "candidate", ["bin/harness", "./harness", "../bin/harness", "", ".", "missing"]
)
def test_resolver_rejects_unsupported_candidates(executable_boundary, candidate):
    from ouroboros.config import resolve_megacode_cli_path

    assert resolve_megacode_cli_path(candidate) is None


def test_bare_name_is_pinned_before_cwd_changes(executable_boundary, monkeypatch):
    from ouroboros.config import resolve_megacode_cli_path

    _, project, marker, trusted, _ = executable_boundary
    monkeypatch.setenv("PATH", f":.:bin:{trusted.parent}")
    resolved = resolve_megacode_cli_path("harness")
    assert resolved == str(trusted)
    monkeypatch.chdir(project / "bin")
    assert resolve_harness_config_dir(resolved) == trusted.parents[2] / "good-config"
    assert not marker.exists()


def test_invalid_explicit_choice_does_not_fall_back(executable_boundary, monkeypatch):
    from ouroboros.config import resolve_megacode_cli_path

    _, _, _, trusted, _ = executable_boundary
    monkeypatch.setenv("OUROBOROS_MEGACODE_CLI_PATH", str(trusted))
    monkeypatch.setenv("PATH", str(trusted.parent))
    assert resolve_megacode_cli_path("bin/harness") is None
    assert resolve_megacode_cli_path() == str(trusted)


def test_env_config_default_precedence(executable_boundary, monkeypatch):
    from ouroboros.config import resolve_megacode_cli_path

    home, _, _, trusted, _ = executable_boundary
    configured = _executable(home / "configured", home / "config-marker", home / "config-root")
    override = _executable(home / "override", home / "env-marker", home / "env-root")
    monkeypatch.setenv("PATH", str(trusted.parent))
    (home / ".ouroboros/config.yaml").write_text(
        f"orchestrator:\n  megacode_cli_path: {configured}\n"
    )
    monkeypatch.setenv("OUROBOROS_MEGACODE_CLI_PATH", str(override))
    assert resolve_megacode_cli_path() == str(override)
    assert resolve_megacode_cli_path(trusted) == str(trusted)
    monkeypatch.delenv("OUROBOROS_MEGACODE_CLI_PATH")
    assert resolve_megacode_cli_path() == str(configured)
    (home / ".ouroboros/config.yaml").write_text("{}\n")
    assert resolve_megacode_cli_path() == str(trusted)


def test_setup_applies_only_with_supported_executable(executable_boundary):
    from ouroboros.cli.commands.megacode_setup import setup_megacode

    home, _, marker, trusted, trusted_marker = executable_boundary
    skills = home / "packaged/seed"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: seed\ndescription: fixture\n---\n# Seed\n")
    entry = {"command": "uvx", "args": ["ouroboros-ai", "mcp", "serve"]}
    assert not setup_megacode("bin/harness", mcp_entry=entry, skills_dir=skills.parent)
    assert not marker.exists()
    assert not (home.parent / "bad-config").exists()
    assert setup_megacode(str(trusted), mcp_entry=entry, skills_dir=skills.parent)
    assert trusted_marker.exists()
    assert (home.parent / "good-config/config.toml").is_file()
    assert (home.parent / "good-config/ouroboros/skills/seed/SKILL.md").is_file()


def test_absolute_candidate_must_be_runnable(executable_boundary):
    from ouroboros.config import resolve_megacode_cli_path

    home, _, _, _, _ = executable_boundary
    nonexecutable = home / "not-executable"
    nonexecutable.write_text("data")
    nonexecutable.chmod(0o600)
    assert resolve_megacode_cli_path(nonexecutable) is None
    assert resolve_megacode_cli_path(home) is None
    assert resolve_megacode_cli_path(home / "absent") is None


@pytest.mark.parametrize("candidate", ["C:harness", "C:", r"C:bin\harness"])
def test_windows_drive_relative_semantics_fail_before_executable_lookup(monkeypatch, candidate):
    """Exercise pathlib's Windows semantics on POSIX; no Windows execution claim."""
    from pathlib import PureWindowsPath

    from ouroboros.config import _megacode_cli

    class WindowsCandidate(PureWindowsPath):
        def expanduser(self):
            return self

    def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("A drive-relative candidate reached executable lookup")

    assert WindowsCandidate(candidate).drive == "C:"
    assert not WindowsCandidate(candidate).is_absolute()
    monkeypatch.setattr(_megacode_cli, "Path", WindowsCandidate)
    monkeypatch.setattr(_megacode_cli.shutil, "which", unexpected_lookup)
    assert _megacode_cli.resolve_megacode_cli_path(candidate) is None


@pytest.mark.parametrize("source", ["env", "config"])
def test_invalid_configured_choice_does_not_fall_back(executable_boundary, monkeypatch, source):
    from ouroboros.config import resolve_megacode_cli_path

    home, _, _, trusted, _ = executable_boundary
    monkeypatch.setenv("PATH", str(trusted.parent))
    if source == "env":
        monkeypatch.setenv("OUROBOROS_MEGACODE_CLI_PATH", "bin/harness")
        (home / ".ouroboros/config.yaml").write_text(
            f"orchestrator:\n  megacode_cli_path: {trusted}\n"
        )
    else:
        (home / ".ouroboros/config.yaml").write_text(
            "orchestrator:\n  megacode_cli_path: bin/harness\n"
        )
    assert resolve_megacode_cli_path() is None
