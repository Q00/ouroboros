from collections.abc import Iterator
import json
from pathlib import Path

import pytest

from ouroboros.observability.logging import (
    LoggingConfig,
    LogMode,
    configure_logging,
    get_logger,
    reset_logging,
    set_console_logging,
)


@pytest.fixture(autouse=True)
def logging_state() -> Iterator[None]:
    reset_logging()
    set_console_logging(True)
    yield
    reset_logging()
    set_console_logging(True)


@pytest.mark.parametrize("mode", [LogMode.PROD, LogMode.DEV])
@pytest.mark.parametrize("sink", ["console", "file"])
def test_structured_exception_secrets_are_removed_before_rendering(
    mode: LogMode, sink: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Given: numeric and byte credentials do not look like credential strings.
    configure_logging(LoggingConfig(mode=mode, log_dir=tmp_path))
    payload = {"api_key": 1234567890123456, "password": b"plainsecretabc", "attempt": 7}

    # When: the public logger renders the actual exception through both sinks.
    try:
        raise ValueError(payload)
    except ValueError:
        get_logger().exception("mapping.exception")

    # Then: both sinks retain diagnostics without credential values.
    console = capsys.readouterr().err
    persistent = (tmp_path / "ouroboros.log").read_text(encoding="utf-8")
    output = console if sink == "console" else persistent
    assert "1234567890123456" not in output
    assert "plainsecretabc" not in output
    assert "ValueError" in output
    assert "api_key" in output
    assert "password" in output
    assert "attempt" in output
    assert "test_exception_logging.py" in output
    assert "test_structured_exception_secrets_are_removed_before_rendering" in output
    if mode == LogMode.PROD:
        assert json.loads(output)["event"] == "mapping.exception"


@pytest.mark.parametrize("relation", ["cause", "context", "group"])
def test_nested_exceptions_keep_diagnostics_without_secrets(
    relation: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    configure_logging(LoggingConfig(mode=LogMode.PROD, log_dir=tmp_path))
    payload = {"password": b"nestedsecretabc", "detail": "retry failed"}
    try:
        try:
            raise ValueError(payload)
        except ValueError as cause:
            match relation:
                case "cause":
                    raise RuntimeError("operation failed") from cause
                case "context":
                    raise RuntimeError("operation failed")
                case "group":
                    raise ExceptionGroup("operation failed", [cause]) from None
    except (RuntimeError, ExceptionGroup):
        get_logger().exception("nested.exception")

    for output in (
        capsys.readouterr().err,
        (tmp_path / "ouroboros.log").read_text(encoding="utf-8"),
    ):
        exception = json.loads(output)["exception"]
        assert "nestedsecretabc" not in exception
        assert "retry failed" in exception
        assert "operation failed" in exception
        assert "ValueError" in exception
        assert "test_nested_exceptions_keep_diagnostics_without_secrets" in exception


def test_custom_exception_string_cannot_reintroduce_credentials(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class CustomError(Exception):
        def __str__(self) -> str:
            return "plainsecretabc"

    configure_logging(LoggingConfig(mode=LogMode.PROD, log_dir=tmp_path))
    try:
        raise CustomError({"password": b"plainsecretabc", "detail": "retry failed"})
    except CustomError:
        get_logger().exception("custom.exception")

    for output in (
        capsys.readouterr().err,
        (tmp_path / "ouroboros.log").read_text(encoding="utf-8"),
    ):
        exception = json.loads(output)["exception"]
        assert "plainsecretabc" not in exception
        assert "retry failed" in exception
        assert "CustomError" in exception
        assert "test_custom_exception_string_cannot_reintroduce_credentials" in exception


def test_preformatted_exception_text_retains_existing_redaction(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    configure_logging(LoggingConfig(mode=LogMode.PROD, log_dir=tmp_path))
    message = str({"api_key": "plainsecretabc", "detail": "retry failed"})
    try:
        raise ValueError(message)
    except ValueError:
        get_logger().exception("preformatted.exception")

    for output in (
        capsys.readouterr().err,
        (tmp_path / "ouroboros.log").read_text(encoding="utf-8"),
    ):
        exception = json.loads(output)["exception"]
        assert "plainsecretabc" not in exception
        assert "ValueError" in exception


@pytest.mark.parametrize("mode", [LogMode.PROD, LogMode.DEV])
@pytest.mark.parametrize("source", ["argument", "note"])
@pytest.mark.parametrize("encoding", ["repr", "json"])
@pytest.mark.parametrize(
    ("field", "sensitive"),
    [
        ("private_key", True),
        ("refresh_token", True),
        ("credentials", True),
        ("X-PRIVATE-KEY", True),
        ("upstream_auth", True),
        ("bearer", True),
        ("detail", False),
    ],
)
def test_preformatted_fields_preserve_canonical_redaction_in_public_sinks(
    mode: LogMode,
    source: str,
    encoding: str,
    field: str,
    sensitive: bool,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Given: the same ordinary-looking value is sensitive only under its field name.
    configure_logging(LoggingConfig(mode=mode, log_dir=tmp_path))
    payload = {field: "plainsecretabc", "diagnostic": "retry remains available"}
    message = repr(payload) if encoding == "repr" else json.dumps(payload)
    error = ValueError(message if source == "argument" else "operation failed")
    error.add_note("retry remains available")
    if source == "note":
        error.add_note(message)

    # When: the public logger renders the preformatted argument or note.
    try:
        raise error
    except ValueError:
        get_logger().exception("canonical.exception")

    # Then: both real sinks mask sensitive fields and preserve safe diagnostics.
    for output in (
        capsys.readouterr().err,
        (tmp_path / "ouroboros.log").read_text(encoding="utf-8"),
    ):
        assert ("plainsecretabc" not in output) is sensitive
        assert "retry remains available" in output
        assert "ValueError" in output
        assert "test_preformatted_fields_preserve_canonical_redaction_in_public_sinks" in output


@pytest.mark.parametrize("mode", [LogMode.PROD, LogMode.DEV])
def test_nested_preformatted_field_is_not_hidden_by_safe_outer_field(
    mode: LogMode, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    configure_logging(LoggingConfig(mode=mode, log_dir=tmp_path))
    nested = str({"api_key": "plainsecretabc", "diagnostic": "retry remains available"})
    try:
        raise ValueError(str({"detail": nested}))
    except ValueError:
        get_logger().exception("nested.preformatted")

    for output in (
        capsys.readouterr().err,
        (tmp_path / "ouroboros.log").read_text(encoding="utf-8"),
    ):
        assert "plainsecretabc" not in output
        assert "retry remains available" in output
        assert "ValueError" in output


@pytest.mark.parametrize("mode", [LogMode.PROD, LogMode.DEV])
@pytest.mark.parametrize("sink", ["console", "file"])
@pytest.mark.parametrize("source", ["key", "note", "text_key", "note_key"])
def test_exception_keys_and_notes_cannot_bypass_redaction(
    mode: LogMode,
    sink: str,
    source: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    configure_logging(LoggingConfig(mode=mode, log_dir=tmp_path))
    credential = "plainsecretabc" if source == "note" else "sk-live-abc123def456ghi789"
    payload = {"api_key": "privatevalue", "password": "privatevalue", "attempt": 7}
    if source == "key":
        payload[f"api_key_{credential}"] = 123
    error = ValueError(str({credential: "paired-value"}) if source == "text_key" else payload)
    error.add_note("retry remains available")
    if source == "note":
        error.add_note(str({"api_key": credential, "detail": "upstream unavailable"}))
    if source == "note_key":
        error.add_note(str({credential: "paired-value"}))
    try:
        raise error
    except ValueError:
        get_logger().exception("review.exception")

    console = capsys.readouterr().err
    (tmp_path / "console.log").write_text(console, encoding="utf-8")
    persistent = (tmp_path / "ouroboros.log").read_text(encoding="utf-8")
    output = console if sink == "console" else persistent
    assert credential not in output
    assert "privatevalue" not in output
    if source != "text_key":
        assert "api_key" in output
        assert "password" in output
        assert "attempt" in output
    assert "ValueError" in output
    assert "retry remains available" in output
    assert "test_exception_keys_and_notes_cannot_bypass_redaction" in output
    if source == "note":
        assert "upstream unavailable" in output
