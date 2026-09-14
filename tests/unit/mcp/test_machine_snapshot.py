"""Focused privacy and failure tests for the static machine snapshot."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ouroboros.mcp.machine_snapshot import collect_machine_snapshot


def test_snapshot_is_typed_and_has_bounded_categories():
    snapshot = collect_machine_snapshot()
    assert set(snapshot.to_dict()) == {
        "os",
        "architecture",
        "python",
        "executable",
        "package",
        "disk",
        "config_path",
    }
    assert all(probe["status"] in {"ok", "not_checked"} for probe in snapshot.to_dict().values())


def test_partial_failures_are_normalized_without_exception_text():
    with patch(
        "ouroboros.mcp.machine_snapshot.shutil.disk_usage", side_effect=PermissionError("SECRET")
    ):
        probe = collect_machine_snapshot().disk
    assert probe.status == "not_checked"
    assert probe.reason == "permission_denied"
    assert "SECRET" not in str(probe)


def test_config_symlink_is_classified_without_following_or_reading(tmp_path: Path):
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    config.symlink_to(tmp_path / "private-sentinel")
    with patch("ouroboros.mcp.machine_snapshot.Path.home", return_value=tmp_path):
        probe = collect_machine_snapshot().config_path
    assert probe.status == "ok"
    assert probe.value["kind"] == "symlink"
    assert "private-sentinel" not in str(probe.value)


def test_synthetic_macos_facts_are_safe():
    from types import SimpleNamespace

    info = SimpleNamespace(sysname="Darwin", release="24.0", version="synthetic", machine="arm64")
    with (
        patch("ouroboros.mcp.machine_snapshot.sys.platform", "darwin"),
        patch("ouroboros.mcp.machine_snapshot.os.uname", return_value=info),
    ):
        snapshot = collect_machine_snapshot()
    assert snapshot.os.value["system"] == "Darwin"
    assert snapshot.architecture.value["machine"] == "arm64"


def test_synthetic_windows_uses_no_platform_shell_fallback():
    from types import SimpleNamespace

    with (
        patch("ouroboros.mcp.machine_snapshot.sys.platform", "win32"),
        patch(
            "ouroboros.mcp.machine_snapshot.sys.getwindowsversion",
            create=True,
            return_value=SimpleNamespace(major=10, minor=0, build=26100),
        ),
        patch("ouroboros.mcp.machine_snapshot.os.uname", side_effect=AssertionError("not native")),
        patch("subprocess.check_output", side_effect=AssertionError("no commands")) as command,
    ):
        snapshot = collect_machine_snapshot()
    assert snapshot.os.value == {"system": "Windows", "release": "10.0", "version": "26100"}
    assert snapshot.architecture.status == "not_checked"
    assert snapshot.architecture.reason == "unsupported"
    command.assert_not_called()


def test_missing_config_is_not_checked(tmp_path: Path):
    with patch("ouroboros.mcp.machine_snapshot.Path.home", return_value=tmp_path):
        probe = collect_machine_snapshot().config_path
    assert probe.status == "not_checked"
    assert probe.reason == "missing"


def test_regular_config_is_metadata_only(tmp_path: Path):
    config = tmp_path / ".ouroboros" / "config.yaml"
    config.parent.mkdir()
    config.write_text("PRIVATE_CONFIG_SENTINEL")
    with (
        patch("ouroboros.mcp.machine_snapshot.Path.home", return_value=tmp_path),
        patch.object(Path, "read_text", side_effect=AssertionError("must not read content")),
    ):
        probe = collect_machine_snapshot().config_path
    assert probe.status == "ok"
    assert probe.value == {"path": str(config), "kind": "regular_file"}


def test_home_failure_does_not_abort_other_categories():
    with patch("ouroboros.mcp.machine_snapshot.Path.home", side_effect=RuntimeError("SECRET_HOME")):
        snapshot = collect_machine_snapshot()
    assert snapshot.config_path.reason == "unexpected_failure"
    assert snapshot.disk.reason == "unexpected_failure"
    assert snapshot.python.status == "ok"
    assert "SECRET_HOME" not in str(snapshot.to_dict())


def test_unsupported_and_missing_package_have_explicit_reasons():
    from importlib.metadata import PackageNotFoundError

    with (
        patch(
            "ouroboros.mcp.machine_snapshot.shutil.disk_usage",
            side_effect=NotImplementedError("SECRET"),
        ),
        patch(
            "ouroboros.mcp.machine_snapshot.importlib.metadata.distribution",
            side_effect=PackageNotFoundError("ouroboros-ai"),
        ),
    ):
        snapshot = collect_machine_snapshot()
    assert snapshot.disk.reason == "unsupported"
    assert snapshot.package.reason == "missing"
    assert "SECRET" not in str(snapshot.to_dict())


def test_empty_interpreter_path_is_not_a_successful_current_directory():
    with patch("ouroboros.mcp.machine_snapshot.sys.executable", ""):
        probe = collect_machine_snapshot().executable
    assert probe.status == "not_checked"
    assert probe.reason == "missing"


def test_package_unsupported_uses_standard_reason():
    with patch(
        "ouroboros.mcp.machine_snapshot.importlib.metadata.distribution",
        side_effect=NotImplementedError("SECRET"),
    ):
        probe = collect_machine_snapshot().package
    assert probe.status == "not_checked"
    assert probe.reason == "unsupported"
