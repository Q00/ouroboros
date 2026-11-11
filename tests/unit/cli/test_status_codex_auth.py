"""Codex health checks discover auth files without validating their contents."""

from pathlib import Path
import sys

import pytest

from ouroboros.cli.commands.status import _check_credentials, _codex_auth_file_exists


@pytest.fixture
def auth_homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    homes = {name: tmp_path / name for name in ("codex", "home", "default")}
    for name, home in homes.items():
        auth_dir = home if name == "codex" else home / ".codex"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text("{}", encoding="utf-8")
    for key in ("CODEX_HOME", "HOME", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("USERPROFILE", str(homes["default"]))
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setattr(Path, "home", lambda: homes["default"])
    return homes


@pytest.mark.parametrize("source", ["codex", "home", "default"])
@pytest.mark.parametrize("file_state", ["file", "missing", "directory"])
def test_codex_auth_respects_home_priority(
    monkeypatch: pytest.MonkeyPatch, auth_homes: dict[str, Path], source: str, file_state: str
) -> None:
    if source in {"codex", "home"}:
        monkeypatch.setenv("HOME", str(auth_homes["home"]))
    if source == "codex":
        monkeypatch.setenv("CODEX_HOME", str(auth_homes["codex"]))
    auth_dir = auth_homes[source] if source == "codex" else auth_homes[source] / ".codex"
    auth_file = auth_dir / "auth.json"
    if file_state != "file":
        auth_file.unlink()
        if file_state == "directory":
            auth_file.mkdir()

    assert _codex_auth_file_exists() is (file_state == "file")


@pytest.mark.parametrize("codex_home", [None, ""])
@pytest.mark.parametrize("home", [None, ""])
def test_codex_auth_empty_overrides_use_platform_home(
    monkeypatch: pytest.MonkeyPatch,
    auth_homes: dict[str, Path],
    codex_home: str | None,
    home: str | None,
) -> None:
    if codex_home is not None:
        monkeypatch.setenv("CODEX_HOME", codex_home)
    if home is not None:
        monkeypatch.setenv("HOME", home)

    assert _codex_auth_file_exists() is True


@pytest.mark.parametrize("api_key", ["", "test-key-do-not-display"])
def test_codex_health_handles_unresolvable_home(
    monkeypatch: pytest.MonkeyPatch, auth_homes: dict[str, Path], api_key: str
) -> None:
    def unavailable_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", unavailable_home)
    monkeypatch.setenv("OPENAI_API_KEY", api_key)

    assert _codex_auth_file_exists() is False
    row = _check_credentials({}, auth_homes["default"] / "config.yaml")
    assert row["status"] == ("ok" if api_key else "error")
    assert "test-key-do-not-display" not in str(row)


@pytest.mark.skipif(sys.platform != "win32", reason="Uses native Windows home resolution")
@pytest.mark.parametrize("codex_home", [None, ""])
@pytest.mark.parametrize("home", [None, ""])
def test_codex_auth_uses_native_userprofile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, codex_home: str | None, home: str | None
) -> None:
    profile = tmp_path / "사용자 home"
    auth_file = profile / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(profile))
    for key, value in (("CODEX_HOME", codex_home), ("HOME", home)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    assert Path.home() == profile
    assert _codex_auth_file_exists() is True
