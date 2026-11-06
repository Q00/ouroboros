"""Observable one-step Undo behavior for successful and failed CLI saves."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands import config as config_commands
from ouroboros.cli.commands.config import app
from ouroboros.config import loader, models
from ouroboros.config_tui import persistence

runner = CliRunner(env={"COLUMNS": "200"})


def _config(level: str) -> bytes:
    return (
        "\ufeff# 설정 보존 😀\r\n"
        "orchestrator:\r\n"
        "  worktree_root: 'C:/workspace/문서😀'\r\n"
        f"logging:\r\n  level: {level}\r\n"
        "models:\r\n  pin: true\r\n"
        "persistence:\r\n  database_path: data/events.db\r\n"
    ).encode()


ORIGINAL = _config("info")


@pytest.fixture()
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(models, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(loader, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(persistence, "get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_bytes(ORIGINAL)
    return tmp_path


@pytest.fixture(params=[None, b"", _config("warning")], ids=["absent", "empty", "nonempty"])
def prior_backup(config_dir: Path, request: pytest.FixtureRequest) -> bytes | None:
    content = request.param
    if content is not None:
        (config_dir / "config.yaml.bak").write_bytes(content)
    return content


def _assert_files(config_dir: Path, current: bytes, backup: bytes | None) -> None:
    assert (config_dir / "config.yaml").read_bytes() == current
    backup_path = config_dir / "config.yaml.bak"
    assert (backup_path.read_bytes() if backup_path.exists() else None) == backup
    assert not list(config_dir.glob(".config.yaml.bak-*"))


def _set_level(level: str) -> None:
    result = runner.invoke(app, ["set", "logging.level", level])
    assert result.exit_code == 0, result.output


def test_set_undo_redo_preserve_exact_bytes(config_dir: Path, prior_backup: bytes | None) -> None:
    _set_level("debug")
    saved = (config_dir / "config.yaml").read_bytes()
    data = yaml.safe_load(saved)
    assert data["logging"]["level"] == "debug"
    assert data["orchestrator"]["worktree_root"] == "C:/workspace/문서😀"
    _assert_files(config_dir, saved, ORIGINAL)

    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 0, result.output
    _assert_files(config_dir, ORIGINAL, saved)
    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 0, result.output
    _assert_files(config_dir, saved, ORIGINAL)


@pytest.mark.parametrize("first_save", ["cli", "gui"])
def test_consecutive_saves_undo_the_latest_change(config_dir: Path, first_save: str) -> None:
    if first_save == "gui":
        persistence.apply_config_values({"logging.level": "debug"})
    else:
        _set_level("debug")
    intermediate = (config_dir / "config.yaml").read_bytes()
    _assert_files(config_dir, intermediate, ORIGINAL)
    _set_level("warning")
    latest = (config_dir / "config.yaml").read_bytes()
    _assert_files(config_dir, latest, intermediate)

    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 0, result.output
    _assert_files(config_dir, intermediate, latest)
    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 0, result.output
    _assert_files(config_dir, latest, intermediate)


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("models.pin", "tru", "Invalid boolean"),
        ("logging.levle", "debug", "Unknown config key"),
        ("logging.level", "not-a-level", "Invalid value"),
        ("persistence.database_path", "", "Invalid value"),
    ],
    ids=["boolean", "unknown-key", "schema", "database-path"],
)
def test_rejected_set_preserves_config_and_backup(
    config_dir: Path, prior_backup: bytes | None, key: str, value: str, reason: str
) -> None:
    result = runner.invoke(app, ["set", key, value])
    assert result.exit_code == 1, result.output
    assert reason in result.output
    _assert_files(config_dir, ORIGINAL, prior_backup)


@pytest.mark.parametrize("default_level", ["info", "invalid"])
def test_set_validates_the_target_config_path(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, default_level: str
) -> None:
    unrelated = config_dir / "unrelated-default"
    unrelated.mkdir()
    unrelated_bytes = _config(default_level)
    (unrelated / "config.yaml").write_bytes(unrelated_bytes)
    monkeypatch.setattr(loader, "get_config_dir", lambda: unrelated)

    _set_level("debug")
    saved = (config_dir / "config.yaml").read_bytes()
    assert yaml.safe_load(saved)["logging"]["level"] == "debug"
    _assert_files(config_dir, saved, ORIGINAL)
    assert (unrelated / "config.yaml").read_bytes() == unrelated_bytes
    assert not (unrelated / "config.yaml.bak").exists()


@pytest.mark.parametrize(
    "failure", ["config-write", "backup-create", "backup-write", "backup-replace"]
)
def test_failed_set_preserves_config_and_backup(
    config_dir: Path, prior_backup: bytes | None, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config_path = config_dir / "config.yaml"
    backup_path = config_dir / "config.yaml.bak"
    original_write_text = Path.write_text
    original_temporary_file = persistence.NamedTemporaryFile
    original_replace = Path.replace

    def fail_config_write(path, content, *args, **kwargs):
        if path == config_path:
            original_write_text(path, content[:7], *args, **kwargs)
            raise OSError("candidate write failed")
        return original_write_text(path, content, *args, **kwargs)

    def fail_backup_creation(*args, **kwargs):
        raise OSError("backup creation failed")

    @contextmanager
    def fail_backup_write(*args, **kwargs):
        with original_temporary_file(*args, **kwargs) as temporary:
            original_write = temporary.write

            def partial_write(content):
                original_write(content[:7])
                raise OSError("backup write failed")

            monkeypatch.setattr(temporary, "write", partial_write)
            yield temporary

    def fail_backup_replace(path, target):
        if Path(target) == backup_path:
            raise OSError("backup replacement failed")
        return original_replace(path, target)

    if failure == "config-write":
        monkeypatch.setattr(Path, "write_text", fail_config_write)
    elif failure == "backup-create":
        monkeypatch.setattr(persistence, "NamedTemporaryFile", fail_backup_creation)
    elif failure == "backup-write":
        monkeypatch.setattr(persistence, "NamedTemporaryFile", fail_backup_write)
    else:
        monkeypatch.setattr(Path, "replace", fail_backup_replace)

    result = runner.invoke(app, ["set", "logging.level", "debug"])
    assert result.exit_code == 1, result.output
    assert "rolled back" in result.output
    _assert_files(config_dir, ORIGINAL, prior_backup)


def test_failed_rollback_is_reported_and_preserves_backup(
    config_dir: Path, prior_backup: bytes | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = config_dir / "config.yaml"
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes
    partial_content = b""

    def fail_config_write(path, content, *args, **kwargs):
        nonlocal partial_content
        if path == config_path:
            original_write_text(path, content[:7], *args, **kwargs)
            partial_content = path.read_bytes()
            raise OSError("candidate write failed")
        return original_write_text(path, content, *args, **kwargs)

    def fail_rollback(path, content):
        if path == config_path:
            raise OSError("cannot restore current config")
        return original_write_bytes(path, content)

    monkeypatch.setattr(Path, "write_text", fail_config_write)
    monkeypatch.setattr(Path, "write_bytes", fail_rollback)
    with patch.object(
        config_commands, "print_error", wraps=config_commands.print_error
    ) as print_error:
        result = runner.invoke(app, ["set", "logging.level", "debug"])
    assert result.exit_code == 1, result.output
    print_error.assert_called_once()
    message = print_error.call_args.args[0]
    assert "candidate write failed" in message
    assert "rollback also failed" in message
    assert "cannot restore current config" in message
    assert "rolled back" not in message
    assert partial_content and partial_content != ORIGINAL
    _assert_files(config_dir, partial_content, prior_backup)
