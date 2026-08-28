"""Unit tests for orchestrator runtime factory helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.gjc_runtime import GjcRuntime
from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime
from ouroboros.orchestrator.runtime_factory import (
    create_agent_runtime,
    create_agent_runtime_async,
    resolve_agent_runtime_backend,
)
from ouroboros.orchestrator.zcode_cli_runtime import ZcodeCLIRuntime

_EXPECTED_CANONICAL_PROJECT_CWD = str(Path("/tmp/project").resolve())


class TestResolveAgentRuntimeBackend:
    """Tests for backend resolution."""

    def test_resolve_explicit_codex_alias(self) -> None:
        """Normalizes the codex_cli alias to codex."""
        assert resolve_agent_runtime_backend("codex_cli") == "codex"

    def test_resolve_uses_config_helper(self) -> None:
        """Falls back to config/env helper when no explicit backend is provided."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_agent_runtime_backend",
            return_value="codex",
        ):
            assert resolve_agent_runtime_backend() == "codex"

    def test_resolve_opencode_aliases(self) -> None:
        """OpenCode aliases normalize to opencode."""
        assert resolve_agent_runtime_backend("opencode") == "opencode"
        assert resolve_agent_runtime_backend("opencode_cli") == "opencode"

    def test_resolve_gjc_aliases(self) -> None:
        """GJC aliases normalize to gjc."""
        assert resolve_agent_runtime_backend("gjc") == "gjc"
        assert resolve_agent_runtime_backend("gajae-code") == "gjc"
        assert resolve_agent_runtime_backend("gajae_code") == "gjc"

    def test_resolve_hermes_aliases(self) -> None:
        """Hermes aliases normalize to hermes."""
        assert resolve_agent_runtime_backend("hermes") == "hermes"
        assert resolve_agent_runtime_backend("hermes_cli") == "hermes"

    def test_resolve_rejects_unknown_backend(self) -> None:
        """Raises for unsupported backends."""
        with pytest.raises(ValueError):
            resolve_agent_runtime_backend("unknown")


@pytest.mark.asyncio
async def test_async_factory_does_not_block_event_loop_during_pi_capability_probe() -> None:
    def slow_probe(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        time.sleep(0.2)
        return subprocess.CompletedProcess(
            args=["/tmp/pi", "--help"],
            returncode=0,
            stdout="Usage: pi [options]",
            stderr="",
        )

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with patch("ouroboros.orchestrator.pi_runtime.subprocess.run", side_effect=slow_probe):
        runtime_task = asyncio.create_task(
            create_agent_runtime_async(
                create_agent_runtime,
                backend="pi",
                cli_path="/tmp/pi",
                cwd="/tmp/project",
            )
        )
        await asyncio.sleep(0.02)
        heartbeat_elapsed = loop.time() - started_at
        runtime = await runtime_task

    assert heartbeat_elapsed < 0.1
    assert runtime.runtime_backend == "pi"


class TestCreateAgentRuntime:
    """Tests for runtime construction."""

    @pytest.mark.parametrize(
        "backend",
        [
            "claude",
            "codex",
            "codex_mcp",
            "claude_mcp",
            "copilot",
            "gemini",
            "zcode",
            "hermes",
            "kiro",
            "opencode",
            "goose",
            "pi",
            "omp",
            "gjc",
            "antigravity",
            "grok",
        ],
    )
    @pytest.mark.asyncio
    async def test_every_runtime_fails_closed_before_effects_when_cwd_is_unresolved(
        self,
        backend: str,
    ) -> None:
        cwd_calls: list[int] = []

        def moving_process_cwd() -> str:
            cwd_calls.append(len(cwd_calls))
            if len(cwd_calls) == 1:
                raise FileNotFoundError("launch cwd unavailable")
            return "/tmp/unselected-later-cwd"

        with patch(
            "ouroboros.orchestrator.adapter.os.getcwd",
            side_effect=moving_process_cwd,
        ):
            runtime = create_agent_runtime(
                backend=backend,
                cli_path="/tmp/runtime-cli",
            )
            messages = [message async for message in runtime.execute_task("must not run")]

        assert len(messages) == 1
        assert messages[0].is_error
        assert messages[0].data["error_type"] == "WorkerCwdUnavailable"
        assert cwd_calls == [0]

    def test_omitted_cwd_absence_is_shared_without_reinterpretation(self) -> None:
        cwd_results: list[OSError | str] = [
            FileNotFoundError("launch cwd unavailable"),
            "/tmp/dispatcher-late",
            "/tmp/runtime-later",
        ]

        def moving_process_cwd() -> str:
            result = cwd_results.pop(0)
            if isinstance(result, OSError):
                raise result
            return result

        with patch(
            "ouroboros.orchestrator.adapter.os.getcwd",
            side_effect=moving_process_cwd,
        ):
            runtime = create_agent_runtime(
                backend="codex",
                cli_path="/tmp/codex",
            )

        assert runtime.working_directory is None
        assert runtime._skill_dispatcher.__self__._cwd is None
        assert cwd_results == ["/tmp/dispatcher-late", "/tmp/runtime-later"]

    def test_explicit_cwd_resolution_failure_never_uses_process_cwd(self) -> None:
        with (
            patch(
                "ouroboros.orchestrator.adapter.Path.resolve",
                side_effect=FileNotFoundError("requested workspace unavailable"),
            ),
            patch(
                "ouroboros.orchestrator.adapter.os.getcwd",
                return_value="/fallback/process-cwd",
            ) as getcwd,
        ):
            with pytest.raises(FileNotFoundError, match="requested workspace unavailable"):
                create_agent_runtime(
                    backend="codex",
                    cli_path="/tmp/codex",
                    cwd="/requested/workspace",
                )

        getcwd.assert_not_called()

    def test_create_claude_runtime(self) -> None:
        """Creates the Claude adapter for the claude backend."""
        runtime = create_agent_runtime(backend="claude", permission_mode="acceptEdits")
        assert isinstance(runtime, ClaudeAgentAdapter)
        assert runtime._cwd

    def test_create_codex_runtime_uses_configured_cli_path(self) -> None:
        """Creates Codex runtime with the configured CLI path."""
        mock_dispatcher = object()

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_codex_cli_path",
                return_value="/tmp/codex",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=mock_dispatcher,
            ) as mock_create_dispatcher,
        ):
            runtime = create_agent_runtime(
                backend="codex",
                permission_mode="acceptEdits",
                cwd="/tmp/project",
            )

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._cli_path == "/tmp/codex"
        assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD
        assert runtime._skill_dispatcher is mock_dispatcher
        assert (
            mock_create_dispatcher.call_args.kwargs["cwd"].value == _EXPECTED_CANONICAL_PROJECT_CWD
        )
        assert mock_create_dispatcher.call_args.kwargs["runtime_backend"] == "codex"

    def test_relative_cwd_is_shared_with_runtime_and_dispatcher(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch = tmp_path / "launch"
        workspace = launch / "workspace"
        later = tmp_path / "later"
        workspace.mkdir(parents=True)
        later.mkdir()
        monkeypatch.chdir(launch)

        with patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=object(),
        ) as create_dispatcher:
            runtime = create_agent_runtime(
                backend="codex",
                cli_path="/tmp/codex",
                cwd="workspace",
            )
        monkeypatch.chdir(later)

        assert runtime.working_directory == str(workspace)
        assert create_dispatcher.call_args.kwargs["cwd"].value == str(workspace)

    def test_create_codex_runtime_propagates_runtime_profile(self) -> None:
        """``get_runtime_profile()`` must reach CodexCliRuntime via the factory.

        The runtime is the only place that translates the orchestrator
        ``runtime_profile`` into a Codex ``--profile`` argument, so a
        regression in the factory wiring would silently disable
        worker-subprocess isolation. Lock the path under test.
        """
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_runtime_profile",
                return_value="worker",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="codex", cwd="/tmp/project")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._runtime_profile == "worker"
        assert runtime._codex_profile == "ouroboros-worker"

    def test_create_copilot_runtime_propagates_runtime_profile(self) -> None:
        """``get_runtime_profile()`` must reach CopilotCliRuntime via the factory."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_runtime_profile",
                return_value="worker",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.get_copilot_cli_path",
                return_value="/tmp/copilot",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="copilot", cwd="/tmp/project")

        assert isinstance(runtime, CopilotCliRuntime)
        assert runtime._runtime_profile == "worker"
        assert runtime._copilot_agent == "ouroboros-worker"

    def test_create_codex_runtime_default_profile_is_none(self) -> None:
        """Unset profile must remain unset all the way through to the runtime."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_runtime_profile",
                return_value=None,
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="codex", cwd="/tmp/project")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._runtime_profile is None
        assert runtime._codex_profile is None

    def test_create_claude_runtime_uses_factory_cwd_and_cli_path(self) -> None:
        """Claude runtime receives the same construction options as other backends."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_cli_path",
            return_value="/tmp/claude",
        ):
            runtime = create_agent_runtime(backend="claude", cwd="/tmp/project")

        assert isinstance(runtime, ClaudeAgentAdapter)
        assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD
        assert runtime._cli_path == "/tmp/claude"

    def test_create_opencode_runtime_uses_configured_cli_path(self) -> None:
        """Creates OpenCode runtime with the explicit CLI path."""
        runtime = create_agent_runtime(
            backend="opencode",
            permission_mode="acceptEdits",
            cwd="/tmp/project",
            cli_path="/tmp/opencode",
        )

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._cli_path == "/tmp/opencode"
        assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD

    def test_create_runtime_uses_configured_opencode_alias_when_backend_omitted(self) -> None:
        """Configured OpenCode aliases should resolve through the shared runtime factory."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_agent_runtime_backend",
                return_value="opencode_cli",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
                return_value="acceptEdits",
            ) as mock_get_permission_mode,
            patch(
                "ouroboros.orchestrator.runtime_factory.get_llm_backend",
                return_value="opencode",
            ),
        ):
            runtime = create_agent_runtime(cwd="/tmp/project")

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD
        assert runtime._permission_mode == "acceptEdits"
        assert mock_get_permission_mode.call_args.kwargs["backend"] == "opencode"

    def test_create_runtime_uses_configured_permission_mode(self) -> None:
        """Runtime factory uses config/env permission defaults when omitted."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
            return_value="bypassPermissions",
        ):
            runtime = create_agent_runtime(backend="codex")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._permission_mode == "bypassPermissions"

    def test_create_opencode_runtime_uses_backend_specific_permission_default(self) -> None:
        """OpenCode runtime asks the shared config helper for the OpenCode-specific mode."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
                return_value="bypassPermissions",
            ) as mock_get_permission_mode,
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="opencode")

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._permission_mode == "bypassPermissions"
        assert mock_get_permission_mode.call_args.kwargs["backend"] == "opencode"

    def test_create_runtime_uses_configured_llm_backend_when_omitted(self) -> None:
        """Runtime factory reuses config/env llm backend defaults for builtin tool dispatch."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_llm_backend",
                return_value="opencode",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="codex")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._llm_backend == "opencode"

    def test_create_hermes_runtime_uses_configured_cli_path(self) -> None:
        """Creates Hermes runtime with the configured CLI path and dispatcher context."""
        mock_dispatcher = object()

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_hermes_cli_path",
                return_value="/tmp/hermes",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=mock_dispatcher,
            ),
        ):
            runtime = create_agent_runtime(
                backend="hermes",
                permission_mode="acceptEdits",
                cwd="/tmp/project",
                llm_backend="codex",
            )

        assert isinstance(runtime, HermesCliRuntime)
        assert runtime._cli_path == "/tmp/hermes"
        assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD
        assert runtime._skill_dispatcher is mock_dispatcher
        assert runtime._llm_backend == "codex"

    def test_create_hermes_runtime_accepts_stream_timeout_overrides(self) -> None:
        """MCP seed execution can disable Hermes quiet-stream guards explicitly."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=object(),
        ):
            runtime = create_agent_runtime(
                backend="hermes",
                startup_output_timeout_seconds=0,
                stdout_idle_timeout_seconds=0,
            )

        assert isinstance(runtime, HermesCliRuntime)
        assert runtime._startup_output_timeout_seconds is None
        assert runtime._stdout_idle_timeout_seconds is None

    def test_create_codex_runtime_accepts_stream_timeout_overrides(self) -> None:
        """MCP seed execution can disable Codex quiet-stream guards explicitly."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=object(),
        ):
            runtime = create_agent_runtime(
                backend="codex",
                startup_output_timeout_seconds=0,
                stdout_idle_timeout_seconds=0,
            )

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._startup_output_timeout_seconds is None
        assert runtime._stdout_idle_timeout_seconds is None

    def test_opencode_runtime_always_uses_subprocess_mode(self) -> None:
        """OpenCodeRuntime always gets opencode_mode='subprocess' regardless of config.

        The runtime factory hardcodes 'subprocess' because OpenCodeRuntime
        runs `opencode run --pure` (no bridge plugin). Plugin mode is
        exclusively an MCP-server concern.
        """
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="opencode")

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._opencode_mode == "subprocess"

    def test_create_opencode_runtime_accepts_stdout_idle_timeout_override(self) -> None:
        """OpenCode seed execution can disable quiet-stream guards explicitly."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=object(),
        ):
            runtime = create_agent_runtime(
                backend="opencode",
                stdout_idle_timeout_seconds=0,
            )

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._stdout_idle_timeout_seconds is None

    def test_create_opencode_runtime_uses_configured_stdout_idle_timeout(self) -> None:
        """Factory wires the OpenCode-specific idle timeout config into runtime."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_opencode_stdout_idle_timeout_seconds",
                return_value=1800.0,
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="opencode")

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._stdout_idle_timeout_seconds == 1800.0

    def test_opencode_runtime_ignores_config_plugin_mode(self) -> None:
        """Even when config says plugin, runtime factory forces subprocess.

        Config might say opencode_mode=plugin (user set up plugin mode) but
        OpenCodeRuntime is standalone `ouroboros run` — no bridge, so
        handlers must not emit _subagent envelopes.
        """
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="opencode")

        assert isinstance(runtime, OpenCodeRuntime)
        assert runtime._opencode_mode == "subprocess"


def test_resolve_goose_aliases() -> None:
    """Goose aliases normalize to goose."""
    assert resolve_agent_runtime_backend("goose") == "goose"
    assert resolve_agent_runtime_backend("goose_cli") == "goose"


def test_create_goose_runtime_uses_configured_cli_path() -> None:
    """Creates Goose runtime with the configured CLI path."""
    from ouroboros.orchestrator.goose_runtime import GooseCliRuntime

    mock_dispatcher = object()

    with (
        patch(
            "ouroboros.orchestrator.runtime_factory.get_goose_cli_path",
            return_value="/tmp/goose",
        ),
        patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=mock_dispatcher,
        ) as mock_create_dispatcher,
    ):
        runtime = create_agent_runtime(
            backend="goose",
            permission_mode="auto",
            cwd="/tmp/project",
        )

    assert isinstance(runtime, GooseCliRuntime)
    assert runtime._cli_path == "/tmp/goose"
    assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD
    assert runtime._skill_dispatcher is mock_dispatcher
    assert mock_create_dispatcher.call_args.kwargs["runtime_backend"] == "goose"


def test_create_gjc_runtime_uses_configured_cli_path() -> None:
    """Creates GJC runtime with the configured CLI path and dispatcher context."""
    mock_dispatcher = object()

    with (
        patch(
            "ouroboros.orchestrator.runtime_factory.get_gjc_cli_path",
            return_value="/tmp/gjc",
        ),
        patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=mock_dispatcher,
        ) as mock_create_dispatcher,
    ):
        runtime = create_agent_runtime(
            backend="gjc",
            permission_mode="acceptEdits",
            cwd="/tmp/project",
            llm_backend="gjc",
        )

    assert isinstance(runtime, GjcRuntime)
    assert runtime._cli_path == "/tmp/gjc"
    assert runtime._cwd == _EXPECTED_CANONICAL_PROJECT_CWD
    assert runtime._skill_dispatcher is mock_dispatcher
    assert runtime._llm_backend == "gjc"
    assert mock_create_dispatcher.call_args.kwargs["runtime_backend"] == "gjc"


def test_create_gjc_runtime_accepts_stream_timeout_overrides() -> None:
    """GJC RPC runtime can disable quiet-stream guards explicitly."""
    with patch(
        "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
        return_value=object(),
    ):
        runtime = create_agent_runtime(
            backend="gjc",
            cli_path="/tmp/gjc",
            cwd="/tmp/project",
            startup_output_timeout_seconds=0,
            stdout_idle_timeout_seconds=0,
        )

    assert isinstance(runtime, GjcRuntime)
    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None


def test_create_zcode_runtime_accepts_stream_timeout_overrides() -> None:
    """Zcode runtime forwards the execute-seed stream-timeout overrides.

    ``zcode --prompt --json`` emits a single buffered JSON summary at
    completion — it produces no stdout until the run finishes. The MCP
    execute-seed path therefore calls ``create_agent_runtime(...,
    startup_output_timeout_seconds=0, stdout_idle_timeout_seconds=0)``
    to disable the inherited Codex quiet-stream guards; if the Zcode
    factory drops those overrides the subprocess is killed as "produced
    no stdout" after the 60s startup default, even on a healthy run.
    Regression for the bot review on PR #1568 (HEAD 1f31e1d1).
    """
    with patch(
        "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
        return_value=object(),
    ):
        runtime = create_agent_runtime(
            backend="zcode",
            cli_path="/tmp/zcode.cjs",
            cwd="/tmp/project",
            startup_output_timeout_seconds=0,
            stdout_idle_timeout_seconds=0,
        )

    assert isinstance(runtime, ZcodeCLIRuntime)
    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None
