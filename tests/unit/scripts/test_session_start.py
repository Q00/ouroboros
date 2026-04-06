"""Tests for the SessionStart hook script."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scripts" / "session-start.py"
_spec = importlib.util.spec_from_file_location("session_start", str(_SCRIPT_PATH))
session_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_start)


class TestSessionStartMain:
    """Regression coverage for safe SessionStart output handling."""

    def test_no_update_keeps_stdout_silent(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            session_start,
            "_load_version_checker",
            lambda: SimpleNamespace(
                check_update=lambda: {
                    "update_available": False,
                    "message": None,
                }
            ),
        )

        session_start.main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_update_notice_goes_to_stderr(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            session_start,
            "_load_version_checker",
            lambda: SimpleNamespace(
                check_update=lambda: {
                    "update_available": True,
                    "message": "Ouroboros update available",
                }
            ),
        )

        session_start.main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Ouroboros update available\n"

    def test_loader_failure_reports_stderr(self, monkeypatch, capsys) -> None:
        def _raise() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(session_start, "_load_version_checker", _raise)

        session_start.main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ouroboros: update check failed: boom" in captured.err

    def test_check_update_raises_reports_stderr(self, monkeypatch, capsys) -> None:
        """check_update() raising after module loads is also caught."""

        def _exploding_check():
            raise ConnectionError("network down")

        monkeypatch.setattr(
            session_start,
            "_load_version_checker",
            lambda: SimpleNamespace(check_update=_exploding_check),
        )

        session_start.main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ouroboros: update check failed: network down" in captured.err

    def test_malformed_result_keeps_silent(self, monkeypatch, capsys) -> None:
        """check_update() returning None or non-dict must not crash."""
        monkeypatch.setattr(
            session_start,
            "_load_version_checker",
            lambda: SimpleNamespace(check_update=lambda: None),
        )

        session_start.main()

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_result_missing_keys_keeps_silent(self, monkeypatch, capsys) -> None:
        """Result dict without expected keys stays silent (no update)."""
        monkeypatch.setattr(
            session_start,
            "_load_version_checker",
            lambda: SimpleNamespace(check_update=lambda: {}),
        )

        session_start.main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
