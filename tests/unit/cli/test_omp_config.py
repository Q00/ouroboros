"""Tests for the optional OMP host configuration hook."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ouroboros.cli.omp_config import configure_omp_tool_call_timeout


def test_missing_omp_is_a_successful_noop() -> None:
    with patch("ouroboros.cli.omp_config.shutil.which", return_value=None):
        assert configure_omp_tool_call_timeout() is True


def test_configures_omp_with_sixty_second_deadline() -> None:
    current = MagicMock(returncode=0, stdout="30000\n")
    completed = MagicMock(returncode=0)
    with (
        patch("ouroboros.cli.omp_config.shutil.which", return_value="/bin/omp"),
        patch(
            "ouroboros.cli.omp_config.subprocess.run",
            side_effect=[current, completed],
        ) as run,
    ):
        assert configure_omp_tool_call_timeout() is True

    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == [
        "/bin/omp",
        "config",
        "get",
        "extensionHandlers.toolCallTimeoutMs",
    ]
    assert run.call_args_list[1].args[0] == [
        "/bin/omp",
        "config",
        "set",
        "extensionHandlers.toolCallTimeoutMs",
        "60000",
    ]


def test_preserves_higher_user_timeout() -> None:
    current = MagicMock(returncode=0, stdout="120000\n")
    with (
        patch("ouroboros.cli.omp_config.shutil.which", return_value="/bin/omp"),
        patch("ouroboros.cli.omp_config.subprocess.run", return_value=current) as run,
    ):
        assert configure_omp_tool_call_timeout() is True

    run.assert_called_once()


def test_preserves_oversized_decimal_timeout() -> None:
    current = MagicMock(returncode=0, stdout="9223372036854775808\n")
    with (
        patch("ouroboros.cli.omp_config.shutil.which", return_value="/bin/omp"),
        patch("ouroboros.cli.omp_config.subprocess.run", return_value=current) as run,
    ):
        assert configure_omp_tool_call_timeout() is True

    run.assert_called_once()


def test_dry_run_previews_without_mutating_omp(capsys) -> None:
    current = MagicMock(returncode=0, stdout="30000\n")
    with (
        patch("ouroboros.cli.omp_config.shutil.which", return_value="/bin/omp"),
        patch("ouroboros.cli.omp_config.subprocess.run", return_value=current) as run,
    ):
        assert configure_omp_tool_call_timeout(dry_run=True) is True

    run.assert_called_once()
    assert (
        "Would run: /bin/omp config set extensionHandlers.toolCallTimeoutMs 60000"
        in capsys.readouterr().out
    )
