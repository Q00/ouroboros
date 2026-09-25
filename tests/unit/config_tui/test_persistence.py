"""Tests for the validated batch write path (#1413).

The contract under test: writes share `config set`'s key validator, pass
the full Pydantic load check after writing, and roll back byte-for-byte on
validation failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ouroboros.config_tui import persistence


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(persistence, "get_config_dir", lambda: tmp_path)
    # The post-write validation reads the same canonical path.
    from ouroboros.config import loader as config_loader
    from ouroboros.config import models as config_models

    monkeypatch.setattr(config_models, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(config_loader, "get_config_dir", lambda: tmp_path)
    return tmp_path


def _read(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "config.yaml").read_text()) or {}


def test_apply_valid_values_persists(config_dir: Path) -> None:
    persistence.apply_config_values(
        {
            "orchestrator.runtime_backend": "codex",
            "orchestrator.runtime_profile.stages.execute": "hermes",
            "clarification.default_model": "my-model",
        }
    )
    data = _read(config_dir)
    assert data["orchestrator"]["runtime_backend"] == "codex"
    assert data["orchestrator"]["runtime_profile"]["stages"]["execute"] == "hermes"
    assert data["clarification"]["default_model"] == "my-model"


def test_apply_nested_value_promotes_null_section(config_dir: Path) -> None:
    (config_dir / "config.yaml").write_text(
        yaml.dump(
            {
                "orchestrator": {
                    "runtime_backend": "codex",
                    "runtime_profile": None,
                }
            },
            sort_keys=False,
        )
    )

    persistence.apply_config_values(
        {
            "orchestrator.runtime_profile.stages.interview": "codex",
            "clarification.default_model": "my-model",
        }
    )

    data = _read(config_dir)
    assert data["orchestrator"]["runtime_profile"] == {"stages": {"interview": "codex"}}
    assert data["clarification"]["default_model"] == "my-model"


def test_apply_nested_value_rejects_scalar_section(config_dir: Path) -> None:
    original = "orchestrator:\n  runtime_profile: worker\n"
    (config_dir / "config.yaml").write_text(original)

    with pytest.raises(
        persistence.ConfigWriteError,
        match=r"'runtime_profile' is not a section",
    ):
        persistence.apply_config_values({"orchestrator.runtime_profile.stages.interview": "codex"})

    assert (config_dir / "config.yaml").read_text() == original


def test_apply_none_deletes_stage_override(config_dir: Path) -> None:
    persistence.apply_config_values({"orchestrator.runtime_profile.stages.execute": "codex"})
    persistence.apply_config_values({"orchestrator.runtime_profile.stages.execute": None})
    data = _read(config_dir)
    assert "execute" not in data["orchestrator"]["runtime_profile"]["stages"]


def test_unknown_key_rejected_without_writing(config_dir: Path) -> None:
    with pytest.raises(persistence.ConfigWriteError, match="Unknown config key"):
        persistence.apply_config_values({"orchestrator.not_a_real_key_xyz": "value"})
    assert not (config_dir / "config.yaml").exists()


def test_unknown_runtime_profile_child_rejected_when_section_is_null(config_dir: Path) -> None:
    original = "orchestrator:\n  runtime_profile: null\n"
    (config_dir / "config.yaml").write_text(original)

    with pytest.raises(persistence.ConfigWriteError, match="Unknown config key"):
        persistence.apply_config_values(
            {"orchestrator.runtime_profile.not_a_real_key_xyz": "value"}
        )

    assert (config_dir / "config.yaml").read_text() == original


def test_invalid_value_rolls_back_file(config_dir: Path) -> None:
    persistence.apply_config_values({"orchestrator.runtime_backend": "codex"})
    before = (config_dir / "config.yaml").read_text()

    with pytest.raises(persistence.ConfigWriteError, match="rolled back"):
        persistence.apply_config_values({"orchestrator.runtime_backend": "not-a-backend"})

    assert (config_dir / "config.yaml").read_text() == before


def test_invalid_stage_backend_rolls_back(config_dir: Path) -> None:
    # The key path is structurally valid (validator cannot drill past the
    # Optional runtime_profile), so rejection must come from the Pydantic
    # load check — proving the post-write gate carries real weight.
    with pytest.raises(persistence.ConfigWriteError, match="rolled back"):
        persistence.apply_config_values(
            {"orchestrator.runtime_profile.stages.execute": "not-a-backend"}
        )
    assert not (config_dir / "config.yaml").exists()


def test_invalid_value_on_fresh_file_removes_it(config_dir: Path) -> None:
    with pytest.raises(persistence.ConfigWriteError):
        persistence.apply_config_values({"llm.backend": "not-a-backend"})
    assert not (config_dir / "config.yaml").exists()


def test_empty_batch_is_noop(config_dir: Path) -> None:
    persistence.apply_config_values({})
    assert not (config_dir / "config.yaml").exists()


def test_load_raw_config_missing_file_returns_empty(config_dir: Path) -> None:
    assert persistence.load_raw_config() == {}


def test_apply_writes_backup_for_undo(config_dir: Path) -> None:
    persistence.apply_config_values({"orchestrator.runtime_backend": "codex"})
    before = (config_dir / "config.yaml").read_text()
    persistence.apply_config_values({"orchestrator.runtime_backend": "hermes"})
    assert (config_dir / "config.yaml.bak").read_text() == before


@pytest.fixture
def cp949_config_files(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real CP949 text I/O for fixture files on every CI platform."""
    original_open = Path.open
    original_read_text = Path.read_text
    original_write_text = Path.write_text

    def locale_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if path.is_relative_to(config_dir) and "b" not in mode and encoding in (None, "locale"):
            encoding = "cp949"
        return original_open(
            path, mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline
        )

    # pathlib can resolve an omitted encoding to UTF-8 before calling open
    # when the interpreter's UTF-8 mode is enabled. Preserve the distinction
    # between omitted and explicit UTF-8 for these fixture paths only.
    def locale_read_text(path, encoding=None, errors=None, **kwargs):
        if path.is_relative_to(config_dir) and encoding is None:
            encoding = "locale"
        return original_read_text(path, encoding=encoding, errors=errors, **kwargs)

    def locale_write_text(path, data, encoding=None, errors=None, **kwargs):
        if path.is_relative_to(config_dir) and encoding is None:
            encoding = "locale"
        return original_write_text(path, data, encoding=encoding, errors=errors, **kwargs)

    monkeypatch.setattr(Path, "open", locale_open)
    monkeypatch.setattr(Path, "read_text", locale_read_text)
    monkeypatch.setattr(Path, "write_text", locale_write_text)


def _utf8_config(worktree_root: str, *, newline: str = "\n", bom: bool = False) -> bytes:
    content = (
        "# Preserve this comment in backups and rollback.\n"
        "orchestrator:\n"
        f"  worktree_root: '{worktree_root}'\n"
        "logging:\n"
        "  level: info\n"
    )
    encoded = content.replace("\n", newline).encode("utf-8")
    return b"\xef\xbb\xbf" + encoded if bom else encoded


@pytest.mark.usefixtures("cp949_config_files")
@pytest.mark.parametrize(
    ("worktree_root", "bom"),
    [
        pytest.param("C:/workspace/문서", False, id="korean-mojibake"),
        pytest.param("C:/workspace/문서한😀", False, id="korean-emoji-decode-error"),
        pytest.param("C:/workspace/문서", True, id="utf8-bom"),
    ],
)
def test_load_raw_config_preserves_utf8_under_cp949(
    config_dir: Path, worktree_root: str, bom: bool
) -> None:
    (config_dir / "config.yaml").write_bytes(_utf8_config(worktree_root, bom=bom))

    data = persistence.load_raw_config()

    assert data["orchestrator"]["worktree_root"] == worktree_root
    assert data["logging"]["level"] == "info"


@pytest.mark.usefixtures("cp949_config_files")
@pytest.mark.parametrize(
    ("worktree_root", "newline", "bom"),
    [
        pytest.param("C:/workspace/문서", "\n", False, id="korean-lf"),
        pytest.param("C:/workspace/문서한😀", "\r\n", True, id="emoji-bom-crlf"),
    ],
)
def test_apply_preserves_unrelated_utf8_value_and_backup_bytes(
    config_dir: Path, worktree_root: str, newline: str, bom: bool
) -> None:
    from ouroboros.config.loader import load_config

    original = _utf8_config(worktree_root, newline=newline, bom=bom)
    config_path = config_dir / "config.yaml"
    config_path.write_bytes(original)

    persistence.apply_config_values({"logging.level": "debug"})

    # Decode the stored bytes independently of the locale and raw reader.
    saved = yaml.safe_load(config_path.read_bytes().decode("utf-8"))
    assert saved["orchestrator"]["worktree_root"] == worktree_root
    assert saved["logging"]["level"] == "debug"
    validated = load_config(config_path)
    assert validated.orchestrator.worktree_root == str(Path(worktree_root))
    assert validated.logging.level == "debug"
    assert (config_dir / "config.yaml.bak").read_bytes() == original


@pytest.mark.usefixtures("cp949_config_files")
@pytest.mark.parametrize(
    ("worktree_root", "newline", "bom"),
    [
        pytest.param("C:/workspace/문서", "\n", False, id="korean-lf"),
        pytest.param("C:/workspace/문서한😀", "\r\n", True, id="emoji-bom-crlf"),
    ],
)
def test_invalid_value_restores_original_utf8_bytes_under_cp949(
    config_dir: Path, worktree_root: str, newline: str, bom: bool
) -> None:
    original = _utf8_config(worktree_root, newline=newline, bom=bom)
    config_path = config_dir / "config.yaml"
    config_path.write_bytes(original)

    with pytest.raises(persistence.ConfigWriteError, match="rolled back"):
        persistence.apply_config_values({"logging.level": "not-a-level"})

    assert config_path.read_bytes() == original
    assert (config_dir / "config.yaml.bak").read_bytes() == original


@pytest.mark.usefixtures("cp949_config_files")
@pytest.mark.parametrize("operation", ["read", "edit"])
def test_invalid_utf8_is_rejected_without_writes(config_dir: Path, operation: str) -> None:
    # A legacy CP949 file is decodable by the ambient locale but is outside
    # the canonical UTF-8 contract; it must not be silently transcoded.
    original = "orchestrator:\n  worktree_root: 'C:/workspace/문서'\n".encode("cp949")
    config_path = config_dir / "config.yaml"
    backup_path = config_dir / "config.yaml.bak"
    config_path.write_bytes(original)
    existing_backup = b"# previous backup\r\n{}\r\n"
    backup_path.write_bytes(existing_backup)

    with pytest.raises(persistence.ConfigWriteError):
        if operation == "read":
            persistence.load_raw_config()
        else:
            persistence.apply_config_values({"logging.level": "debug"})

    assert config_path.read_bytes() == original
    assert backup_path.read_bytes() == existing_backup
