"""Tests for Stage 1 mechanical verification."""

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.evaluation.mechanical import (
    CommandResult,
    MechanicalConfig,
    MechanicalVerifier,
    parse_coverage_from_output,
    run_command,
    run_mechanical_verification,
)
from ouroboros.evaluation.models import CheckType


class TestParseCoverageFromOutput:
    """Tests for coverage parsing."""

    def test_parse_total_percentage(self) -> None:
        """Parse coverage from TOTAL line."""
        output = """
        src/module.py     100      10    90%
        TOTAL            1000     100    90%
        """
        assert parse_coverage_from_output(output) == 0.90

    def test_parse_100_percent(self) -> None:
        """Parse 100% coverage."""
        output = "TOTAL   500   0   100%"
        assert parse_coverage_from_output(output) == 1.0

    def test_parse_0_percent(self) -> None:
        """Parse 0% coverage."""
        output = "TOTAL   500   500   0%"
        assert parse_coverage_from_output(output) == 0.0

    def test_parse_coverage_alternative_format(self) -> None:
        """Parse alternative Coverage: XX% format."""
        output = "Coverage: 85.5%"
        result = parse_coverage_from_output(output)
        assert result is not None
        assert abs(result - 0.855) < 0.001

    def test_parse_no_coverage_found(self) -> None:
        """Return None when no coverage found."""
        output = "All tests passed"
        assert parse_coverage_from_output(output) is None


class TestMechanicalConfig:
    """Tests for MechanicalConfig."""

    def test_default_values(self) -> None:
        """Verify default configuration values."""
        config = MechanicalConfig()
        assert config.coverage_threshold == 0.7
        assert config.timeout_seconds == 300
        assert config.working_dir is None

    def test_custom_values(self) -> None:
        """Create config with custom values."""
        from pathlib import Path

        config = MechanicalConfig(
            coverage_threshold=0.8,
            timeout_seconds=60,
            working_dir=Path("/tmp"),
        )
        assert config.coverage_threshold == 0.8
        assert config.timeout_seconds == 60
        assert config.working_dir == Path("/tmp")


class TestCommandResult:
    """Tests for CommandResult."""

    def test_creation(self) -> None:
        """Create CommandResult."""
        result = CommandResult(
            return_code=0,
            stdout="output",
            stderr="",
        )
        assert result.return_code == 0
        assert result.timed_out is False

    def test_timeout(self) -> None:
        """Create timed out CommandResult."""
        result = CommandResult(
            return_code=-1,
            stdout="",
            stderr="Timeout",
            timed_out=True,
        )
        assert result.timed_out is True


class TestRunCommand:
    """Tests for run_command function."""

    @pytest.mark.asyncio
    async def test_successful_command(self) -> None:
        """Run successful command."""
        result = await run_command(("echo", "hello"), timeout=5)
        assert result.return_code == 0
        assert "hello" in result.stdout
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_failed_command(self) -> None:
        """Run failing command."""
        result = await run_command(("false",), timeout=5)
        assert result.return_code != 0

    @pytest.mark.asyncio
    async def test_command_not_found(self) -> None:
        """Handle command not found."""
        result = await run_command(("nonexistent_command_xyz",), timeout=5)
        assert result.return_code == -1
        assert "not found" in result.stderr.lower() or "Command not found" in result.stderr


class TestMechanicalVerifier:
    """Tests for MechanicalVerifier class."""

    @pytest.mark.asyncio
    async def test_verify_generates_events(self) -> None:
        """Verify generates start and complete events."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = CommandResult(0, "OK", "")

            verifier = MechanicalVerifier()
            result = await verifier.verify("exec-1", checks=[CheckType.LINT])

            assert result.is_ok
            mech_result, events = result.value
            assert len(events) == 2
            assert events[0].type == "evaluation.stage1.started"
            assert events[1].type == "evaluation.stage1.completed"

    @pytest.mark.asyncio
    async def test_verify_all_pass(self) -> None:
        """All checks passing."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = CommandResult(0, "All OK", "")

            verifier = MechanicalVerifier()
            result = await verifier.verify(
                "exec-1",
                checks=[CheckType.LINT, CheckType.BUILD],
            )

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is True
            assert len(mech_result.checks) == 2
            assert all(c.passed for c in mech_result.checks)

    @pytest.mark.asyncio
    async def test_verify_one_fails(self) -> None:
        """One check failing marks overall as failed."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            # First call passes, second fails
            mock_run.side_effect = [
                CommandResult(0, "OK", ""),
                CommandResult(1, "", "Error"),
            ]

            verifier = MechanicalVerifier()
            result = await verifier.verify(
                "exec-1",
                checks=[CheckType.LINT, CheckType.BUILD],
            )

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is False
            assert len(mech_result.failed_checks) == 1

    @pytest.mark.asyncio
    async def test_verify_coverage_below_threshold(self) -> None:
        """Coverage below threshold marks check as failed."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            # Return coverage output with 50% (below 70% threshold)
            mock_run.return_value = CommandResult(
                0,
                "TOTAL   100   50   50%",
                "",
            )

            config = MechanicalConfig(coverage_threshold=0.7)
            verifier = MechanicalVerifier(config)
            result = await verifier.verify("exec-1", checks=[CheckType.COVERAGE])

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is False
            assert mech_result.coverage_score == 0.5

    @pytest.mark.asyncio
    async def test_verify_coverage_above_threshold(self) -> None:
        """Coverage above threshold passes."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = CommandResult(
                0,
                "TOTAL   100   20   80%",
                "",
            )

            config = MechanicalConfig(coverage_threshold=0.7)
            verifier = MechanicalVerifier(config)
            result = await verifier.verify("exec-1", checks=[CheckType.COVERAGE])

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is True
            assert mech_result.coverage_score == 0.8

    @pytest.mark.asyncio
    async def test_verify_timeout_handling(self) -> None:
        """Timeout is handled gracefully."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = CommandResult(
                -1,
                "",
                "Command timed out",
                timed_out=True,
            )

            verifier = MechanicalVerifier()
            result = await verifier.verify("exec-1", checks=[CheckType.TEST])

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is False
            assert "timed out" in mech_result.checks[0].message.lower()


class TestRunMechanicalVerification:
    """Tests for convenience function."""

    @pytest.mark.asyncio
    async def test_convenience_function(self) -> None:
        """Test the convenience function works."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = CommandResult(0, "OK", "")

            result = await run_mechanical_verification(
                "exec-1",
                checks=[CheckType.LINT],
            )

            assert result.is_ok
