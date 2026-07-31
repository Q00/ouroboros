"""Tests for Windows process-boundary stream normalization."""

from __future__ import annotations

from io import StringIO
import sys
from typing import Any

from typer.main import get_command
from typer.testing import CliRunner

from ouroboros.cli import streams
from ouroboros.cli.formatters import console
from ouroboros.cli.main import app


class _ReconfigurableStream(StringIO):
    def __init__(self, encoding: str, *, fail: bool = False) -> None:
        super().__init__()
        self._encoding = encoding
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    @property
    def encoding(self) -> str:
        return self._encoding

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise ValueError("detached")
        self._encoding = kwargs["encoding"]


class _EncodingOnlyStream(_ReconfigurableStream):
    def reconfigure(self, *, encoding: str) -> None:
        self.calls.append({"encoding": encoding})
        self._encoding = encoding


class _OpaqueEncodingStream(StringIO):
    @property
    def encoding(self) -> str:
        raise ValueError("detached")


class _OpaqueReconfigureStream(StringIO):
    @property
    def encoding(self) -> str:
        return "cp949"

    @property
    def reconfigure(self) -> Any:
        raise OSError("host-owned")


class _UnsupportedReconfigureStream(_ReconfigurableStream):
    def reconfigure(self, **kwargs: str) -> None:
        raise NotImplementedError("reconfiguration is unsupported")


def test_legacy_windows_streams_are_reconfigured_in_place(monkeypatch: Any) -> None:
    stdout = _ReconfigurableStream("cp949")
    stderr = _ReconfigurableStream("cp932")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    streams.normalize_windows_standard_streams()

    expected = [{"encoding": "utf-8", "errors": "replace"}]
    assert stdout.calls == expected
    assert stderr.calls == expected
    assert sys.stdout is stdout
    assert sys.stderr is stderr


def test_utf8_and_host_owned_streams_are_left_untouched(monkeypatch: Any) -> None:
    stdout = _ReconfigurableStream("cp65001")
    stderr = StringIO()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    streams.normalize_windows_standard_streams()

    assert stdout.calls == []
    assert sys.stderr is stderr


def test_failed_reconfigure_preserves_embedded_stream(monkeypatch: Any) -> None:
    stdout = _ReconfigurableStream("cp936", fail=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)

    streams.normalize_windows_standard_streams()

    assert sys.stdout is stdout


def test_opaque_capability_properties_are_treated_as_unavailable(monkeypatch: Any) -> None:
    stdout = _OpaqueEncodingStream()
    stderr = _OpaqueReconfigureStream()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    streams.normalize_windows_standard_streams()

    assert sys.stdout is stdout
    assert sys.stderr is stderr


def test_root_entry_tolerates_opaque_encoding_property(monkeypatch: Any) -> None:
    stdout = _OpaqueEncodingStream()
    messages: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(console, "print", lambda message: messages.append(str(message)))

    get_command(app).main(args=["--version"], prog_name="ouroboros", standalone_mode=False)

    assert any("Ouroboros" in message for message in messages)


def test_root_entry_tolerates_unsupported_reconfigure(monkeypatch: Any) -> None:
    stdout = _UnsupportedReconfigureStream("cp949")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)

    get_command(app).main(args=["--version"], prog_name="ouroboros", standalone_mode=False)

    assert "Ouroboros" in stdout.getvalue()


def test_shared_console_resolves_replaced_stdout_dynamically(monkeypatch: Any) -> None:
    replacement = StringIO()
    monkeypatch.setattr(sys, "stdout", replacement)

    console.print("✓ → Unicode")

    assert "✓ → Unicode" in replacement.getvalue()


def test_cli_runner_captures_eager_version_output_on_windows(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "Ouroboros" in result.stdout


def test_root_entry_tolerates_host_reconfigure_signature(monkeypatch: Any) -> None:
    """A narrower embedded stream API cannot abort Click before dispatch."""
    stdout = _EncodingOnlyStream("cp949")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)

    get_command(app).main(args=["--version"], prog_name="ouroboros", standalone_mode=False)

    assert "Ouroboros" in stdout.getvalue()
