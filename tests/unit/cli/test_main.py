"""Unit tests for CLI main module."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros import __version__
from ouroboros.cli.main import app

runner = CliRunner()


class _BenchmarkHermesRuntime:
    """Hermes-shaped runtime that captures end-to-end CLI benchmark calls."""

    runtime_backend = "hermes"
    llm_backend = "hermes"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.adapter import TaskResult

        envelope = json.loads(prompt)
        call_context = envelope["call_context"]
        trace = envelope["trace"]
        selected_chunk_ids = list(trace["selected_chunk_ids"])
        self.calls.append(
            {
                "prompt": prompt,
                "envelope": envelope,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_handle": resume_handle,
                "resume_session_id": resume_session_id,
            }
        )
        completion = {
            "schema_version": "rlm.hermes.output.v1",
            "mode": envelope["mode"],
            "verdict": "passed",
            "confidence": 0.95,
            "result": {
                "summary": f"benchmark completion for {call_context['call_id']}",
                "call_id": call_context["call_id"],
                "parent_call_id": call_context["parent_call_id"],
                "selected_chunk_ids": selected_chunk_ids,
            },
            "evidence_references": [
                {
                    "chunk_id": chunk_id,
                    "claim": f"{call_context['call_id']} consumed {chunk_id}",
                }
                for chunk_id in selected_chunk_ids
            ],
            "residual_gaps": [],
        }
        return Result.ok(
            TaskResult(
                success=True,
                final_message=json.dumps(completion, sort_keys=True),
                messages=(),
            )
        )


class TestMainApp:
    """Tests for the main Typer application."""

    def test_app_has_help(self) -> None:
        """Test that --help shows formatted help text."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Ouroboros" in result.output
        assert "Self-Improving AI Workflow System" in result.output

    def test_app_version_option(self) -> None:
        """Test that --version shows version information."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        # Strip ANSI codes for comparison (Rich adds color formatting)
        import re

        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert __version__ in clean_output

    def test_app_version_short_option(self) -> None:
        """Test that -V shows version information."""
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        # Strip ANSI codes for comparison (Rich adds color formatting)
        import re

        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert __version__ in clean_output

    def test_no_args_shows_help(self) -> None:
        """Test that running without args shows help (exit code 2 for no_args_is_help)."""
        result = runner.invoke(app, [])
        # no_args_is_help=True causes exit code 2, which is expected
        assert result.exit_code == 2
        assert "Ouroboros" in result.output


class TestCommandGroups:
    """Tests for command group registration."""

    def test_run_command_group_registered(self) -> None:
        """Test that run command group is registered."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "Execute Ouroboros workflows" in result.output

    def test_config_command_group_registered(self) -> None:
        """Test that config command group is registered."""
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "Manage Ouroboros configuration" in result.output

    def test_status_command_group_registered(self) -> None:
        """Test that status command group is registered."""
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
        assert "Check Ouroboros system status" in result.output

    def test_rlm_command_registered(self) -> None:
        """Test that the isolated RLM command is registered."""
        result = runner.invoke(app, ["rlm", "--help"])
        assert result.exit_code == 0
        assert "Recursive Language Model" in result.output


class TestRunCommands:
    """Tests for run command group."""

    def test_run_workflow_help(self) -> None:
        """Test run workflow command help."""
        result = runner.invoke(app, ["run", "workflow", "--help"])
        assert result.exit_code == 0
        assert "seed" in result.output.lower()
        assert "runtime" in result.output.lower()
        assert "hermes" in result.output.lower()

    def test_run_resume_help(self) -> None:
        """Test run resume command help."""
        result = runner.invoke(app, ["run", "resume", "--help"])
        assert result.exit_code == 0
        assert "Resume" in result.output


class TestRLMCommand:
    """Tests for the isolated rlm command."""

    def test_rlm_dry_run_uses_isolated_path(self) -> None:
        """The RLM command must not dispatch through run or evolve paths."""
        mock_run_orchestrator = AsyncMock()

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=mock_run_orchestrator,
        ):
            result = runner.invoke(app, ["rlm", "src", "--dry-run"])

        assert result.exit_code == 0
        assert "RLM command path ready" in result.output
        assert "run/evolve command paths were not invoked" in result.output
        mock_run_orchestrator.assert_not_awaited()

    def test_rlm_benchmark_flag_uses_benchmark_runner(self) -> None:
        """The RLM command can enter the built-in benchmark through the loop."""
        from ouroboros.rlm import RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID, RLMRunResult

        mock_run_benchmark = AsyncMock(
            return_value=RLMRunResult(
                status="ready",
                target="src",
                target_kind="path",
                cwd=Path.cwd(),
                max_depth=5,
                ambiguity_threshold=0.2,
                message="RLM command path ready; run/evolve command paths were not invoked.",
            )
        )

        with patch("ouroboros.cli.commands.rlm.run_rlm_benchmark", new=mock_run_benchmark):
            result = runner.invoke(app, ["rlm", "--benchmark", "--dry-run"])

        assert result.exit_code == 0
        assert "RLM command path ready" in result.output
        mock_run_benchmark.assert_awaited_once()
        config = mock_run_benchmark.await_args.args[0]
        assert config.dry_run is True
        assert mock_run_benchmark.await_args.kwargs["benchmark_id"] == (
            RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
        )

    def test_rlm_vanilla_baseline_flag_uses_baseline_runner(self) -> None:
        """The RLM command exposes the vanilla baseline without entering recursive loops."""
        from ouroboros.rlm import (
            RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
            RLMVanillaBaselineQualityScore,
            RLMVanillaTruncationBaselineResult,
        )

        project_root = Path(__file__).resolve().parents[3]
        fixture_path = project_root / "tests" / "fixtures" / "rlm" / "long_context_truncation.json"
        mock_run_loop = AsyncMock()
        mock_run_benchmark = AsyncMock()
        mock_run_baseline = AsyncMock(
            return_value=RLMVanillaTruncationBaselineResult(
                baseline_id="rlm-vanilla-truncation-baseline-v1",
                fixture_id="rlm-long-context-truncation-v1",
                target_path="long_context_truncation_target.txt",
                status="completed",
                success=True,
                call_id="rlm_call_vanilla_truncation_baseline",
                prompt="{}",
                completion="{}",
                selected_chunk_ids=("long_context_truncation_target.txt:1-2",),
                omitted_chunk_ids=("long_context_truncation_target.txt:3-4",),
                retained_line_count=2,
                omitted_line_count=2,
                target_line_count=4,
                elapsed_ms=1,
                output_quality=RLMVanillaBaselineQualityScore(
                    schema_version=RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
                    scoring_method="truncation_fixture_requirements_v1",
                    score=0.9,
                    required_field_score=1.0,
                    confidence_score=1.0,
                    retained_fact_citation_score=1.0,
                    truncation_boundary_score=0.0,
                    omitted_fact_safety_score=1.0,
                    required_fields_present=(),
                    required_fields_missing=(),
                    cited_retained_fact_ids=(),
                    missing_retained_fact_ids=(),
                    cited_selected_chunk_ids=(),
                    cited_omitted_chunk_ids=(),
                    claimed_omitted_fact_ids=(),
                    reports_truncation_boundary=False,
                    confidence=0.9,
                ),
            )
        )

        with (
            patch("ouroboros.cli.commands.rlm.run_rlm_loop", new=mock_run_loop),
            patch("ouroboros.cli.commands.rlm.run_rlm_benchmark", new=mock_run_benchmark),
            patch(
                "ouroboros.cli.commands.rlm.run_vanilla_truncation_baseline",
                new=mock_run_baseline,
            ),
        ):
            result = runner.invoke(
                app,
                ["rlm", "--vanilla-baseline", "--truncation-fixture", str(fixture_path)],
            )

        assert result.exit_code == 0
        assert "Vanilla Hermes baseline completed with one sub-call" in result.output
        mock_run_baseline.assert_awaited_once()
        mock_run_loop.assert_not_awaited()
        mock_run_benchmark.assert_not_awaited()
        config = mock_run_baseline.await_args.args[0]
        assert config.fixture["fixture_id"] == "rlm-long-context-truncation-v1"

    def test_rlm_truncation_benchmark_flag_uses_shared_runner(self) -> None:
        """The RLM command can record vanilla and recursive outputs from one fixture."""
        from ouroboros.rlm import (
            RLM_SHARED_TRUNCATION_BENCHMARK_ID,
            RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION,
            RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
            RLMRunResult,
            RLMSharedTruncationBenchmarkResult,
            RLMVanillaBaselineQualityScore,
            RLMVanillaTruncationBaselineResult,
        )

        project_root = Path(__file__).resolve().parents[3]
        fixture_path = project_root / "tests" / "fixtures" / "rlm" / "long_context_truncation.json"
        vanilla_result = RLMVanillaTruncationBaselineResult(
            baseline_id="rlm-vanilla-truncation-baseline-v1",
            fixture_id="rlm-long-context-truncation-v1",
            target_path="long_context_truncation_target.txt",
            status="completed",
            success=True,
            call_id="rlm_call_vanilla_truncation_baseline",
            prompt="{}",
            completion="{}",
            selected_chunk_ids=("long_context_truncation_target.txt:1-2",),
            omitted_chunk_ids=("long_context_truncation_target.txt:3-4",),
            retained_line_count=2,
            omitted_line_count=2,
            target_line_count=4,
            elapsed_ms=1,
            output_quality=RLMVanillaBaselineQualityScore(
                schema_version=RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
                scoring_method="truncation_fixture_requirements_v1",
                score=0.9,
                required_field_score=1.0,
                confidence_score=1.0,
                retained_fact_citation_score=1.0,
                truncation_boundary_score=0.0,
                omitted_fact_safety_score=1.0,
                required_fields_present=(),
                required_fields_missing=(),
                cited_retained_fact_ids=(),
                missing_retained_fact_ids=(),
                cited_selected_chunk_ids=(),
                cited_omitted_chunk_ids=(),
                claimed_omitted_fact_ids=(),
                reports_truncation_boundary=False,
                confidence=0.9,
            ),
            result_path=Path("vanilla.json"),
        )
        shared_result = RLMSharedTruncationBenchmarkResult(
            benchmark_id=RLM_SHARED_TRUNCATION_BENCHMARK_ID,
            schema_version=RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION,
            fixture_id="rlm-long-context-truncation-v1",
            target_path="long_context_truncation_target.txt",
            status="completed",
            success=True,
            vanilla_result=vanilla_result,
            rlm_result=RLMRunResult(
                status="completed",
                target="long_context_truncation_target.txt",
                target_kind="path",
                cwd=Path.cwd(),
                max_depth=5,
                ambiguity_threshold=0.2,
                message="RLM command path completed",
                hermes_subcall_count=5,
            ),
            selected_chunk_ids=("long_context_truncation_target.txt:1-2",),
            omitted_chunk_ids=("long_context_truncation_target.txt:3-4",),
            result_path=Path("shared.json"),
        )
        mock_run_loop = AsyncMock()
        mock_run_benchmark = AsyncMock()
        mock_run_baseline = AsyncMock()
        mock_run_shared = AsyncMock(return_value=shared_result)

        with (
            patch("ouroboros.cli.commands.rlm.run_rlm_loop", new=mock_run_loop),
            patch("ouroboros.cli.commands.rlm.run_rlm_benchmark", new=mock_run_benchmark),
            patch(
                "ouroboros.cli.commands.rlm.run_vanilla_truncation_baseline",
                new=mock_run_baseline,
            ),
            patch(
                "ouroboros.cli.commands.rlm._run_shared_truncation_with_default_trace_store",
                new=mock_run_shared,
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "rlm",
                    "--truncation-benchmark",
                    "--truncation-fixture",
                    str(fixture_path),
                    "--truncation-output",
                    "shared.json",
                    "--baseline-output",
                    "vanilla.json",
                ],
        )

        assert result.exit_code == 0
        assert "Shared truncation benchmark completed" in result.output
        import re

        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "vanilla=1, rlm=5" in clean_output
        mock_run_shared.assert_awaited_once()
        mock_run_loop.assert_not_awaited()
        mock_run_benchmark.assert_not_awaited()
        mock_run_baseline.assert_not_awaited()
        config = mock_run_shared.await_args.args[0]
        assert config.fixture["fixture_id"] == "rlm-long-context-truncation-v1"
        assert config.result_path == Path("shared.json")
        assert config.baseline_result_path == Path("vanilla.json")

    def test_rlm_benchmark_executes_end_to_end_with_hermes_inner_lm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ooo rlm benchmark path reaches Hermes as the inner LM boundary."""
        from ouroboros.persistence.event_store import EventStore
        from ouroboros.rlm import (
            HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT,
            RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
            RLMRunConfig,
        )

        project_root = Path(__file__).resolve().parents[3]
        runtime = _BenchmarkHermesRuntime()
        event_db = tmp_path / "rlm-cli-benchmark.db"

        def event_store_factory(
            database_url: str | None = None,
            *,
            read_only: bool = False,
        ) -> EventStore:
            assert database_url is None
            return EventStore(
                f"sqlite+aiosqlite:///{event_db}",
                read_only=read_only,
            )

        def default_hermes_runtime(config: RLMRunConfig) -> _BenchmarkHermesRuntime:
            assert config.cwd == project_root
            return runtime

        monkeypatch.setattr("ouroboros.persistence.event_store.EventStore", event_store_factory)
        monkeypatch.setattr("ouroboros.rlm.loop._default_hermes_runtime", default_hermes_runtime)

        result = runner.invoke(
            app,
            ["rlm", "--benchmark", "--cwd", str(project_root)],
        )

        import re

        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

        assert result.exit_code == 0
        assert "RLM command path completed" in clean_output
        assert RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID in clean_output
        assert "cited_source_files=" in clean_output
        assert "rlm_tree_depth=" in clean_output
        assert len(runtime.calls) >= 1
        assert all(call["tools"] == [] for call in runtime.calls)
        assert all(
            call["system_prompt"] == HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT for call in runtime.calls
        )

        envelopes = [call["envelope"] for call in runtime.calls]
        assert all(
            envelope["run"]["seed_id"] == RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID for envelope in envelopes
        )
        assert all(
            envelope["constraints"]["must_not_call_ouroboros"] is True for envelope in envelopes
        )
        assert all(envelope["context"]["benchmark_fixture"] is not None for envelope in envelopes)
        assert any(envelope["mode"] == "execute_atomic" for envelope in envelopes)

    def test_rlm_recursive_fixture_executes_end_to_end_from_cli(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The checked-in recursive fixture runs through the ooo rlm CLI boundary."""
        from ouroboros.persistence.event_store import EventStore
        from ouroboros.rlm import (
            HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT,
            RLM_HERMES_CALL_STARTED_EVENT,
            RLM_HERMES_CALL_SUCCEEDED_EVENT,
            RLM_TRACE_AGGREGATE_TYPE,
            RLMRunConfig,
        )

        project_root = Path(__file__).resolve().parents[3]
        fixture_path = project_root / "tests" / "fixtures" / "rlm" / "long_context_truncation.json"
        runtime = _BenchmarkHermesRuntime()
        event_db = tmp_path / "rlm-cli-recursive-fixture.db"

        def event_store_factory(
            database_url: str | None = None,
            *,
            read_only: bool = False,
        ) -> EventStore:
            assert database_url is None
            return EventStore(
                f"sqlite+aiosqlite:///{event_db}",
                read_only=read_only,
            )

        def default_hermes_runtime(config: RLMRunConfig) -> _BenchmarkHermesRuntime:
            assert config.cwd == tmp_path
            assert config.fixture_id == "rlm-long-context-truncation-v1"
            return runtime

        async def replay_events() -> list[object]:
            store = EventStore(f"sqlite+aiosqlite:///{event_db}", read_only=True)
            await store.initialize(create_schema=False)
            try:
                return await store.replay(RLM_TRACE_AGGREGATE_TYPE, "rlm_generation_0")
            finally:
                await store.close()

        monkeypatch.setattr("ouroboros.persistence.event_store.EventStore", event_store_factory)
        monkeypatch.setattr("ouroboros.rlm.loop._default_hermes_runtime", default_hermes_runtime)

        result = runner.invoke(
            app,
            [
                "rlm",
                "--recursive-fixture",
                str(fixture_path),
                "--cwd",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0
        assert "RLM command path completed" in result.output
        assert "5 Hermes atomic execution sub-call" in result.output
        assert "target:" in result.output
        assert "fixture:" in result.output

        target_file = tmp_path / "long_context_truncation_target.txt"
        assert target_file.is_file()
        assert target_file.read_text(encoding="utf-8").splitlines()[0].startswith(
            "FACT:LC-001 command isolation is mandatory"
        )

        assert len(runtime.calls) == 5
        assert all(call["tools"] == [] for call in runtime.calls)
        assert all(
            call["system_prompt"] == HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT for call in runtime.calls
        )

        envelopes = [call["envelope"] for call in runtime.calls]
        assert [envelope["call_context"]["call_id"] for envelope in envelopes] == [
            "rlm_call_atomic_chunk_001",
            "rlm_call_atomic_chunk_002",
            "rlm_call_atomic_chunk_003",
            "rlm_call_atomic_chunk_004",
            "rlm_call_atomic_synthesis",
        ]
        assert [envelope["mode"] for envelope in envelopes] == [
            "execute_atomic",
            "execute_atomic",
            "execute_atomic",
            "execute_atomic",
            "synthesize_parent",
        ]
        assert envelopes[-1]["trace"]["selected_chunk_ids"] == [
            "long_context_truncation_target.txt:1-2",
            "long_context_truncation_target.txt:3-4",
            "long_context_truncation_target.txt:5-6",
            "long_context_truncation_target.txt:7-8",
        ]
        assert all(
            envelope["constraints"]["must_not_call_ouroboros"] is True
            for envelope in envelopes
        )

        events = asyncio.run(replay_events())
        assert [event.type for event in events] == [
            RLM_HERMES_CALL_STARTED_EVENT,
            RLM_HERMES_CALL_SUCCEEDED_EVENT,
            RLM_HERMES_CALL_STARTED_EVENT,
            RLM_HERMES_CALL_SUCCEEDED_EVENT,
            RLM_HERMES_CALL_STARTED_EVENT,
            RLM_HERMES_CALL_SUCCEEDED_EVENT,
            RLM_HERMES_CALL_STARTED_EVENT,
            RLM_HERMES_CALL_SUCCEEDED_EVENT,
            RLM_HERMES_CALL_STARTED_EVENT,
            RLM_HERMES_CALL_SUCCEEDED_EVENT,
        ]

    def test_rlm_benchmark_id_rejects_unknown_benchmark(self) -> None:
        """Unknown benchmark IDs fail before any run/evolve fallback can happen."""
        result = runner.invoke(
            app,
            ["rlm", "--benchmark-id", "missing-benchmark", "--dry-run"],
        )

        assert result.exit_code == 1
        assert "Unknown RLM benchmark ID: missing-benchmark" in result.output


class TestInitCommands:
    """Tests for init command group."""

    def test_init_start_help(self) -> None:
        """Test init start command help."""
        result = runner.invoke(app, ["init", "start", "--help"])
        assert result.exit_code == 0
        assert "context" in result.output.lower()
        assert "runtime" in result.output.lower()
        assert "llm-backend" in result.output.lower()


class TestConfigCommands:
    """Tests for config command group."""

    def test_config_show_help(self) -> None:
        """Test config show command help."""
        result = runner.invoke(app, ["config", "show", "--help"])
        assert result.exit_code == 0
        assert "Display" in result.output

    def test_config_init_help(self) -> None:
        """Test config init command help."""
        result = runner.invoke(app, ["config", "init", "--help"])
        assert result.exit_code == 0
        assert "Initialize" in result.output

    def test_config_set_help(self) -> None:
        """Test config set command help."""
        result = runner.invoke(app, ["config", "set", "--help"])
        assert result.exit_code == 0
        assert "Set" in result.output

    def test_config_validate_help(self) -> None:
        """Test config validate command help."""
        result = runner.invoke(app, ["config", "validate", "--help"])
        assert result.exit_code == 0
        assert "Validate" in result.output


class TestStatusCommands:
    """Tests for status command group."""

    def test_status_executions_help(self) -> None:
        """Test status executions command help."""
        result = runner.invoke(app, ["status", "executions", "--help"])
        assert result.exit_code == 0
        assert "List" in result.output

    def test_status_execution_help(self) -> None:
        """Test status execution command help."""
        result = runner.invoke(app, ["status", "execution", "--help"])
        assert result.exit_code == 0
        assert "details" in result.output.lower()

    def test_status_health_help(self) -> None:
        """Test status health command help."""
        result = runner.invoke(app, ["status", "health", "--help"])
        assert result.exit_code == 0
        assert "health" in result.output.lower()

    def test_status_health_runs(self) -> None:
        """Test status health command execution."""
        result = runner.invoke(app, ["status", "health"])
        assert result.exit_code == 0
        assert "System Health" in result.output


class TestMCPCommands:
    """Tests for mcp command group."""

    def test_mcp_command_group_registered(self) -> None:
        """Test that mcp command group is registered."""
        result = runner.invoke(app, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "MCP" in result.output

    def test_mcp_serve_help(self) -> None:
        """Test mcp serve command help."""
        result = runner.invoke(app, ["mcp", "serve", "--help"])
        assert result.exit_code == 0
        assert "transport" in result.output.lower()
        assert "port" in result.output.lower()
        assert "runtime" in result.output.lower()
        assert "llm-backend" in result.output.lower()

    def test_mcp_info(self) -> None:
        """Test mcp info command."""
        result = runner.invoke(app, ["mcp", "info"])
        assert result.exit_code == 0
        assert "ouroboros-mcp" in result.output
        assert "ouroboros_execute_seed" in result.output


class TestTUICommands:
    """Tests for tui command group."""

    def test_tui_command_group_registered(self) -> None:
        """Test that tui command group is registered."""
        result = runner.invoke(app, ["tui", "--help"])
        assert result.exit_code == 0
        assert "Interactive TUI monitor" in result.output

    def test_tui_monitor_help(self) -> None:
        """Test tui monitor command help."""
        import re

        result = runner.invoke(app, ["tui", "monitor", "--help"])
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output).lower()
        assert "db-path" in plain
        assert "monitor" in plain


class TestShorthandCommands:
    """Tests for CLI shorthand/convenience commands (v0.8.0+ UX redesign)."""

    def test_run_shorthand_falls_back_to_workflow(self, tmp_path: Path) -> None:
        """Test that 'ouroboros run seed.yaml' is equivalent to 'ouroboros run workflow seed.yaml'."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        mock_run_orchestrator = AsyncMock()

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=mock_run_orchestrator,
        ):
            runner.invoke(app, ["run", str(seed_file)])

        # Should invoke workflow command (orchestrator by default calls _run_orchestrator)
        mock_run_orchestrator.assert_awaited_once()

    def test_run_shorthand_with_no_orchestrator(self, tmp_path: Path) -> None:
        """Test that 'ouroboros run seed.yaml --no-orchestrator' uses placeholder mode."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        result = runner.invoke(app, ["run", str(seed_file), "--no-orchestrator"])

        assert result.exit_code == 0
        assert "Would execute" in result.output

    def test_run_explicit_workflow_still_works(self, tmp_path: Path) -> None:
        """Test backward compat: 'ouroboros run workflow seed.yaml' still works."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        result = runner.invoke(app, ["run", "workflow", str(seed_file), "--no-orchestrator"])

        assert result.exit_code == 0
        assert "Would execute" in result.output

    def test_run_workflow_accepts_hermes_runtime_override(self, tmp_path: Path) -> None:
        """Hermes should be accepted as a CLI runtime choice."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        result = runner.invoke(
            app,
            ["run", "workflow", str(seed_file), "--runtime", "hermes", "--no-orchestrator"],
        )

        assert result.exit_code == 0
        assert "Would execute" in result.output

    def test_run_resume_subcommand_still_works(self) -> None:
        """Test backward compat: 'ouroboros run resume' still works."""
        result = runner.invoke(app, ["run", "resume"])
        assert result.exit_code == 0

    def test_init_shorthand_falls_back_to_start(self) -> None:
        """Test that 'ouroboros init <context>' routes to 'ouroboros init start <context>'."""
        result = runner.invoke(app, ["init", "start", "--help"])

        # The shorthand should show the same help as the explicit command
        result2 = runner.invoke(app, ["init", "--help"])
        # Both should be accessible
        assert result.exit_code == 0
        assert result2.exit_code == 0

    def test_init_list_subcommand_still_works(self) -> None:
        """Test backward compat: 'ouroboros init list' still routes to list."""
        with patch("ouroboros.cli.commands.init.create_llm_adapter"):
            with patch(
                "ouroboros.cli.commands.init.InterviewEngine.list_interviews",
                new=AsyncMock(return_value=[]),
            ):
                result = runner.invoke(app, ["init", "list"])
                assert result.exit_code == 0

    def test_monitor_top_level_alias(self) -> None:
        """Test that 'ouroboros monitor' is a shorthand for 'ouroboros tui monitor'."""
        result = runner.invoke(app, ["monitor", "--help"])
        # Should show monitor help (hidden command but still accessible)
        assert result.exit_code == 0

    def test_orchestrator_is_default(self, tmp_path: Path) -> None:
        """Test that orchestrator mode is the default for 'run workflow'."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")

        mock_run_orchestrator = AsyncMock()

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=mock_run_orchestrator,
        ):
            # No --orchestrator flag needed
            runner.invoke(app, ["run", "workflow", str(seed_file)])

        # _run_orchestrator should be awaited by the default orchestrator path
        mock_run_orchestrator.assert_awaited_once()
