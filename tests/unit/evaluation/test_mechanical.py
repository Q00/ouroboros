"""Tests for Stage 1 mechanical verification."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.acceptance import AcceptanceState
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.evaluation.mechanical import (
    CommandResult,
    MechanicalConfig,
    MechanicalVerifier,
    parse_coverage_from_output,
    run_command,
    run_mechanical_verification,
)
from ouroboros.evaluation.models import CheckType, EvaluationResult, MechanicalDisposition
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message

# Config with commands set for tests that need real/mocked command execution
_TEST_CONFIG = MechanicalConfig(
    lint_command=("echo", "lint-ok"),
    build_command=("echo", "build-ok"),
    test_command=("echo", "test-ok"),
    static_command=("echo", "static-ok"),
    coverage_command=("echo", "coverage-ok"),
)


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

    def test_colored_non_table_prose_does_not_hide_supported_total(self) -> None:
        assert parse_coverage_from_output("\x1b[32mTests passed\x1b[0m\nTOTAL 10 0 100%") == 1.0

    def test_unrelated_lcov_like_words_preserve_supported_total(self) -> None:
        output = "daily: checks passed\nSF builds passed\nTOTAL 10 0 100%"
        assert parse_coverage_from_output(output) == 1.0

    def test_unrelated_xml_element_preserves_supported_total(self) -> None:
        assert (
            parse_coverage_from_output(
                "<coverage-example/><cov:coverage-example/>\nTOTAL 10 0 100%"
            )
            == 1.0
        )

    def test_unicode_non_control_prose_preserves_supported_total(self) -> None:
        assert parse_coverage_from_output("검사 성공 ✓\nTOTAL 10 0 100%") == 1.0

    def test_unrelated_go_json_output_does_not_hide_supported_total(self) -> None:
        output = (
            'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"ok\\n"}\n'
            '{"Action":"output","Package":"q","Output":"pass\\n"}'
        )
        assert parse_coverage_from_output(output) == 1.0

    def test_unrelated_non_output_json_without_package_preserves_total(self) -> None:
        output = 'TOTAL 10 0 100%\n{"Action":"pass"}\n{"message":"unrelated"}'
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

    @pytest.mark.parametrize(
        "output",
        [
            "Required Coverage: 70%\nTOTAL   100   85   15.00%",
            "Coverage: 70% required\nTOTAL   100   85   15.00%",
            "Required Coverage: 70%",
            "Coverage: 999%",
            "TOTAL   100   0   101%",
            "TOTAL   10   0   100%\nTOTAL   100   85   15%",
            "Coverage: 100%\nCoverage: 15%",
            "TOTAL   10   0   100%\nTOTAL   100   85   15.00%",
            "TOTAL   10   0   100%\nTOTAL   100   85   40   30   15%",
            "TOTAL   100   20   80%\nCoverage: 15%",
        ],
        ids=[
            "requirement-beside-decimal-total",
            "requirement-line-beside-decimal-total",
            "requirement-only",
            "over-100-colon",
            "over-100-total",
            "conflicting-totals",
            "conflicting-colon",
            "readable-beside-decimal-total",
            "readable-beside-branch-total",
            "total-disagrees-with-colon",
        ],
    )
    def test_ambiguous_or_impossible_figures_are_unread(self, output: str) -> None:
        """A figure the output does not pin down is not a measurement."""
        assert parse_coverage_from_output(output) is None

    def test_unrelated_go_json_output_does_not_mask_a_supported_total(self) -> None:
        output = 'TOTAL 10 0 100%\n{"Action":"output","Package":"example.com/m","Output":"PASS\\n"}'
        assert parse_coverage_from_output(output) == 1.0

    @pytest.mark.parametrize(
        "output",
        ["TOTAL   100   20   80%\nTOTAL   100   20   80%", "TOTAL   100   20   80%\nCoverage: 80%"],
        ids=["repeated-total", "total-agrees-with-colon"],
    )
    def test_agreeing_figures_are_one_reading(self, output: str) -> None:
        assert parse_coverage_from_output(output) == 0.80


class TestMechanicalConfig:
    """Tests for MechanicalConfig."""

    def test_default_values(self) -> None:
        """Verify default configuration values."""
        config = MechanicalConfig()
        assert config.coverage_threshold == 0.7
        assert config.timeout_seconds == 300
        assert config.working_dir is None
        assert config.lint_command is None
        assert config.build_command is None
        assert config.test_command is None
        assert config.static_command is None
        assert config.coverage_command is None

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
        result = await run_command((sys.executable, "-c", "print('hello')"), timeout=5)
        assert result.return_code == 0
        assert "hello" in result.stdout
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_cancelled_command_kills_child_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Outer evaluation deadlines must not leave mechanical subprocesses alive."""

        class FakeProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            async def communicate(self):
                await asyncio.Event().wait()

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> int:
                self.waited = True
                return self.returncode or 0

        fake_process = FakeProcess()

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        task = asyncio.create_task(run_command(("long-running",), timeout=999))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_process.killed is True
        assert fake_process.waited is True

    @pytest.mark.asyncio
    async def test_failed_command(self) -> None:
        """Run failing command."""
        result = await run_command((sys.executable, "-c", "raise SystemExit(7)"), timeout=5)
        assert result.return_code == 7

    @pytest.mark.asyncio
    async def test_command_not_found(self) -> None:
        """Handle command not found."""
        result = await run_command(("nonexistent_command_xyz",), timeout=5)
        assert result.return_code == -1
        assert "not found" in result.stderr.lower() or "Command not found" in result.stderr

    @pytest.mark.asyncio
    async def test_command_strips_nested_mcp_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mechanical checks should not inherit the MCP server recursion sentinel."""
        monkeypatch.setenv("_OUROBOROS_NESTED", "1")

        result = await run_command(
            (
                sys.executable,
                "-c",
                "import os; print(os.environ.get('_OUROBOROS_NESTED', 'missing'))",
            ),
            timeout=5,
        )

        assert result.return_code == 0
        assert result.stdout.strip() == "missing"


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

            verifier = MechanicalVerifier(_TEST_CONFIG)
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

            verifier = MechanicalVerifier(_TEST_CONFIG)
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

            verifier = MechanicalVerifier(_TEST_CONFIG)
            result = await verifier.verify(
                "exec-1",
                checks=[CheckType.LINT, CheckType.BUILD],
            )

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is False
            assert len(mech_result.failed_checks) == 1

    @pytest.mark.asyncio
    async def test_verify_failed_check_preserves_command_and_output_tail(self) -> None:
        """Failed checks keep tail diagnostics instead of only the noisy prefix."""
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = CommandResult(
                1,
                "prefix\n" + ("." * 700) + "\nFAILED tests/test_example.py::test_case",
                "stderr prefix\n" + ("x" * 700) + "\nAssertionError: boom",
            )

            verifier = MechanicalVerifier(_TEST_CONFIG)
            result = await verifier.verify("exec-1", checks=[CheckType.TEST])

            assert result.is_ok
            mech_result, _ = result.value
            details = mech_result.checks[0].details
            assert details["command"] == ["echo", "test-ok"]
            assert "FAILED tests/test_example.py::test_case" in details["stdout_tail"]
            assert "AssertionError: boom" in details["stderr_tail"]
            assert "FAILED tests/test_example.py::test_case" not in details["stdout_preview"]

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

            config = MechanicalConfig(
                coverage_threshold=0.7,
                coverage_command=("echo", "coverage"),
            )
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

            config = MechanicalConfig(
                coverage_threshold=0.7,
                coverage_command=("echo", "coverage"),
            )
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

            verifier = MechanicalVerifier(_TEST_CONFIG)
            result = await verifier.verify("exec-1", checks=[CheckType.TEST])

            assert result.is_ok
            mech_result, _ = result.value
            assert mech_result.passed is False
            assert "timed out" in mech_result.checks[0].message.lower()

            # Timeout failures must carry the same command/cwd diagnostics as
            # other failures so the formatter can show which check hung.
            timeout_details = mech_result.checks[0].details
            assert timeout_details["timed_out"] is True
            assert timeout_details["command"] == ["echo", "test-ok"]
            assert "working_dir" in timeout_details

    @pytest.mark.asyncio
    async def test_verify_skips_unconfigured_checks(self) -> None:
        """Checks with no command configured are skipped (passed with skip message)."""
        config = MechanicalConfig()  # All commands are None
        verifier = MechanicalVerifier(config)
        result = await verifier.verify(
            "exec-1",
            checks=[CheckType.LINT, CheckType.BUILD, CheckType.TEST],
        )

        assert result.is_ok
        mech_result, _ = result.value
        assert mech_result.passed is True
        assert all(c.passed for c in mech_result.checks)
        assert all("skipped" in c.message.lower() for c in mech_result.checks)


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX virtualenv layout")
def test_run_command_resolves_tools_from_the_main_worktree_venv(tmp_path) -> None:
    """A task worktree lacks the gitignored ``.venv``; the command uses the main tree's."""
    main = tmp_path / "project"
    bin_dir = main / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (main / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    for name, body in (("python3", ""), ("projtool", "echo PROJECT_VENV_TOOL")):
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    gitdir = main / ".git" / "worktrees" / "orch_1"
    gitdir.mkdir(parents=True)
    worktree = tmp_path / "worktrees" / "orch_1"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")

    result = asyncio.run(run_command(("projtool",), timeout=30, working_dir=worktree))

    assert result.return_code == 0, result.stderr
    assert "PROJECT_VENV_TOOL" in result.stdout


# Coverage outputs the parser cannot read, one per common tool. Each reports
# 15% against a 70% threshold, so any of them reaching approval is the defect.
_UNPARSED_COVERAGE_OUTPUTS = {
    "pytest-cov --cov-branch": (
        "Name  Stmts Miss Branch BrPart Cover\nTOTAL   100   85   40   30   15%"
    ),
    "pytest-cov precision=2": "TOTAL   100   85   15.00%",
    "go test -cover": "ok  \texample.com/m\t0.002s\tcoverage: 15.0% of statements",
    "colored TOTAL conflicting with TOTAL": ("TOTAL 10 0 100%\n\x1b[31mTOTAL 10 8 20%\x1b[0m"),
    "split SGR TOTAL conflicting with TOTAL": ("TOTAL 10 0 100%\nTO\x1b[31mTAL 10 8 20%\x1b[0m"),
    "go json coverage conflicting with TOTAL": (
        "TOTAL 10 0 100%\n"
        '{"Action":"output","Package":"example.com/m",'
        '"Output":"coverage: 15.0% of statements\\n"}'
    ),
    "go json TOTAL conflicting with TOTAL": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"TOTAL 10 8 20%\\n"}'
    ),
    "go json TOTAL without package": (
        'TOTAL 10 0 100%\n{"Action":"output","Output":"TOTAL 10 8 20%\\n"}'
    ),
    "go json TOTAL null package": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":null,"Output":"TOTAL 10 8 20%\\n"}'
    ),
    "split go json TOTAL without package": (
        'TOTAL 10 0 100%\n{"Action":"output","Output":"TO"}\n'
        '{"Action":"output","Output":"TAL 10 8 20%\\n"}'
    ),
    "go json output missing text": 'TOTAL 10 0 100%\n{"Action":"output","Package":"p"}',
    "split go json TOTAL conflicting with TOTAL": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"TO"}\n'
        '{"Action":"output","Package":"p","Output":"TAL 10 8 20%\\n"}'
    ),
    "colored go json TOTAL conflicting with TOTAL": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p",'
        '"Output":"\\u001b[31mTOTAL 10 8 20%\\u001b[0m\\n"}'
    ),
    "go json package summary conflicting with TOTAL": (
        "TOTAL 10 0 100%\n"
        '{"Action":"output","Package":"example.com/m",'
        '"Output":"ok\\texample.com/m\\t0.002s\\tcoverage: 15.0% of statements\\n"}'
    ),
    "split go json conflicting with TOTAL": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"cover"}\n'
        '{"Action":"output","Package":"p","Output":"age: 15% of statements\\n"}'
    ),
    "colored go json conflicting with TOTAL": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p",'
        '"Output":"\\u001b[31mcoverage: 15% of statements\\u001b[0m\\n"}'
    ),
    "colored go plain conflicting with TOTAL": (
        "TOTAL 10 0 100%\n\x1b[31mok p 0.1s coverage: 15% of statements\x1b[0m"
    ),
    "unknown escape before go report": (
        "TOTAL 10 0 100%\n\x1b[?25lok p 0.1s coverage: 15% of statements"
    ),
    "c1 csi before go report": ("TOTAL 10 0 100%\n\x9b?25lok p 0.1s coverage: 15% of statements"),
    "c1 sgr before go report": ("TOTAL 10 0 100%\n\x9b31mok p 0.1s coverage: 15% of statements"),
    "escaped c1 inside go json": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p",'
        '"Output":"\\u009b?25lok p 0.1s coverage: 15% of statements\\n"}'
    ),
    "go conflicting with TOTAL": (
        "TOTAL 10 0 100%\nok  \texample.com/m\t0.002s\tcoverage: 15.0% of statements"
    ),
    "lowercase coverage conflicting with TOTAL": "TOTAL 10 0 100%\ncoverage: 15% of statements",
    "capitalized go coverage conflict": (
        "TOTAL 10 0 100%\nok example.com/m (cached) Coverage: 15% of statements"
    ),
    "jest --coverage": "All files |   15.2 |   10.1 |   12.3 |   15.2 |",
    "node spec coverage conflicting with TOTAL": (
        "TOTAL 10 0 100%\nℹ all files | 12.50 | 66.67 | 100.00 |"
    ),
    "node tap coverage conflicting with TOTAL": (
        "TOTAL 10 0 100%\n# all files | 12.50 | 66.67 | 100.00 |"
    ),
    "jest conflicting with TOTAL": (
        "TOTAL 10 0 100%\nAll files |   15.2 |   10.1 |   12.3 |   15.2 |"
    ),
    "colored jest conflicting with TOTAL": (
        "TOTAL 10 0 100%\n\x1b[31mAll files\x1b[0m | 15.2 | 10.1 | 12.3 | 15.2 |"
    ),
    "malformed ANSI table beside TOTAL": (
        "TOTAL 10 0 100%\n\x1b[" + "3" * 33 + "mAll files | 15.2 | 10.1 | 12.3 | 15.2 |"
    ),
    # Exceed the explicit row limit rather than depending on the interpreter's
    # JSON recursion limit, which differs across Python versions.
    "oversized deep JSON beside TOTAL": "TOTAL 10 0 100%\n" + '{"x":' * 11000 + "0" + "}" * 11000,
    "malformed JSON beside TOTAL": 'TOTAL 10 0 100%\n{"Action":"output",',
    "excess JSON events beside TOTAL": (
        "TOTAL 10 0 100%\n" + '{"Action":"output","Package":"p","Output":"x"}\n' * 257
    ),
    "coverage.py XML conflicting with TOTAL": (
        'TOTAL 10 0 100%\n<?xml version="1.0" ?>\n'
        '<coverage version="7.13.5" line-rate="0.8">\n</coverage>\n'
    ),
    "inline XML declaration and coverage": (
        'TOTAL 10 0 100%\n<?xml version="1.0"?><coverage line-rate="0.8"/>\n'
    ),
    "nested coverage XML element": (
        'TOTAL 10 0 100%\n<report><coverage line-rate="0.8"/></report>\n'
    ),
    "partial embedded coverage element": "TOTAL 10 0 100%\n<report><coverage\n",
    "namespaced coverage XML conflicting with TOTAL": (
        'TOTAL 10 0 100%\n<cov:coverage xmlns:cov="urn:cov" line-rate="0.8"/>\n'
    ),
    "unicode namespaced coverage XML": (
        'TOTAL 10 0 100%\n<보고:coverage xmlns:보고="urn:cov" line-rate="0.8"/>\n'
    ),
    "colored namespaced coverage XML": (
        'TOTAL 10 0 100%\n\x1b[31m<cov:coverage line-rate="0.8"/>\x1b[0m\n'
    ),
    "namespaced coverage XML split across go events": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"<cov:cov"}\n'
        '{"Action":"output","Package":"p","Output":"erage/>\\n"}'
    ),
    "colored coverage XML conflicting with TOTAL": (
        'TOTAL 10 0 100%\n\x1b[32m<coverage line-rate="0.8">\x1b[0m\n'
    ),
    "coverage XML split across go events": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"<cover"}\n'
        '{"Action":"output","Package":"p","Output":"age line-rate=\\"0.8\\">\\n"}'
    ),
    "node lcov conflicting with TOTAL": (
        "TOTAL 10 0 100%\nTN:\nSF:src/subject.js\nDA:1,1\nDA:2,0\nLH:1\nLF:2\nend_of_record\n"
    ),
    "colored lcov source beside TOTAL": ("TOTAL 10 0 100%\n\x1b[32mSF:src/subject.js\x1b[0m\n"),
    "lcov counters beside TOTAL": "TOTAL 10 0 100%\nLH:1\nLF:2\n",
    **{
        f"lcov {name} partial record": f"TOTAL 10 0 100%\n{row}\n"
        for name, row in {
            "line": "DA:1,0",
            "branch": "BRDA:12,0,1,0",
            "function": "FN:7,foo",
            "function-hit": "FNDA:0,foo",
            "test-name": "TN:",
            "version": "VER:123",
            "truncated-line": "DA:",
            "truncated-source": "SF:",
            "truncated-count": "LF:",
            "record-end": "end_of_record",
        }.items()
    },
    "lcov output split across go events": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"SF"}\n'
        '{"Action":"output","Package":"p","Output":":src/x.js\\n"}'
    ),
    "cargo tarpaulin": "15.00% coverage, 15/100 lines covered",
    "tarpaulin conflicting with TOTAL": "TOTAL 10 0 100%\n15.00% coverage, 15/100 lines covered",
    "colored tarpaulin conflicting with TOTAL": (
        "TOTAL 10 0 100%\n\x1b[31m15.00% coverage, 15/100 lines covered\x1b[0m"
    ),
    "tarpaulin output split across go events": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"15.00% cov"}\n'
        '{"Action":"output","Package":"p","Output":"erage, 15/100 lines covered\\n"}'
    ),
    "coverage report --format=total": "15",
    "bare total conflicting with TOTAL": "TOTAL 10 0 100%\n15\n",
    "bare decimal conflicting with TOTAL": "TOTAL 10 0 100%\n15.00\n",
    "bare percentage conflicting with TOTAL": "TOTAL 10 0 100%\n15%\n",
    "colored bare percentage conflicting with TOTAL": "TOTAL 10 0 100%\n\x1b[31m15%\x1b[0m\n",
    "bare total split across go events": (
        'TOTAL 10 0 100%\n{"Action":"output","Package":"p","Output":"1"}\n'
        '{"Action":"output","Package":"p","Output":"5\\n"}'
    ),
    "no output": "",
    "requirement beside decimal TOTAL": "Required Coverage: 70%\nTOTAL   100   85   15.00%",
    "coverage over 100%": "Coverage: 999%",
    "conflicting TOTAL rows": "TOTAL   10   0   100%\nTOTAL   100   85   15%",
    "readable beside decimal TOTAL": "TOTAL   10   0   100%\nTOTAL   100   85   15.00%",
    "trailing requirement": "Coverage: 70% required",
    "parenthesized requirement": "Coverage: 70% (minimum required)",
    "requirement beside measurement": "Coverage: 70% required\nCoverage: 80%",
    "TOTAL trailing requirement": "TOTAL 100 0 100% required",
    "malformed coverage beside measurement": "Coverage: unknown\nCoverage: 80%",
    "space before coverage colon": "TOTAL 100 20 80%\nCoverage : 15%",
    "tab before coverage colon": "TOTAL 100 20 80%\nCoverage\t: 15%",
    "spaces before coverage colon": "TOTAL 100 20 80%\nCoverage  : 15%",
    "coverage prefix whitespace with CRLF": "TOTAL 100 20 80%\r\n\tCoverage \t: 15%\r\n",
    "spaced coverage colon with unknown value": "TOTAL 100 20 80%\nCoverage : unknown",
    "requirement label alone": "Coverage requirement: 70%",
    "requirement prose alone": "Coverage must reach 70%",
    **{
        name: f"TOTAL 100 20 80%\n{row}"
        for name, row in {
            "missing colon": "Coverage 15%",
            "missing colon and whitespace": "Coverage15%",
            "missing colon with CRLF": "\tCoverage\t15%\r",
            "equals separator": "Coverage = 15%",
            "semicolon separator": "Coverage; 15%",
            "pipe separator": "Coverage | 15%",
            "unrecognized punctuation separator": "Coverage ! 15%",
            "duplicated separator": "Coverage:: 15%",
            "missing numeric value": "Coverage %",
            "missing entire measurement": "Coverage",
            "missing numeric value after colon": "Coverage: %",
            "negative numeric value": "Coverage -15%",
            "explicit positive sign": "Coverage +15%",
            "separated sign": "Coverage + 15%",
            "duplicated sign": "Coverage --15%",
            "missing integral part": "Coverage .15%",
            "missing fractional part": "Coverage 15.%",
            "duplicated decimal point": "Coverage 1..5%",
            "comma decimal": "Coverage 15,0%",
            "exponential value": "Coverage 1e1%",
            "nonfinite value": "Coverage NaN%",
            "infinite value": "Coverage inf%",
            "missing percent unit": "Coverage 15",
            "malformed percent unit": "Coverage 15pct",
            "repeated percent unit": "Coverage 15%%",
            "unit without value": "Coverage percent",
            "numeric requirement suffix": "Coverage 15% required",
        }.items()
    },
}


def _printing(text: str) -> tuple[str, ...]:
    return (sys.executable, "-c", f"import sys; sys.stdout.write({text!r})")


class _UnexpectedLLMAdapter:
    async def complete(
        self, messages: list[Message], config: CompletionConfig
    ) -> Result[CompletionResponse, ProviderError]:
        pytest.fail("Stage 1-only evaluation must not call an LLM")


async def _evaluate_stage1_only(config: MechanicalConfig) -> EvaluationResult:
    from ouroboros.evaluation import EvaluationContext, EvaluationPipeline, PipelineConfig

    pipeline = EvaluationPipeline(
        llm_adapter=_UnexpectedLLMAdapter(),
        config=PipelineConfig(stage2_enabled=False, stage3_enabled=False, mechanical=config),
    )
    context = EvaluationContext(
        execution_id="exec-cov",
        seed_id="seed-cov",
        current_ac="Coverage is at least 70%",
        artifact="x",
    )
    result = await pipeline.evaluate(context)
    assert result.is_ok, result
    return result.value


class TestUnparsedCoverageIsNotEvidence:
    """A coverage command that exits 0 without a readable figure decides nothing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_passing_test", [False, True], ids=["alone", "with-test"])
    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    @pytest.mark.parametrize(
        "prefix",
        [chr(n) for n in [*range(9), *range(14, 27), 31, 127]]
        + ["\u200b", "\ufeff", "\u00ad", "\u200e", "\u2060", "\ufffd"],
        ids=lambda prefix: f"U+{ord(prefix):04X}",
    )
    async def test_hidden_prefix_total_is_not_approved(
        self, prefix: str, stream: str, with_passing_test: bool
    ) -> None:
        low = prefix + "TOTAL 10 8 20%\n"
        stdout = "TOTAL 10 0 100%\n" + (low if stream == "stdout" else "")
        stderr = low if stream == "stderr" else ""
        command = (
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r})",
        )
        result = await _evaluate_stage1_only(
            MechanicalConfig(
                coverage_command=command,
                test_command=_printing("1 passed") if with_passing_test else None,
            )
        )
        assert result.stage1_result is not None
        assert result.stage1_result.coverage_score is None
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_passing_test", [False, True], ids=["alone", "with-test"])
    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    @pytest.mark.parametrize(
        "low",
        [
            b"\xffTOTAL 10 8 20%\n",
            b'\x1b[0m{"Action":"output","Package":"p","Output":"TOTAL 10 8 20%\\n"}\n',
            b"\x1b[31mTOTAL 10 8 20%\x1b[0m\n",
            b"\x1b[31mTOTAL 10 0 100%\x1b[0m\n",
        ],
        ids=["invalid-utf8", "ansi-prefix-go-json", "colored-total", "matching-colored-total"],
    )
    async def test_decorated_coverage_is_not_approved(
        self, low: bytes, stream: str, with_passing_test: bool
    ) -> None:
        stdout = b"TOTAL 10 0 100%\n" + (low if stream == "stdout" else b"")
        stderr = low if stream == "stderr" else b""
        command = (
            sys.executable,
            "-c",
            f"import sys; sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r})",
        )
        result = await _evaluate_stage1_only(
            MechanicalConfig(
                coverage_command=command,
                test_command=_printing("1 passed") if with_passing_test else None,
            )
        )
        assert result.stage1_result is not None
        assert result.stage1_result.coverage_score is None
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "prose",
        [
            "a\u200bb diagnostic",
            "\x08ordinary diagnostic",
            "a\ufffdb diagnostic",
            "\x1b[32m검사 성공 ✓\x1b[0m",
        ],
    )
    async def test_decorated_ordinary_prose_preserves_supported_report(self, prose: str) -> None:
        result = await _evaluate_stage1_only(
            MechanicalConfig(coverage_command=_printing(prose + "\nTOTAL 10 0 100%\n"))
        )
        assert result.final_approved is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_passing_test", [False, True])
    async def test_conflicting_stderr_coverage_is_not_ignored(
        self, with_passing_test: bool
    ) -> None:
        command = (
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('TOTAL 10 0 100%\\n'); "
            "sys.stderr.write('TOTAL 100 85 15%\\n')",
        )
        result = await _evaluate_stage1_only(
            MechanicalConfig(
                coverage_threshold=0.7,
                coverage_command=command,
                test_command=_printing("1 passed") if with_passing_test else None,
            )
        )
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stdout", "stderr", "approved"),
        [
            ("TOTAL 10 0 100%\n", "warning: ordinary diagnostic\n", True),
            ("TOTAL 10 0 100%\n", "TOTAL 10 0 100%\n", True),
            ("nothing\n", "TOTAL 10 0 100%\n", False),
            ("TOTAL 10 0 100%\n", "coverage: 15% of statements\n", False),
        ],
    )
    async def test_stderr_cross_check_preserves_stdout_evidence_contract(
        self, stdout: str, stderr: str, approved: bool
    ) -> None:
        command = (
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r})",
        )
        result = await _evaluate_stage1_only(
            MechanicalConfig(coverage_threshold=0.7, coverage_command=command)
        )
        assert result.final_approved is approved

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_passing_test", [False, True], ids=["alone", "with-test"])
    @pytest.mark.parametrize(
        "output", _UNPARSED_COVERAGE_OUTPUTS.values(), ids=list(_UNPARSED_COVERAGE_OUTPUTS)
    )
    async def test_unreadable_coverage_is_not_approved(
        self, output: str, with_passing_test: bool
    ) -> None:
        config = MechanicalConfig(
            coverage_threshold=0.7,
            coverage_command=_printing(output),
            test_command=_printing("1 passed") if with_passing_test else None,
        )

        result = await _evaluate_stage1_only(config)

        stage1 = result.stage1_result
        assert stage1 is not None
        coverage = next(c for c in stage1.checks if c.check_type is CheckType.COVERAGE)
        assert coverage.passed is False
        assert coverage.executed is False
        assert coverage.details["coverage_unparsed"] is True
        assert coverage.details["return_code"] == 0
        assert "coverage_score" not in coverage.details
        assert coverage.message == (
            "Coverage command exited 0 but no coverage figure could be read from its "
            "output; threshold 70.0% not verified"
        )
        assert stage1.coverage_score is None
        assert stage1.passed is False
        assert stage1.disposition is MechanicalDisposition.NO_EVIDENCE
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED
        assert result.failure_reason == (
            "Not approved: unverified. No executed verification evidence "
            "(Stage 1 reported failures from no executed check)."
        )
        completed = next(e for e in result.events if e.type == "evaluation.stage1.completed")
        assert completed.data["passed"] is False
        assert completed.data["coverage_score"] is None
        assert {"check_type": "coverage", "passed": False, "message": coverage.message} in (
            completed.data["checks"]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("output", "approved", "disposition"),
        [
            ("TOTAL   100   85   15%", False, MechanicalDisposition.EXECUTED_FAIL),
            ("Coverage: 15.0%", False, MechanicalDisposition.EXECUTED_FAIL),
            ("TOTAL   100   20   80%", True, MechanicalDisposition.EXECUTED_PASS),
            ("\tCoverage: 80.0% \r\n", True, MechanicalDisposition.EXECUTED_PASS),
            ("TOTAL 100 20 80%\r\nCoverage: 80%\r\n", True, MechanicalDisposition.EXECUTED_PASS),
            ("\tCoverage:\t80% \r\n", True, MechanicalDisposition.EXECUTED_PASS),
            (
                "Required Coverage: 70%\nTOTAL 100 20 80%",
                True,
                MechanicalDisposition.EXECUTED_PASS,
            ),
            (
                "Coverage requirement: 70%\nTOTAL 100 20 80%",
                True,
                MechanicalDisposition.EXECUTED_PASS,
            ),
            ("Coverage must reach 70%\nCoverage: 80%", True, MechanicalDisposition.EXECUTED_PASS),
            ("Coverage report follows\nCoverage: 80%", True, MechanicalDisposition.EXECUTED_PASS),
            ("CoverageThreshold=70%\nCoverage: 80%", True, MechanicalDisposition.EXECUTED_PASS),
        ],
        ids=[
            "pytest-cov-below",
            "coverage-colon-below",
            "pytest-cov-above",
            "crlf",
            "agreeing",
            "whitespace-after-colon",
            "requirement-beside-measurement",
            "requirement-label-beside-measurement",
            "requirement-prose-beside-measurement",
            "report-prose-beside-measurement",
            "different-identifier-beside-measurement",
        ],
    )
    async def test_readable_coverage_still_decides(
        self, output: str, approved: bool, disposition: MechanicalDisposition
    ) -> None:
        config = MechanicalConfig(coverage_threshold=0.7, coverage_command=_printing(output))

        result = await _evaluate_stage1_only(config)

        stage1 = result.stage1_result
        assert stage1 is not None
        coverage = next(c for c in stage1.checks if c.check_type is CheckType.COVERAGE)
        assert coverage.executed is True
        assert coverage.passed is approved
        assert "coverage_unparsed" not in coverage.details
        assert stage1.disposition is disposition
        assert result.final_approved is approved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "no report",
        "Coverage: 70% required",
        "Coverage: unknown",
        "TOTAL 100 20 80%\nCoverage : unknown",
        "TOTAL 100 20 80%\nCoverage 15%",
        "TOTAL 100 20 80%\nCoverage = 15%",
        "TOTAL 100 20 80%\nCoverage .15%",
        "TOTAL 100 20 80%\nCoverage 15pct",
    ],
)
@pytest.mark.parametrize("return_code", [0, 1])
@pytest.mark.parametrize("failing_test", [False, True])
async def test_disabled_coverage_threshold_preserves_exit_status(
    output: str, return_code: int, failing_test: bool
) -> None:
    config = MechanicalConfig(
        coverage_threshold=0.0,
        coverage_command=(
            sys.executable,
            "-c",
            f"import sys; print({output!r}); sys.exit({return_code})",
        ),
        test_command=(sys.executable, "-c", "assert False, 'feature missing'")
        if failing_test
        else None,
    )
    result = await _evaluate_stage1_only(config)
    stage1 = result.stage1_result
    assert stage1 is not None
    coverage = next(c for c in stage1.checks if c.check_type is CheckType.COVERAGE)
    assert coverage.executed is True
    assert coverage.passed is (return_code == 0)
    assert coverage.details["return_code"] == return_code
    assert "coverage_score" not in coverage.details
    assert stage1.coverage_score is None
    approved = return_code == 0 and not failing_test
    assert stage1.disposition is (
        MechanicalDisposition.EXECUTED_PASS if approved else MechanicalDisposition.EXECUTED_FAIL
    )
    assert result.final_approved is approved
    assert result.acceptance_state is (
        AcceptanceState.APPROVED if approved else AcceptanceState.REJECTED
    )
    completed = next(e for e in result.events if e.type == "evaluation.stage1.completed")
    assert completed.data["passed"] is approved
    assert completed.data["coverage_score"] is None


_JSON_TRANSPORT_OUTPUTS = {
    **{
        f"escaped-prefix-U+{ord(prefix):04X}": ([prefix + "TOTAL 10 8 20%\n"], True, False)
        for prefix in [chr(n) for n in [*range(9), *range(14, 27), 31, 127]]
        + ["\u200b", "\ufeff", "\u00ad", "\u200e", "\u2060", "\ufffd"]
    },
    "split-prefix": (["\u200b", "TOTAL 10 8 20%\n"], True, False),
    "split-token": (["TO", "\x08TAL 10 8 20%\n"], True, False),
    "split-SGR": (["\x1b[3", "1m\u200bTOTAL 10 8 20%\x1b[0m\n"], True, False),
    "second-row": (["ordinary log\n�TOTAL 10 8 20%\n"], True, False),
    "escaped-prose-0": (["ordinary log\n"], True, True),
    "escaped-prose-1": (["검사 성공 ✓\n"], True, True),
    "escaped-prose-2": (["\x08ordinary diagnostic\n"], True, True),
    "escaped-prose-3": (["a\u200bb diagnostic\n"], True, True),
    "escaped-prose-4": (["a�b diagnostic\n"], True, True),
    "escaped-prose-5": (["emoji 👩\u200d💻 log\n"], True, True),
    "escaped-prose-6": (["\x1b[32mTests passed\x1b[0m\n"], True, True),
    "raw-json-prose-U+200D": (["ordinary \u200d diagnostic\n"], False, False),
    "raw-json-prose-U+200F": (["ordinary \u200f diagnostic\n"], False, False),
    "raw-json-prose-U+FFFD": (["ordinary � diagnostic\n"], False, False),
    "supported-high": ([], True, True),
    "decoded-low-TOTAL": (["TOTAL 10 8 20%\n"], True, False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_passing_test", [False, True], ids=["alone", "with-test"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize(
    ("payload", "approved"),
    [
        pytest.param(
            "".join(
                json.dumps({"Action": "output", "Package": "p", "Output": text}, ensure_ascii=ascii)
                + "\n"
                for text in texts
            ),
            approved,
            id=name,
        )
        for name, (texts, ascii, approved) in _JSON_TRANSPORT_OUTPUTS.items()
    ],
)
async def test_json_transport_coverage_evidence(
    payload: str, approved: bool, stream: str, with_passing_test: bool
) -> None:
    """Decoded decoration never hides a TOTAL; ordinary escaped prose remains usable."""
    stdout = "TOTAL 10 0 100%\n" + (payload if stream == "stdout" else "")
    stderr = payload if stream == "stderr" else ""
    command = (
        sys.executable,
        "-c",
        f"import sys;sys.stdout.write({stdout!r});sys.stderr.write({stderr!r})",
    )
    result = await _evaluate_stage1_only(
        MechanicalConfig(
            coverage_threshold=0.7,
            coverage_command=command,
            test_command=_printing("1 passed") if with_passing_test else None,
        )
    )
    stage1 = result.stage1_result
    assert stage1 is not None
    assert stage1.coverage_score == (1.0 if approved else None)
    assert stage1.passed is approved
    assert result.final_approved is approved
    assert result.acceptance_state is (
        AcceptanceState.APPROVED if approved else AcceptanceState.UNVERIFIED
    )
    coverage = next(c for c in stage1.checks if c.check_type is CheckType.COVERAGE)
    assert coverage.executed is approved
    if not approved:
        assert coverage.details["coverage_unparsed"] is True
