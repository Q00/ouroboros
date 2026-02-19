"""Stage 1: Mechanical Verification.

Zero-cost verification through automated checks:
- Lint: Code style and formatting
- Build: Compilation validation
- Test: Unit/integration test execution
- Static: Static analysis (type checking)
- Coverage: Test coverage threshold (NFR9 >= 0.7)

The MechanicalVerifier is stateless and produces immutable results.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.evaluation.models import CheckResult, CheckType, MechanicalResult
from ouroboros.events.base import BaseEvent
from ouroboros.events.evaluation import (
    create_stage1_completed_event,
    create_stage1_started_event,
)


@dataclass(frozen=True, slots=True)
class MechanicalConfig:
    """Configuration for mechanical verification.

    Attributes:
        coverage_threshold: Minimum coverage required (default 0.7 per NFR9)
        lint_command: Command to run linting
        build_command: Command to run build
        test_command: Command to run tests
        static_command: Command to run static analysis
        timeout_seconds: Timeout for each command
        working_dir: Working directory for commands
    """

    coverage_threshold: float = 0.7
    lint_command: tuple[str, ...] = ("uv", "run", "ruff", "check", ".")
    build_command: tuple[str, ...] = ("uv", "run", "python", "-m", "py_compile")
    test_command: tuple[str, ...] = ("uv", "run", "pytest", "--tb=short", "-q")
    static_command: tuple[str, ...] = (
        "uv",
        "run",
        "mypy",
        "src/ouroboros",
        "--ignore-missing-imports",
    )
    coverage_command: tuple[str, ...] = (
        "uv",
        "run",
        "pytest",
        "--cov=src/ouroboros",
        "--cov-report=term-missing",
        "-q",
    )
    timeout_seconds: int = 300
    working_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of running a shell command.

    Attributes:
        return_code: Exit code of the command
        stdout: Standard output
        stderr: Standard error
        timed_out: Whether the command timed out
    """

    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def run_command(
    command: tuple[str, ...],
    timeout: int,
    working_dir: Path | None = None,
) -> CommandResult:
    """Run a shell command asynchronously.

    Args:
        command: Command and arguments to run
        timeout: Timeout in seconds
        working_dir: Working directory

    Returns:
        CommandResult with output and status
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
            return CommandResult(
                return_code=process.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(
                return_code=-1,
                stdout="",
                stderr="Command timed out",
                timed_out=True,
            )
    except FileNotFoundError as e:
        return CommandResult(
            return_code=-1,
            stdout="",
            stderr=f"Command not found: {e}",
        )
    except OSError as e:
        return CommandResult(
            return_code=-1,
            stdout="",
            stderr=f"OS error: {e}",
        )


def parse_coverage_from_output(output: str) -> float | None:
    """Extract coverage percentage from pytest-cov output.

    Args:
        output: stdout from coverage command

    Returns:
        Coverage as float (0.0-1.0) or None if not found
    """
    # Look for "TOTAL ... XX%" pattern
    import re

    # Pattern matches lines like "TOTAL   1234   123  90%"
    pattern = r"TOTAL\s+\d+\s+\d+\s+(\d+)%"
    match = re.search(pattern, output)
    if match:
        return float(match.group(1)) / 100.0

    # Alternative pattern: "Coverage: XX%"
    alt_pattern = r"Coverage:\s*(\d+(?:\.\d+)?)%"
    alt_match = re.search(alt_pattern, output)
    if alt_match:
        return float(alt_match.group(1)) / 100.0

    return None


class MechanicalVerifier:
    """Stage 1 mechanical verification executor.

    Runs zero-cost automated checks on artifacts.
    Stateless - all state passed via parameters.

    Example:
        verifier = MechanicalVerifier(config)
        result = await verifier.verify(execution_id, checks=[CheckType.LINT, CheckType.TEST])
    """

    def __init__(self, config: MechanicalConfig | None = None) -> None:
        """Initialize verifier with configuration.

        Args:
            config: Verification configuration, uses defaults if None
        """
        self.config = config or MechanicalConfig()

    async def verify(
        self,
        execution_id: str,
        checks: list[CheckType] | None = None,
    ) -> Result[tuple[MechanicalResult, list[BaseEvent]], ValidationError]:
        """Run mechanical verification checks.

        Args:
            execution_id: Execution identifier for events
            checks: List of checks to run, defaults to all

        Returns:
            Result containing MechanicalResult and events, or error
        """
        if checks is None:
            checks = list(CheckType)

        events: list[BaseEvent] = []
        check_results: list[CheckResult] = []
        coverage_score: float | None = None

        # Emit start event
        events.append(
            create_stage1_started_event(
                execution_id=execution_id,
                checks_to_run=[c.value for c in checks],
            )
        )

        # Run each check
        for check_type in checks:
            result = await self._run_check(check_type)
            check_results.append(result)

            # Track coverage if it was a coverage check
            if check_type == CheckType.COVERAGE and result.passed:
                coverage_score = result.details.get("coverage_score")

        # Determine overall pass/fail
        all_passed = all(c.passed for c in check_results)

        # Verify coverage threshold if coverage was checked
        if coverage_score is not None and coverage_score < self.config.coverage_threshold:
            # Find and update coverage check to failed
            updated_results = []
            for cr in check_results:
                if cr.check_type == CheckType.COVERAGE:
                    updated_results.append(
                        CheckResult(
                            check_type=CheckType.COVERAGE,
                            passed=False,
                            message=f"Coverage {coverage_score:.1%} below threshold {self.config.coverage_threshold:.1%}",
                            details=cr.details,
                        )
                    )
                else:
                    updated_results.append(cr)
            check_results = updated_results
            all_passed = False

        mechanical_result = MechanicalResult(
            passed=all_passed,
            checks=tuple(check_results),
            coverage_score=coverage_score,
        )

        # Emit completion event
        events.append(
            create_stage1_completed_event(
                execution_id=execution_id,
                passed=all_passed,
                checks=[
                    {
                        "check_type": c.check_type.value,
                        "passed": c.passed,
                        "message": c.message,
                    }
                    for c in check_results
                ],
                coverage_score=coverage_score,
            )
        )

        return Result.ok((mechanical_result, events))

    async def _run_check(self, check_type: CheckType) -> CheckResult:
        """Run a single check.

        Args:
            check_type: Type of check to run

        Returns:
            CheckResult with pass/fail status
        """
        command = self._get_command_for_check(check_type)
        if command is None:
            return CheckResult(
                check_type=check_type,
                passed=True,
                message=f"Check {check_type.value} skipped (no command configured)",
                details={"skipped": True},
            )

        cmd_result = await run_command(
            command,
            timeout=self.config.timeout_seconds,
            working_dir=self.config.working_dir,
        )

        if cmd_result.timed_out:
            return CheckResult(
                check_type=check_type,
                passed=False,
                message=f"Check {check_type.value} timed out after {self.config.timeout_seconds}s",
                details={"timed_out": True},
            )

        passed = cmd_result.return_code == 0
        details: dict[str, Any] = {
            "return_code": cmd_result.return_code,
            "stdout_preview": cmd_result.stdout[:500] if cmd_result.stdout else "",
            "stderr_preview": cmd_result.stderr[:500] if cmd_result.stderr else "",
        }

        # Extract coverage if this was a coverage check
        if check_type == CheckType.COVERAGE and passed:
            coverage = parse_coverage_from_output(cmd_result.stdout)
            if coverage is not None:
                details["coverage_score"] = coverage

        message = (
            f"Check {check_type.value} passed"
            if passed
            else f"Check {check_type.value} failed (exit code {cmd_result.return_code})"
        )

        return CheckResult(
            check_type=check_type,
            passed=passed,
            message=message,
            details=details,
        )

    def _get_command_for_check(self, check_type: CheckType) -> tuple[str, ...] | None:
        """Get the command for a specific check type.

        Args:
            check_type: Type of check

        Returns:
            Command tuple or None if not configured
        """
        commands = {
            CheckType.LINT: self.config.lint_command,
            CheckType.BUILD: self.config.build_command,
            CheckType.TEST: self.config.test_command,
            CheckType.STATIC: self.config.static_command,
            CheckType.COVERAGE: self.config.coverage_command,
        }
        return commands.get(check_type)


async def run_mechanical_verification(
    execution_id: str,
    config: MechanicalConfig | None = None,
    checks: list[CheckType] | None = None,
) -> Result[tuple[MechanicalResult, list[BaseEvent]], ValidationError]:
    """Convenience function for running mechanical verification.

    Args:
        execution_id: Execution identifier
        config: Optional configuration
        checks: Optional list of checks to run

    Returns:
        Result with MechanicalResult and events
    """
    verifier = MechanicalVerifier(config)
    return await verifier.verify(execution_id, checks)
