"""Public configuration errors stay safe before schema loading and at consumers."""

import inspect
import os
from pathlib import Path
import subprocess
import sys
import traceback
from unittest.mock import Mock

import pytest
import yaml

import ouroboros.config as config
from ouroboros.core.errors import ConfigError

SECRET = "SYNTHETIC_PRIVATE_VALUE"
PARSE_CASES = {
    "tag": f"orchestrator: !{SECRET} {{}}\n".encode(),
    "alias": f"orchestrator: *{SECRET}\n".encode(),
    "utf8": SECRET.encode() + b"\xff",
    "mapping-key": f"? [{SECRET}]\n: value\n".encode(),
}
GETTERS = ["get_max_parallel_workers", "get_usage_limit_pause_seconds"]
NUMBERS = [SECRET, [SECRET], {SECRET: SECRET}, True, False, 0, -1, 1.5, float("inf"), float("nan")]
ENV_KEYS = [
    "OUROBOROS_MAX_PARALLEL_WORKERS",
    "OUROBOROS_USAGE_LIMIT_PAUSE_HOURS",
    "OUROBOROS_MCP_TOOL_TIMEOUT_SECONDS",
    "OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS",
    "OUROBOROS_GENERATION_NO_PROGRESS_TIMEOUT_SECONDS",
    "OUROBOROS_GENERATION_SAFETY_TIMEOUT_SECONDS",
    "OUROBOROS_WATCHDOG_POLL_SECONDS",
    "OUROBOROS_GENERATION_TIMEOUT",
]
EXPORT_GETTERS = sorted(
    name
    for name in set(config.__all__)
    if name.startswith(("get_", "resolve_"))
    and not name.startswith("get_default_")
    and name != "get_config_dir"
)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Tests start in the credential-free runner, then each case gets its own home.
    home = tmp_path / "home"
    directory = home / ".ouroboros"
    directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in list(os.environ):
        if name.startswith("OUROBOROS_") and name not in {
            "OUROBOROS_TELEMETRY",
            "OUROBOROS_DASHBOARD",
        }:
            monkeypatch.delenv(name)
    return directory / "config.yaml"


def assert_safe(error: ConfigError) -> None:
    assert SECRET not in str(error)
    assert SECRET not in repr(error.details)
    assert SECRET not in str(error.config_key)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("getter", GETTERS)
@pytest.mark.parametrize("content", PARSE_CASES.values(), ids=PARSE_CASES)
def test_partial_getter_parser(config_home: Path, getter: str, content: bytes) -> None:
    config_home.write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        getattr(config, getter)()
    assert_safe(caught.value)
    assert caught.value.config_file == str(config_home)
    assert caught.value.details["yaml_error"] in {"invalid_yaml", "invalid_utf8"}


@pytest.mark.parametrize("getter", GETTERS)
@pytest.mark.parametrize("value", NUMBERS, ids=lambda value: type(value).__name__ + repr(value))
def test_partial_getter_numbers(config_home: Path, getter: str, value: object) -> None:
    field = "max_parallel_workers" if getter == GETTERS[0] else "usage_limit_pause_hours"
    if getter == GETTERS[1] and value == 1.5:
        value = float("-inf")
    config_home.write_text(yaml.safe_dump({"orchestrator": {field: value}}), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        getattr(config, getter)()
    assert_safe(caught.value)
    assert caught.value.config_key == "orchestrator." + field
    assert "value" not in caught.value.details


@pytest.mark.parametrize("getter", GETTERS)
@pytest.mark.parametrize("value", [SECRET, [SECRET], True, 7, 1.2])
def test_partial_getter_nonmapping_section(config_home: Path, getter: str, value: object) -> None:
    config_home.write_text(yaml.safe_dump({"orchestrator": value}), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        getattr(config, getter)()
    assert_safe(caught.value)
    assert caught.value.config_key == "orchestrator"
    assert "value" not in caught.value.details


@pytest.mark.parametrize("getter", GETTERS)
@pytest.mark.parametrize("content", [SECRET, f"- {SECRET}\n", "true", "42"])
def test_partial_getter_nonmapping_root(config_home: Path, getter: str, content: str) -> None:
    config_home.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        getattr(config, getter)()
    assert_safe(caught.value)
    assert "value_type" in caught.value.details


@pytest.mark.parametrize("key", ENV_KEYS)
@pytest.mark.parametrize("value", [SECRET, "-1"])
def test_numeric_environment(
    config_home: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    config_home.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(key, value)
    getter = (
        config.get_max_parallel_workers
        if key == ENV_KEYS[0]
        else config.get_usage_limit_pause_seconds
        if key == ENV_KEYS[1]
        else config.get_runtime_controls_config
    )
    with pytest.raises(ConfigError) as caught:
        getter()
    assert_safe(caught.value)
    assert caught.value.config_key == key
    assert "value" not in caught.value.details


@pytest.mark.parametrize("getter", GETTERS)
def test_getter_read_failure(
    config_home: Path, monkeypatch: pytest.MonkeyPatch, getter: str
) -> None:
    config_home.write_text("{}", encoding="utf-8")
    original = Path.open

    def fail_read(path, *args, **kwargs):
        if path == config_home:
            raise OSError(5, SECRET)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_read)
    with pytest.raises(ConfigError) as caught:
        getattr(config, getter)()
    assert_safe(caught.value)
    assert caught.value.config_file == str(config_home)


@pytest.mark.parametrize(
    "content",
    [None, "", "{}", "orchestrator: {}", "orchestrator: null", "logging: {level: INVALID}"],
)
def test_partial_getter_defaults(config_home: Path, content: str | None) -> None:
    if content is not None:
        config_home.write_text(content, encoding="utf-8")
    assert config.get_max_parallel_workers() == 3
    assert config.get_usage_limit_pause_seconds() == 18000


@pytest.mark.parametrize("hours,seconds", [("1.5", 5400), ("0.00001", 1), ("8760", 31536000)])
def test_partial_getters_keep_unrelated_schema_errors(
    config_home: Path, hours: str, seconds: int
) -> None:
    config_home.write_text(
        "evaluation: {satisfaction_threshold: INVALID}\n"
        f"orchestrator: {{max_parallel_workers: '5', usage_limit_pause_hours: '{hours}'}}\n",
        encoding="utf-8",
    )
    assert config.get_max_parallel_workers() == 5
    assert config.get_usage_limit_pause_seconds() == seconds
    with pytest.raises(ConfigError):
        config.load_config(config_home)


def test_pause_bound_does_not_retain_numeric_value(config_home: Path) -> None:
    config_home.write_text("orchestrator: {usage_limit_pause_hours: 8761}", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        config.get_usage_limit_pause_seconds()
    assert_safe(caught.value)
    assert "value" not in caught.value.details
    assert caught.value.details["max_seconds"] == config.MAX_USAGE_LIMIT_PAUSE_SECONDS


@pytest.mark.parametrize("entrypoint", ["load_config", "load_credentials"])
@pytest.mark.parametrize("content", PARSE_CASES.values(), ids=PARSE_CASES)
def test_full_loaders_real_parser(config_home: Path, entrypoint: str, content: bytes) -> None:
    config_home.write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        getattr(config, entrypoint)(config_home)
    assert_safe(caught.value)


@pytest.mark.parametrize("getter", EXPORT_GETTERS)
@pytest.mark.parametrize(
    "content",
    [PARSE_CASES["tag"], f"runtime_controls: {{watchdog_poll_seconds: {SECRET}}}\n".encode()],
)
def test_all_exported_getter_error_surfaces(config_home: Path, getter: str, content: bytes) -> None:
    config_home.write_bytes(content)
    function = getattr(config, getter)
    args = {"stage": "execute", "role": "execute"}
    kwargs = {
        name: args[name]
        for name, p in inspect.signature(function).parameters.items()
        if p.default is inspect.Parameter.empty
    }
    try:
        function(**kwargs)
    except ConfigError as error:
        assert_safe(error)


MCP_BAD = {
    **PARSE_CASES,
    "worker-string": f"orchestrator: {{max_parallel_workers: {SECRET}}}".encode(),
    "worker-dict": f"orchestrator: {{max_parallel_workers: {{{SECRET}: x}}}}".encode(),
    "section": f"orchestrator: {SECRET}".encode(),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [False, True], ids=["execute", "start"])
@pytest.mark.parametrize("content", MCP_BAD.values(), ids=MCP_BAD)
async def test_actual_mcp_config_error(config_home: Path, start: bool, content: bytes) -> None:
    from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler

    config_home.write_bytes(content)
    handler = (
        StartExecuteSeedHandler(event_store=Mock(), agent_runtime_backend="claude")
        if start
        else ExecuteSeedHandler(agent_runtime_backend="claude")
    )
    result = await handler.handle(
        {"seed_content": "goal: safe config probe\n", "cwd": str(config_home.parent)}
    )
    assert result.is_err
    output = str(result.error)
    assert "Execution handler config error:" in output, output
    assert SECRET not in output


@pytest.mark.parametrize("content", MCP_BAD.values(), ids=MCP_BAD)
def test_actual_cli_run_config_error(config_home: Path, content: bytes) -> None:
    config_home.write_bytes(content)
    seed = config_home.parent / "seed.yaml"
    seed.write_text(
        "goal: safe config probe\nontology_schema: {name: probe, description: probe}\nmetadata: {}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", "run", str(seed)],
        cwd=config_home.parent,
        capture_output=True,
        text=True,
        timeout=40,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert SECRET not in output
    assert "configuration" in output.lower() or "orchestrator" in output


CLI_VALUES = [f"/missing/{SECRET}", [SECRET], {SECRET: SECRET}, 42, True, False, "a\0" + SECRET]


@pytest.mark.parametrize("invalid_schema", [False, True])
@pytest.mark.parametrize("value", CLI_VALUES)
def test_real_cli_validate_path_and_schema(
    config_home: Path, value: object, invalid_schema: bool
) -> None:
    data = {"orchestrator": {"runtime_backend": "claude", "cli_path": value}}
    if invalid_schema:
        data["evaluation"] = {"satisfaction_threshold": SECRET}
    config_home.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", "config", "validate"],
        cwd=config_home.parent,
        capture_output=True,
        text=True,
        timeout=40,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert SECRET not in output
    assert "orchestrator.cli_path" in output, output
    assert "Traceback" not in output


def test_cli_path_check_independent_of_schema(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from ouroboros.cli.commands import config as command

    config_home.write_text(
        f"orchestrator: {{runtime_backend: claude, cli_path: /missing/{SECRET}}}", encoding="utf-8"
    )
    monkeypatch.setattr("ouroboros.config.loader.load_config", lambda: config.OuroborosConfig())
    result = CliRunner().invoke(command.app, ["validate"])
    assert result.exit_code == 1
    assert "orchestrator.cli_path" in result.output
    assert SECRET not in result.output


@pytest.mark.parametrize("command", [("config", "validate"), ("status", "health")])
@pytest.mark.parametrize("content", PARSE_CASES.values(), ids=PARSE_CASES)
def test_real_cli_parser_errors(
    config_home: Path, command: tuple[str, str], content: bytes
) -> None:
    config_home.write_bytes(content)
    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", *command],
        cwd=config_home.parent,
        capture_output=True,
        text=True,
        timeout=40,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert SECRET not in output
    if command[0] == "status":
        assert "configuration unavailable" in output


@pytest.mark.parametrize("getter", GETTERS)
def test_partial_getters_ignore_user_mapping_keys(config_home: Path, getter: str) -> None:
    config_home.write_text(f"orchestrator: {{{SECRET}: true}}", encoding="utf-8")
    assert getattr(config, getter)() == (3 if getter == GETTERS[0] else 18000)


@pytest.mark.parametrize(
    "backend",
    [
        "claude",
        "codex",
        "opencode",
        "hermes",
        "goose",
        "zcode",
        "gemini",
        "copilot",
        "kiro",
        "grok",
        "antigravity",
        "ourocode",
        "dsh",
        "gjc",
        "omp",
        "megacode",
        "pi",
    ],
)
def test_cli_path_fields_independent_of_schema(
    config_home: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    from typer.testing import CliRunner

    from ouroboros.backends import get_backend_capability
    from ouroboros.cli.commands import config as command

    capability = get_backend_capability(backend)
    assert capability is not None and capability.cli_config_key is not None
    config_home.write_text(
        yaml.safe_dump(
            {
                "orchestrator": {
                    "runtime_backend": backend,
                    capability.cli_config_key: f"/missing/{SECRET}",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("ouroboros.config.loader.load_config", lambda: config.OuroborosConfig())
    result = CliRunner().invoke(command.app, ["validate"])
    assert result.exit_code == 1
    assert "orchestrator." + capability.cli_config_key in result.output
    assert SECRET not in result.output
