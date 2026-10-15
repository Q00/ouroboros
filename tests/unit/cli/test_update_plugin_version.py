"""Update-flow regressions for installed Codex plugin version reporting."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands import update
from ouroboros.core.errors import ConfigError

runner = CliRunner()


def _plain(output: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)


def _listing(version: str) -> str:
    return json.dumps(
        {
            "installed": [
                {
                    "pluginId": "ouroboros@ouroboros",
                    "name": "ouroboros",
                    "marketplaceName": "ouroboros",
                    "version": version,
                    "installed": True,
                    "enabled": True,
                }
            ],
            "available": [],
        }
    )


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Keep the real CLI flow while faking network and external executables."""
    codex_home = tmp_path / "Codex home"
    codex_home.mkdir()
    codex_config = codex_home / "config.toml"
    codex_config.write_bytes(b'[marketplaces.ouroboros]\r\nref = "v0.52.0"\r\n')
    config_dir = tmp_path / "ouroboros"
    config_dir.mkdir()
    runtime_config = config_dir / "config.yaml"
    runtime_config.write_bytes(b"orchestrator:\n  runtime_backend: codex\n")
    original_configs = {path: path.read_bytes() for path in (codex_config, runtime_config)}

    state = SimpleNamespace(
        current="0.55.6",
        latest="0.55.6",
        refreshed="0.55.6",
        stdout=_listing("0.52.0"),
        query_returncode=0,
        query_error=None,
        setup_returncode=0,
        calls=[],
        codex=str(tmp_path / "selected Codex" / "codex.exe"),
        claude=str(tmp_path / "selected Claude" / "claude.exe"),
        opencode=str(tmp_path / "selected OpenCode" / "opencode.exe"),
    )
    state.query = [state.codex, "plugin", "list", "--marketplace", "ouroboros", "--json"]
    environment = tmp_path / "tools" / "ouroboros-ai"
    identity = update.InstallationIdentity(
        manager="uv",
        tool_name="ouroboros-ai",
        environment=environment,
        profile="ouroboros-ai[mcp]",
        console_path=environment / "Scripts" / "ouroboros.exe",
        manager_binary=str(tmp_path / "uv.exe"),
        manager_home=environment.parent,
    )
    config = SimpleNamespace(
        orchestrator=SimpleNamespace(
            runtime_backend="codex",
            opencode_mode=None,
            codex_cli_path=state.codex,
            cli_path=state.claude,
            opencode_cli_path=state.opencode,
        )
    )
    state.config = config
    for env_key, _, _ in update._RUNTIME_CLI_IDENTITIES.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.delenv("OUROBOROS_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("OUROBOROS_RUNTIME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(update, "get_config_dir", lambda: config_dir)
    monkeypatch.setattr(update, "load_config", lambda *_args, **_kwargs: config)
    executables = {
        "codex": state.codex,
        "claude": state.claude,
        "opencode": state.opencode,
        state.codex: state.codex,
        state.claude: state.claude,
        state.opencode: state.opencode,
    }
    monkeypatch.setattr(update.shutil, "which", lambda command: executables.get(command))
    monkeypatch.setattr(update, "_latest_pypi_version", lambda **_kwargs: state.latest)
    monkeypatch.setattr(update, "_detect_installation_identity", lambda: identity)
    monkeypatch.setattr(update, "_installed_version", lambda _identity: state.refreshed)
    monkeypatch.setattr(update, "configure_omp_tool_call_timeout", lambda **_kwargs: True)

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        state.calls.append((command, kwargs))
        if command == state.query:
            if state.query_error is not None:
                raise state.query_error
            return subprocess.CompletedProcess(
                command, state.query_returncode, state.stdout.encode(), b""
            )
        if "setup" in command:
            return subprocess.CompletedProcess(command, state.setup_returncode, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def invoke(*args: str):
        monkeypatch.setattr(update, "__version__", state.current)
        return runner.invoke(update.app, list(args))

    state.invoke = invoke
    yield state
    for path, original in original_configs.items():
        assert path.read_bytes() == original, f"Update reporting changed {path.name}"


@pytest.mark.parametrize("runtime", ["codex", "codex_cli", "auto"])
def test_current_package_reports_installed_plugin_drift(
    flow: SimpleNamespace, runtime: str
) -> None:
    result = flow.invoke("--runtime", runtime)

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Ouroboros package is up to date" in output
    assert "Codex plugin" in output
    assert "0.52.0" in output and "0.55.6" in output
    assert "differs" in output
    assert "unchanged" in output
    assert [command for command, _ in flow.calls] == [flow.query]


@pytest.mark.parametrize("version", ["0.55.6", "0.55.7"])
def test_current_package_reports_matching_and_newer_plugins(
    flow: SimpleNamespace, version: str
) -> None:
    flow.stdout = _listing(version)

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin" in output and version in output
    assert ("differs" in output) == (version != "0.55.6")
    assert [command for command, _ in flow.calls] == [flow.query]


@pytest.mark.parametrize("packaging_available", [True, False])
@pytest.mark.parametrize(
    ("plugin", "package", "matches"),
    [
        ("0.55.6.post1", "0.55.6", False),
        ("0.55.6+local.1", "0.55.6", False),
        ("0.55.6rc1.dev1", "0.55.6rc1", False),
        ("0.55.6.0", "0.55.6", True),
        ("v0.55.6", "0.55.6.0", True),
        ("0.55.6+LOCAL-01", "0.55.6+local.1", True),
        ("0.55.6+local_1", "0.55.6+local.2", False),
        ("0.55.6rc01.post01.dev01", "0.55.6rc1.post1.dev1", True),
    ],
)
def test_plugin_version_identity_is_independent_of_packaging(
    flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    packaging_available: bool,
    plugin: str,
    package: str,
    matches: bool,
) -> None:
    # Pytest supplies packaging in the dev environment; a base install need not.
    # Exercise the real CLI comparison with both import environments.
    if not packaging_available:
        monkeypatch.setitem(sys.modules, "packaging", None)
        monkeypatch.setitem(sys.modules, "packaging.version", None)
    else:
        import packaging.version  # noqa: F401

    flow.current = flow.latest = package
    flow.stdout = _listing(plugin)

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert ("matches package" in output) is matches
    assert ("differs" in output) is not matches
    assert [command for command, _ in flow.calls] == [flow.query]


def test_unsupported_package_version_does_not_infer_plugin_drift(flow: SimpleNamespace) -> None:
    flow.current = flow.latest = "1!0.55.6"
    flow.stdout = _listing("0.55.6")

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin: v0.55.6" in output
    assert "unknown (unsupported version format)" in output
    assert "matches package" not in output and "differs" not in output
    assert [command for command, _ in flow.calls] == [flow.query]


def test_update_compares_refreshed_package_after_setup(flow: SimpleNamespace) -> None:
    flow.current = "0.54.4"
    flow.refreshed = "0.55.7"
    flow.stdout = _listing("0.55.7")

    result = flow.invoke("--runtime", "codex", "--yes")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Package updated to v0.55.7" in output
    assert "Codex plugin" in output and "0.55.7" in output
    assert "differs" not in output
    commands = [command for command, _ in flow.calls]
    assert commands[-1] == flow.query
    setup_index = next(index for index, command in enumerate(commands) if "setup" in command)
    assert setup_index < commands.index(flow.query)


def test_all_runtimes_inspects_codex_after_host_refresh(flow: SimpleNamespace) -> None:
    flow.current = "0.54.4"

    result = flow.invoke("--runtime", "all", "--yes")

    assert result.exit_code == 0, result.output
    commands = [command for command, _ in flow.calls]
    assert commands[-1] == flow.query
    assert [flow.codex, "plugin", "marketplace", "upgrade", "ouroboros"] in commands
    assert any(command[1:] == ["setup", "refresh"] for command in commands)
    assert "differs" in _plain(result.output)


def test_check_with_available_update_inspects_without_upgrading(flow: SimpleNamespace) -> None:
    flow.current = "0.54.4"

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin" in output and "0.52.0" in output and "0.54.4" in output
    assert "differs" in output
    assert [command for command, _ in flow.calls] == [flow.query]


def test_available_candidate_does_not_count_as_installed(flow: SimpleNamespace) -> None:
    flow.stdout = json.dumps(
        {"installed": [], "available": json.loads(_listing("0.55.6"))["installed"]}
    )

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin" in output and "not installed" in output
    assert "unknown" not in output and "differs" not in output


@pytest.mark.parametrize("stdout", ["null", "not-json", '{"available": []}'])
def test_unreadable_plugin_listing_reports_unknown(flow: SimpleNamespace, stdout: str) -> None:
    flow.stdout = stdout

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin" in output and "unknown" in output
    assert "not installed" not in output


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "missing"])
def test_query_failure_is_advisory_unknown(flow: SimpleNamespace, failure: str) -> None:
    if failure == "nonzero":
        flow.query_returncode = 2
    elif failure == "timeout":
        flow.query_error = subprocess.TimeoutExpired(flow.query, 15)
    else:
        flow.query_error = FileNotFoundError("selected Codex disappeared")

    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin" in output and "unknown" in output
    assert "not installed" not in output


@pytest.mark.parametrize(
    ("args", "current", "setup_returncode"),
    [
        pytest.param(["--runtime", "codex"], "0.55.6", 0, id="current"),
        pytest.param(["--runtime", "codex", "--check"], "0.55.6", 0, id="check-current"),
        pytest.param(["--runtime", "codex", "--check"], "0.54.4", 0, id="check-upgrade"),
        pytest.param(["--runtime", "codex", "--yes"], "0.54.4", 0, id="upgraded"),
        pytest.param(["--runtime", "codex", "--yes"], "0.54.4", 7, id="partial-upgrade"),
        pytest.param(["--runtime", "all"], "0.55.6", 0, id="all-current"),
        pytest.param(["--runtime", "all", "--yes"], "0.54.4", 0, id="all-upgraded"),
    ],
)
def test_deep_plugin_json_remains_advisory_in_update_flows(
    flow: SimpleNamespace, args: list[str], current: str, setup_returncode: int
) -> None:
    flow.current = current
    flow.setup_returncode = setup_returncode
    flow.stdout = '{"installed": [], "private": ' + "[" * 10_000 + "0" + "]" * 10_000 + "}"

    result = flow.invoke(*args)

    assert result.exit_code == (1 if setup_returncode else 0), result.output
    if setup_returncode:
        assert isinstance(result.exception, SystemExit)
        assert "partially updated" in _plain(result.output)
    else:
        assert result.exception is None
    output = _plain(result.output)
    assert "Codex plugin version: unknown (Codex returned invalid plugin JSON)" in output
    assert "not installed" not in output and "private" not in output
    assert [command for command, _ in flow.calls][-1] == flow.query
    if current == flow.latest or "--check" in args:
        assert [command for command, _ in flow.calls] == [flow.query]


@pytest.mark.parametrize("runtime", ["none", "claude", "opencode"])
def test_other_selected_runtime_does_not_inspect_codex(flow: SimpleNamespace, runtime: str) -> None:
    flow.current = "0.54.4"

    result = flow.invoke("--runtime", runtime, "--yes")

    assert result.exit_code == 0, result.output
    assert flow.query not in [command for command, _ in flow.calls]


@pytest.mark.parametrize("current", ["0.54.4", "0.55.6"])
def test_dry_run_never_spawns_plugin_inspection(flow: SimpleNamespace, current: str) -> None:
    flow.current = current

    result = flow.invoke("--runtime", "codex", "--dry-run")

    assert result.exit_code == 0, result.output
    assert flow.calls == []


def test_unknown_plugin_does_not_mask_partial_update_failure(flow: SimpleNamespace) -> None:
    flow.current = "0.54.4"
    flow.setup_returncode = 1
    flow.stdout = "not-json"

    result = flow.invoke("--runtime", "codex", "--yes")

    assert result.exit_code == 1, result.output
    output = _plain(result.output)
    assert "partially updated" in output
    assert "Codex plugin" in output and "unknown" in output
    assert [command for command, _ in flow.calls][-1] == flow.query


def test_current_package_checks_all_without_refresh(flow: SimpleNamespace) -> None:
    result = flow.invoke("--runtime", "all")

    assert result.exit_code == 0, result.output
    assert "differs" in _plain(result.output)
    assert [command for command, _ in flow.calls] == [flow.query]


def test_invalid_runtime_config_reports_unknown_without_failing_check(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_config(*_args: object, **_kwargs: object):
        raise ConfigError("invalid configuration")

    monkeypatch.setattr(update, "load_config", invalid_config)
    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    assert "Codex plugin version: unknown" in _plain(result.output)
    assert flow.calls == []


def test_missing_codex_executable_reports_unknown(
    flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow.config.orchestrator.codex_cli_path = None
    monkeypatch.setattr(update.shutil, "which", lambda _command: None)
    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    assert "Codex plugin version: unknown" in _plain(result.output)
    assert flow.calls == []


def test_editable_package_does_not_infer_drift(flow: SimpleNamespace) -> None:
    flow.current = "0.0.0"
    result = flow.invoke("--runtime", "codex", "--check")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Codex plugin: v0.52.0" in output
    assert "comparison skipped" in output and "differs" not in output


def test_auto_with_other_backend_does_not_query_codex(flow: SimpleNamespace) -> None:
    flow.config.orchestrator.runtime_backend = "claude"
    result = flow.invoke("--check")

    assert result.exit_code == 0, result.output
    assert "Codex plugin" not in _plain(result.output)
    assert flow.calls == []
