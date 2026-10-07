"""Setup must not infer a legacy Codex profile format from a failed probe."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import ouroboros.cli.commands.setup as setup_cmd
from ouroboros.cli.commands.setup import _codex_uses_profile_v2 as detect_profile_format


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both Windows and POSIX home lookups, including Codex overrides."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.delenv("OUROBOROS_CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.parametrize("detected", [True, False, None])
def test_setup_probe_preserves_all_three_detection_results(detected: bool | None) -> None:
    """An inconclusive shared probe is not equivalent to a confirmed old CLI."""
    with patch.object(setup_cmd, "_shared_codex_uses_profile_v2", return_value=detected):
        assert detect_profile_format("configured-codex") is detected


@pytest.mark.parametrize("registrar", ["default", "worker"])
@pytest.mark.parametrize("existing", [False, True], ids=["fresh-home", "existing-profiles"])
def test_unknown_profile_format_preserves_home_without_reading_profiles(
    isolated_home: Path,
    registrar: str,
    existing: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not create directories, parse user files, or select either profile format."""
    codex_home = isolated_home / ".codex"
    originals: dict[Path, bytes] = {}
    if existing:
        codex_home.mkdir()
        originals = {
            codex_home / "config.toml": (
                '# 사용자 설정\r\nmodel = "user-model"\r\n'
                '[profiles.ouroboros-worker]\r\nmodel = "legacy-choice"\r\n'
            ).encode(),
            codex_home / "ouroboros-worker.config.toml": (
                '# 보존할 프로필\r\nmodel = "file-choice"\r\n'
            ).encode(),
        }
        for path, contents in originals.items():
            path.write_bytes(contents)

    monkeypatch.setattr(setup_cmd, "_codex_uses_profile_v2", lambda *_a, **_kw: None)
    register = (
        setup_cmd._register_codex_default_profiles
        if registrar == "default"
        else setup_cmd._register_codex_worker_profile
    )
    with (
        patch.object(Path, "read_text", side_effect=AssertionError("unexpected profile read")),
        patch.object(setup_cmd, "print_warning") as warning,
    ):
        result = register(codex_path="configured-codex")

    assert result is (False if registrar == "worker" else None)
    message = " ".join(str(call.args[0]) for call in warning.call_args_list).lower()
    assert "cannot determine" in message or "could not determine" in message
    assert "--help" in message
    assert "setup" in message
    assert codex_home.exists() is existing
    assert {path: path.read_bytes() for path in codex_home.glob("*")} == originals


def _home_files(home: Path) -> dict[Path, bytes]:
    return {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}


@pytest.mark.parametrize("existing", [False, True], ids=["fresh-install", "existing-install"])
@pytest.mark.parametrize("public_cli", [False, True], ids=["setup-transaction", "public-cli"])
def test_unknown_profile_format_rolls_back_setup_and_reports_failure(
    isolated_home: Path,
    existing: bool,
    public_cli: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late probe failure restores prior writes and never reports setup success."""
    codex_home = isolated_home / ".codex"
    codex_config = codex_home / "config.toml"
    config_dir = isolated_home / ".ouroboros"
    config_path = config_dir / "config.yaml"
    rule_path = codex_home / "rules" / "ouroboros.md"
    if existing:
        config_dir.mkdir()
        config_path.write_bytes(
            "# 기존 실행 설정\r\norchestrator:\r\n  runtime_backend: claude\r\n"
            "llm:\r\n  backend: claude\r\n".encode()
        )
        (config_dir / "credentials.yaml").write_bytes(b"# keep credentials\r\nproviders: {}\r\n")
        rule_path.parent.mkdir(parents=True)
        rule_path.write_bytes(b"# user-edited managed rule\r\n")
        generated_profile = setup_cmd._render_codex_profile_section(
            "ouroboros-fast", setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"]
        )
        codex_config.write_bytes(
            ('# 사용자 설정\r\nmodel = "user-model"\r\n' + generated_profile + "\r\n").encode(
                "utf-8"
            )
        )
        (codex_home / "ouroboros-worker.config.toml").write_bytes(
            b'# preserve profile-v2\r\nmodel = "user-worker"\r\n'
        )
    before_files = _home_files(isolated_home)
    before_dirs = {
        path.relative_to(isolated_home) for path in isolated_home.rglob("*") if path.is_dir()
    }
    stages: list[str] = []

    def register_mcp(**kwargs: object) -> bool:
        codex_home.mkdir(parents=True, exist_ok=True)
        original = codex_config.read_bytes() if codex_config.exists() else b""
        codex_config.write_bytes(original + b'\n[mcp_servers.ouroboros]\ncommand = "new-cli"\n')
        expected = kwargs["expected_snapshots"]
        assert isinstance(expected, dict)
        expected[codex_config] = setup_cmd._snapshot_path(codex_config)
        stages.append("mcp")
        return True

    def install_artifacts(**kwargs: object) -> bool:
        assert config_path.read_bytes() != before_files.get(config_path.relative_to(isolated_home))
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_bytes(b"# new packaged rule\n")
        expected = kwargs["expected_snapshots"]
        assert isinstance(expected, dict)
        expected[rule_path] = setup_cmd._snapshot_path(rule_path)
        stages.append("artifacts")
        return True

    codex_path = str(isolated_home / "bin" / "codex")
    monkeypatch.setattr(setup_cmd, "_codex_uses_profile_v2", detect_profile_format)
    monkeypatch.setattr(setup_cmd, "_register_codex_mcp_server", register_mcp)
    monkeypatch.setattr(setup_cmd, "_install_codex_artifacts", install_artifacts)
    monkeypatch.setattr(setup_cmd, "_detect_runtimes", lambda: {"codex": codex_path})
    monkeypatch.setattr(setup_cmd, "_get_current_backend", lambda: None)
    monkeypatch.setattr(
        setup_cmd.package_profiles, "has_unsupported_claude_sdk_mcp_mix", lambda: False
    )
    with patch.object(
        setup_cmd.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired([codex_path, "--help"], timeout=5),
    ) as probe:
        if public_cli:
            result = CliRunner().invoke(setup_cmd.app, ["--runtime", "codex", "--non-interactive"])
            assert result.exit_code == 1, result.output
            assert "Setup complete!" not in result.output
            assert "Configured Codex runtime" not in result.output
            assert "setup incomplete" in result.output
        else:
            assert setup_cmd._setup_codex(codex_path) is False

    assert stages == ["mcp", "artifacts"]
    probe.assert_called_once()
    assert _home_files(isolated_home) == before_files
    assert {
        path.relative_to(isolated_home) for path in isolated_home.rglob("*") if path.is_dir()
    } == before_dirs
