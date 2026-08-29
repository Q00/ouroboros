"""Unit tests for the config command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.config import _resolve_db_path, app
from ouroboros.config._model_defaults import DEFAULT_SONNET_MODEL

runner = CliRunner(env={"COLUMNS": "200"})

_EVENT_STORE_CONFIG_COMMANDS = [
    ["config", "show", "--json"],
    ["status", "executions"],
    ["resume"],
    ["tui", "open"],
    ["mcp", "doctor", "--json"],
    ["mcp", "serve", "--transport", "stdio"],
]
_EVENT_STORE_CONFIG_TIMEOUT_SECONDS = 30


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a minimal config dir with valid config.yaml."""
    config = {
        "orchestrator": {
            "runtime_backend": "claude",
            "cli_path": "/usr/bin/claude",
        },
        "llm": {"backend": "claude"},
        "logging": {"level": "info"},
        "persistence": {"database_path": "data/ouroboros.db"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))
    return tmp_path


@pytest.fixture()
def codex_config_dir(tmp_path: Path) -> Path:
    """Create a config dir with codex backend."""
    config = {
        "orchestrator": {
            "runtime_backend": "codex",
            "codex_cli_path": "/usr/bin/codex",
        },
        "llm": {"backend": "codex"},
        "logging": {"level": "info"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))
    return tmp_path


def _patch_config_dir(config_dir: Path):
    """Patch get_config_dir to return our temp dir."""
    return patch("ouroboros.cli.commands.config._load_config", side_effect=None)


# ── config show ──────────────────────────────────────────────────


class TestConfigShow:
    """Tests for config show command."""

    def test_show_displays_summary(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "claude" in result.output

    def test_show_section(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show", "orchestrator"])
        assert result.exit_code == 0
        assert "runtime_backend" in result.output

    def test_show_invalid_section(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show", "nonexistent"])
        assert result.exit_code == 1

    def test_show_codex_cli_path(self, codex_config_dir: Path) -> None:
        """config show should display codex_cli_path for codex backend."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir),
            patch("ouroboros.cli.commands.setup._detect_runtimes", return_value={"codex": None}),
        ):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "/usr/bin/codex" in result.output

    def test_show_codex_cli_path_uses_runtime_wrapper_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Effective config output must report the executable runtime will launch."""
        wrapper = tmp_path / "codex-wrapper"
        wrapper.write_bytes(b"\xcf\xfa\xed\xfe" + b"zeude wrapper:codex")
        wrapper.chmod(0o755)
        real_dir = tmp_path / "bin"
        real_dir.mkdir()
        real_cli = real_dir / "codex"
        real_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real_cli.chmod(0o755)

        config = {
            "orchestrator": {
                "runtime_backend": "codex",
                "codex_cli_path": str(wrapper),
            },
            "llm": {"backend": "codex"},
            "logging": {"level": "info"},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

        monkeypatch.setenv("PATH", str(real_dir))
        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["environment"]["cli_path"] == str(real_cli)

    def test_show_database_path_from_persistence(self, config_dir: Path) -> None:
        """config show should resolve persistence.database_path under config dir."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert _resolve_db_path(data, config_dir / "config.yaml") == str(
            config_dir / "data" / "ouroboros.db"
        )

    def test_show_database_path_default(self, tmp_path: Path) -> None:
        """Without persistence.database_path, default is <config_dir>/ouroboros.db."""
        config = {
            "orchestrator": {"runtime_backend": "claude", "cli_path": "/usr/bin/claude"},
            "llm": {"backend": "claude"},
            "logging": {"level": "info"},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert _resolve_db_path(config, tmp_path / "config.yaml") == str(tmp_path / "ouroboros.db")

    def test_show_database_path_uses_legacy_runtime_authority(self, config_dir: Path) -> None:
        legacy_db = config_dir / "ouroboros.db"
        legacy_db.touch()

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show"])

        assert result.exit_code == 0
        data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert _resolve_db_path(data, config_dir / "config.yaml") == str(legacy_db)

    def test_show_invalid_database_path_exits_without_traceback(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        config_dir = home / ".ouroboros"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("persistence:\n  database_path:\n")
        env = os.environ.copy()
        env["HOME"] = str(home)

        result = subprocess.run(
            [sys.executable, "-m", "ouroboros", "config", "show", "--json"],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

        assert result.returncode == 1
        assert "persistence.database_path" in result.stdout
        assert "Traceback" not in result.stdout + result.stderr

    def test_show_json_reports_blank_execution_model_env_as_a_cleared_override(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The effective view must match ``get_execution_model()`` semantics."""
        data = yaml.safe_load((config_dir / "config.yaml").read_text())
        data["execution"] = {"default_model": "terra"}
        (config_dir / "config.yaml").write_text(yaml.dump(data))
        monkeypatch.setenv("OUROBOROS_EXECUTION_MODEL", "")

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show", "--json"])

        assert result.exit_code == 0
        execute = json.loads(result.output)["stages"]["execute"]
        assert execute["model"] == DEFAULT_SONNET_MODEL
        assert execute["model_source"] == "env OUROBOROS_EXECUTION_MODEL (cleared) ⚠"

    def test_show_json_reports_unpinned_claude_execution_model_truthfully(
        self, config_dir: Path
    ) -> None:
        """The effective view must show the same Claude fallback the executors use."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["show", "--json"])

        assert result.exit_code == 0
        execute = json.loads(result.output)["stages"]["execute"]
        assert execute["model"] == DEFAULT_SONNET_MODEL
        assert execute["model_source"] == "default → backend default"

    def test_show_codex_automatic_model_is_not_presented_as_a_runtime_fact(
        self, codex_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config hint is explicitly distinct from the live Codex choice."""
        monkeypatch.setattr(
            "ouroboros.backends.model_catalog.configured_default_model",
            lambda _backend: "gpt-configured",
        )

        with patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir):
            result = runner.invoke(app, ["show", "--json"])

        assert result.exit_code == 0
        execute = json.loads(result.output)["stages"]["execute"]
        assert execute["model"] == (
            "Codex current selected model (config.toml: gpt-configured; not confirmed at runtime)"
        )
        assert execute["model_source"] == "automatic Codex selection"


@pytest.mark.parametrize("arguments", _EVENT_STORE_CONFIG_COMMANDS)
def test_invalid_yaml_commands_redact_config_contents(tmp_path: Path, arguments: list[str]) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    config_dir.mkdir(parents=True)
    secret = "sk-super-secret"
    (config_dir / "config.yaml").write_text(
        f"persistence:\n  database_path: [\napi_key: {secret}: exposed\n"
    )
    env = os.environ.copy()
    env.pop("_OUROBOROS_NESTED", None)
    env["HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", *arguments],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=_EVENT_STORE_CONFIG_TIMEOUT_SECONDS,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert secret not in output
    assert "Traceback" not in output
    if arguments[:2] == ["mcp", "doctor"]:
        checks = json.loads(result.stdout)
        event_store = next(check for check in checks if check["name"] == "event_store")
        assert event_store["message"] == "Invalid EventStore configuration."
    else:
        assert str(home) not in output


@pytest.mark.parametrize("arguments", [["status", "executions"], ["resume"]])
def test_non_mapping_config_redacts_config_path(tmp_path: Path, arguments: list[str]) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("- not-a-mapping\n")
    env = os.environ.copy()
    env["HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", *arguments],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=_EVENT_STORE_CONFIG_TIMEOUT_SECONDS,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert str(home) not in output
    assert "Traceback" not in output


@pytest.mark.parametrize("arguments", _EVENT_STORE_CONFIG_COMMANDS)
def test_wrong_type_persistence_redacts_config_value(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    config_dir.mkdir(parents=True)
    private_value = "/Users/gate/private"
    config_path = config_dir / "config.yaml"
    config_path.write_text(f"persistence: {private_value}\n")
    env = os.environ.copy()
    env.pop("_OUROBOROS_NESTED", None)
    env["HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", *arguments],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=_EVENT_STORE_CONFIG_TIMEOUT_SECONDS,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert private_value not in output
    assert str(config_path) not in output
    assert "Traceback" not in output


@pytest.mark.parametrize("arguments", _EVENT_STORE_CONFIG_COMMANDS)
def test_unresolvable_user_database_path_is_redacted(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    config_dir.mkdir(parents=True)
    private_user = "ouroboros-user-that-must-not-exist-1817"
    config_path = config_dir / "config.yaml"
    config_path.write_text(f"persistence:\n  database_path: '~{private_user}/events.db'\n")
    env = os.environ.copy()
    env.pop("_OUROBOROS_NESTED", None)
    env["HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "ouroboros", *arguments],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=_EVENT_STORE_CONFIG_TIMEOUT_SECONDS,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert private_user not in output
    assert str(config_path) not in output
    assert "Traceback" not in output


# ── config backend ───────────────────────────────────────────────


class TestConfigBackend:
    """Tests for config backend command."""

    def test_help_lists_all_switchable_backends(self) -> None:
        result = runner.invoke(app, ["backend", "--help"])

        assert result.exit_code == 0
        for backend in ("antigravity", "grok", "zcode"):
            assert backend in result.output

    def test_show_current_backend(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend"])
        assert result.exit_code == 0
        assert "claude" in result.output

    def test_show_current_codex_backend_uses_runtime_resolver(self, codex_config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                side_effect=AssertionError("config backend must use the runtime resolver"),
            ),
        ):
            result = runner.invoke(app, ["backend"])

        assert result.exit_code == 0
        assert "/usr/bin/codex" in result.output

    def test_switch_to_same_backend(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend", "claude"])
        assert result.exit_code == 0
        assert "Already using" in result.output

    def test_switch_unsupported_backend(self, config_dir: Path) -> None:
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend", "nonexistent"])
        assert result.exit_code == 1
        assert "Unsupported" in result.output

    def test_switch_opencode_not_switchable(self, config_dir: Path) -> None:
        """opencode is a valid backend but not yet switchable via config backend."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["backend", "opencode"])
        assert result.exit_code == 1
        assert "opencode" in result.output

    def test_switch_cli_not_found(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup._detect_runtimes", return_value={"codex": None}),
        ):
            result = runner.invoke(app, ["backend", "codex"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_switch_delegates_to_setup(self, config_dir: Path) -> None:
        """config backend should delegate to _setup_codex for full side effects."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("ouroboros.cli.commands.setup._setup_codex") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "codex"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/codex")

    def test_switch_to_codex_uses_canonical_detection(self, config_dir: Path) -> None:
        """config backend codex must honor env/config/App detection, not only PATH."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value=None),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={"codex": "/opt/codex/bin/codex"},
            ),
            patch("ouroboros.cli.commands.setup._setup_codex", return_value=True) as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "codex"])

        assert result.exit_code == 0, result.output
        mock_setup.assert_called_once_with("/opt/codex/bin/codex")

    def test_switch_to_codex_fails_when_setup_returns_false(self, config_dir: Path) -> None:
        """config backend codex must not report success when setup rolls back."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("ouroboros.cli.commands.setup._setup_codex", return_value=False),
        ):
            result = runner.invoke(app, ["backend", "codex"])

        assert result.exit_code == 1
        assert "Could not switch backend to codex" in result.output

    def test_switch_to_claude_delegates_to_setup(self, codex_config_dir: Path) -> None:
        """config backend claude should delegate to _setup_claude."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("ouroboros.cli.commands.setup._setup_claude") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "claude"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/claude")

    def test_switch_to_claude_fails_when_setup_returns_false(self, codex_config_dir: Path) -> None:
        """A failed Claude activation must propagate through the CLI exit status."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=codex_config_dir),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("ouroboros.cli.commands.setup._setup_claude", return_value=False),
        ):
            result = runner.invoke(app, ["backend", "claude"])

        assert result.exit_code == 1
        assert "Could not switch backend to claude" in result.output
        assert "Switched backend" not in result.output

    def test_switch_to_hermes_delegates_to_setup(self, config_dir: Path) -> None:
        """config backend hermes should delegate to _setup_hermes."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/hermes"),
            patch("ouroboros.cli.commands.setup._setup_hermes") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "hermes"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/hermes")

    def test_switch_to_gjc_delegates_to_setup(self, config_dir: Path) -> None:
        """config backend gjc should delegate to _setup_gjc for install side effects."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            # Isolate from the developer's real ~/.ouroboros config, which may
            # carry orchestrator.gjc_cli_path and shadow the shutil.which stub.
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
            patch("shutil.which", return_value="/usr/bin/gjc"),
            patch("ouroboros.cli.commands.setup._setup_gjc") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "gjc"])
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/gjc")

    def test_switch_to_gjc_honors_configured_cli_path(self, config_dir: Path) -> None:
        """config backend gjc should honor explicit env/config path helpers."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_gjc_cli_path", return_value="/opt/gjc/bin/gjc"),
            patch("shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup._setup_gjc") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "gjc"])

        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/opt/gjc/bin/gjc")

    def test_switch_to_goose_honors_configured_cli_path(self, config_dir: Path) -> None:
        """config backend goose should honor explicit env/config path helpers."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_goose_cli_path", return_value="/opt/goose/bin/goose"),
            patch("shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup._setup_goose") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "goose"])

        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/opt/goose/bin/goose")

    def test_switch_to_pi_honors_configured_cli_path(self, config_dir: Path) -> None:
        """config backend pi should honor explicit env/config path helpers."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_pi_cli_path", return_value="/opt/pi/bin/pi"),
            patch("shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup._setup_pi") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "pi"])

        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/opt/pi/bin/pi")

    def test_switch_to_pi_reports_missing_cli_path(self, config_dir: Path) -> None:
        """config backend pi should surface pi-specific guidance when no CLI is found."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_pi_cli_path", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            result = runner.invoke(app, ["backend", "pi"])

        assert result.exit_code == 1
        assert "OUROBOROS_PI_CLI_PATH" in result.output

    def test_switch_to_omp_honors_configured_cli_path(self, config_dir: Path) -> None:
        """config backend omp should honor explicit env/config path helpers."""

        def fake_which(name: str) -> str | None:
            return "/opt/omp/bin/omp" if name == "/opt/omp/bin/omp" else None

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_omp_cli_path", return_value="/opt/omp/bin/omp"),
            patch("shutil.which", side_effect=fake_which),
            patch("ouroboros.cli.commands.setup._setup_omp") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "omp"])

        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/opt/omp/bin/omp")

    def test_switch_to_omp_reports_missing_cli_path(self, config_dir: Path) -> None:
        """config backend omp should surface omp-specific guidance when no CLI is found."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_omp_cli_path", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            result = runner.invoke(app, ["backend", "omp"])

        assert result.exit_code == 1
        assert "OUROBOROS_OMP_CLI_PATH" in result.output

    def test_switch_to_omp_fails_when_setup_fails(self, config_dir: Path) -> None:
        """config backend omp must fail (exit 1) when omp setup fails closed."""

        def fake_which(name: str) -> str | None:
            return "/opt/omp/bin/omp" if name == "/opt/omp/bin/omp" else None

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_omp_cli_path", return_value="/opt/omp/bin/omp"),
            patch("shutil.which", side_effect=fake_which),
            patch("ouroboros.cli.commands.setup._setup_omp", return_value=False),
        ):
            result = runner.invoke(app, ["backend", "omp"])

        assert result.exit_code == 1
        assert "Could not switch backend to omp" in result.output

    def test_switch_to_omp_falls_back_to_path_when_configured_path_is_stale(
        self, config_dir: Path
    ) -> None:
        """PR #2299 round 4: a stale configured omp path must not shadow a valid PATH install."""

        def fake_which(name: str) -> str | None:
            return "/usr/bin/omp" if name == "omp" else None

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.get_omp_cli_path", return_value="/missing/configured/omp"),
            patch("shutil.which", side_effect=fake_which),
            patch("ouroboros.cli.commands.setup._setup_omp") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "omp"])

        assert result.exit_code == 0
        mock_setup.assert_called_once_with("/usr/bin/omp")

    def test_switch_to_zcode_honors_configured_cli_path(self, config_dir: Path) -> None:
        """config backend zcode should use the app-bundle/config path and the
        runtime-only setup helper instead of claiming success without writing."""
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.config.get_zcode_cli_path",
                return_value="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
            ),
            patch("shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup._setup_zcode") as mock_setup,
        ):
            result = runner.invoke(app, ["backend", "zcode"])

        assert result.exit_code == 0, result.output
        mock_setup.assert_called_once_with(
            "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
        )

    def test_switch_warns_on_setup_print_error(self, config_dir: Path) -> None:
        """config backend should warn when setup emits print_error (non-exception failure)."""
        from ouroboros.cli.commands import setup as setup_mod

        def _failing_setup(cli_path: str) -> None:
            setup_mod.__dict__["print_error"]("Could not locate packaged Codex rules.")

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("ouroboros.cli.commands.setup._setup_codex", side_effect=_failing_setup),
        ):
            result = runner.invoke(app, ["backend", "codex"])
        assert result.exit_code == 0
        assert "issues" in result.output or "Warning" in result.output

    def test_malformed_config_yaml(self, tmp_path: Path) -> None:
        """config commands should handle malformed YAML gracefully."""
        (tmp_path / "config.yaml").write_text(": invalid: yaml: [")

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 1
        assert "Invalid YAML" in result.output

    def test_structurally_invalid_section_type(self, tmp_path: Path) -> None:
        """config commands should handle sections with wrong types (e.g. orchestrator: [])."""
        config = {"orchestrator": [], "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["show"])
        assert result.exit_code == 1
        assert "Invalid config section" in result.output

    def test_structurally_invalid_logging_section(self, tmp_path: Path) -> None:
        """config validate should catch logging: [] instead of crashing."""
        config = {"orchestrator": {"runtime_backend": "claude"}, "logging": "bad"}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1
        assert "Invalid config section" in result.output


# ── config validate ──────────────────────────────────────────────


class TestConfigValidate:
    """Tests for config validate command."""

    def test_valid_config(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_file", return_value=True),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_invalid_backend_exits_nonzero(self, tmp_path: Path) -> None:
        """validate should exit 1 when backend is unsupported."""
        config = {"orchestrator": {"runtime_backend": "nonexistent"}, "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1

    def test_opencode_backend_is_valid(self, tmp_path: Path) -> None:
        """validate should accept opencode as a valid runtime backend."""
        config = {"orchestrator": {"runtime_backend": "opencode"}, "llm": {"backend": "opencode"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

    def test_hermes_runtime_backend_is_valid(self, tmp_path: Path) -> None:
        """validate should accept hermes as a valid runtime backend."""
        config = {"orchestrator": {"runtime_backend": "hermes"}, "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

    def test_gemini_runtime_backend_is_valid(self, tmp_path: Path) -> None:
        """validate should accept gemini as a valid runtime backend."""
        config = {"orchestrator": {"runtime_backend": "gemini"}, "llm": {"backend": "gemini"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

    def test_gjc_runtime_and_llm_backend_are_valid(self, tmp_path: Path) -> None:
        """validate should accept the config written by `config backend gjc`."""
        config = {
            "orchestrator": {"runtime_backend": "gjc", "gjc_cli_path": "/usr/bin/gjc"},
            "llm": {"backend": "gjc"},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_file", return_value=True),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "alias",
        ["claude_code", "codex_cli", "opencode_cli", "gemini_cli", "hermes_cli"],
    )
    def test_runtime_backend_alias_is_valid(self, tmp_path: Path, alias: str) -> None:
        """validate should accept runtime_backend aliases that the schema permits."""
        config = {"orchestrator": {"runtime_backend": alias}, "llm": {"backend": "claude"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=tmp_path),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0

    def test_missing_cli_path_exits_nonzero(self, tmp_path: Path) -> None:
        """validate should exit 1 when CLI path doesn't exist."""
        config = {
            "orchestrator": {
                "runtime_backend": "claude",
                "cli_path": "/nonexistent/claude",
            },
            "llm": {"backend": "claude"},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1
        assert "does not exist" in result.output


# ── config set ───────────────────────────────────────────────────


class TestConfigSet:
    """Tests for config set command."""

    def test_set_existing_string_value(self, config_dir: Path) -> None:
        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(app, ["set", "logging.level", "debug"])
        assert result.exit_code == 0

        # Verify the file was actually written
        data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert data["logging"]["level"] == "debug"

    def test_set_unknown_top_level_key_rejected(self, config_dir: Path) -> None:
        """config set should reject unknown top-level keys."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["set", "typo.section", "value"])
        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_set_unknown_nested_key_rejected(self, config_dir: Path) -> None:
        """config set should reject unknown nested keys like logging.levle."""
        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["set", "logging.levle", "debug"])
        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_set_rejects_empty_database_path_without_writing(self, config_dir: Path) -> None:
        config_path = config_dir / "config.yaml"
        original = config_path.read_text()

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["set", "persistence.database_path", ""])

        assert result.exit_code == 1
        assert "Invalid value" in result.output
        assert config_path.read_text() == original

    def test_set_repairs_existing_invalid_database_path(self, config_dir: Path) -> None:
        config_path = config_dir / "config.yaml"
        data = yaml.safe_load(config_path.read_text())
        data["persistence"]["database_path"] = ""
        config_path.write_text(yaml.dump(data))

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.loader.load_config"),
        ):
            result = runner.invoke(
                app,
                ["set", "persistence.database_path", "data/repaired.db"],
            )

        assert result.exit_code == 0
        repaired = yaml.safe_load(config_path.read_text())
        assert repaired["persistence"]["database_path"] == "data/repaired.db"

    def test_set_rejects_existing_directory_database_path(self, config_dir: Path) -> None:
        config_path = config_dir / "config.yaml"
        original = config_path.read_text()
        directory = config_dir / "event-store-directory"
        directory.mkdir()

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runner.invoke(
                app,
                ["set", "persistence.database_path", str(directory)],
            )

        assert result.exit_code == 1
        assert "Invalid value" in result.output
        assert config_path.read_text() == original


# ── config init ──────────────────────────────────────────────────


class TestConfigInit:
    """Tests for config init command."""

    def test_init_existing_shows_info(self, config_dir: Path) -> None:
        # Create both files so init considers it fully initialized
        (config_dir / "credentials.yaml").write_text("test: true")
        with patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "already initialized" in result.output

    def test_init_partial_preserves_existing_config(self, config_dir: Path) -> None:
        """config init should NOT overwrite existing config.yaml when only credentials.yaml is missing."""
        # config_dir already has config.yaml with custom settings
        original_data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert original_data["orchestrator"]["runtime_backend"] == "claude"

        # No credentials.yaml — partial init state
        with patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0

        # config.yaml should be untouched
        after_data = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert after_data == original_data
        # credentials.yaml should now exist
        assert (config_dir / "credentials.yaml").exists()

    def test_init_partial_creates_missing_config(self, tmp_path: Path) -> None:
        """config init should create config.yaml when only credentials.yaml exists."""
        (tmp_path / "credentials.yaml").write_text(yaml.dump({"providers": {}}))

        with patch("ouroboros.config.loader.ensure_config_dir", return_value=tmp_path):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0

        # config.yaml should now exist with defaults
        assert (tmp_path / "config.yaml").exists()
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert "orchestrator" in data
        # credentials.yaml should be untouched (still has our original content)
        cred_data = yaml.safe_load((tmp_path / "credentials.yaml").read_text())
        assert cred_data == {"providers": {}}
