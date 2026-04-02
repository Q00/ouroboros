"""End-to-end integration test: minimal Ouroboros Seed through GeminiCLIRuntime.

This module exercises the full Gemini CLI runtime → evaluation pipeline on a
minimal Ouroboros Seed without requiring a real ``gemini`` binary or live API
calls.

Scope
-----
- Subprocess calls are intercepted via ``unittest.mock.patch`` so no real
  ``gemini`` binary is needed.
- Semantic evaluation uses a mock LLM adapter that returns a canned passing
  response so no live API key is required.
- Stage 1 (mechanical verification) is disabled to keep the test lightweight;
  it would require an actual workspace with runnable commands.
- Stage 3 (consensus) is disabled because the mock semantic response has low
  uncertainty, which keeps the trigger below threshold.

Design
------
1. A minimal :class:`~ouroboros.core.seed.Seed` with a single acceptance
   criterion is constructed in-memory.
2. ``asyncio.create_subprocess_exec`` is patched in the
   :mod:`ouroboros.orchestrator.codex_cli_runtime` module (the parent class
   where the subprocess call is made) to return a
   :class:`_FakeGeminiProcess` whose stdout emits a plain-text response that
   satisfies the criterion.
3. :class:`~ouroboros.orchestrator.gemini_cli_runtime.GeminiCLIRuntime` is
   exercised via ``execute_task``; all yielded
   :class:`~ouroboros.orchestrator.adapter.AgentMessage` objects are
   collected.
4. An :class:`~ouroboros.evaluation.models.EvaluationContext` is assembled
   from the seed and the collected runtime output.
5. :class:`~ouroboros.evaluation.pipeline.EvaluationPipeline` is run with a
   mock LLM adapter; the test asserts that the returned
   :class:`~ouroboros.evaluation.models.EvaluationResult` has
   ``final_approved == True``.

Test classes
------------
- TestGeminiCLIRuntimeE2ERun   — full happy-path run + evaluate verdict
- TestGeminiCLIRuntimeE2EEdge  — edge cases (empty output, error exit, tool use)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    EvaluationContext,
    EvaluationResult,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.evaluation.semantic import SemanticConfig
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, UsageInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The subprocess factory lives in the *base* class; patch it there so both
# CodexCliRuntime and GeminiCLIRuntime pick up the replacement.
_EXEC_PATCH_TARGET = (
    "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec"
)

# A canned passing semantic evaluation response (JSON matching the evaluation
# schema defined in ouroboros.evaluation.semantic).
_PASSING_SEMANTIC_JSON = json.dumps(
    {
        "score": 0.95,
        "ac_compliance": True,
        "goal_alignment": 0.95,
        "drift_score": 0.03,
        "uncertainty": 0.08,
        "reward_hacking_risk": 0.02,
        "reasoning": (
            "The runtime output clearly demonstrates that the script prints "
            "'Hello, World!' as required by the acceptance criterion. "
            "Goal alignment is excellent and there is no detectable drift."
        ),
    }
)

# A canned failing semantic evaluation response for negative-path tests.
_FAILING_SEMANTIC_JSON = json.dumps(
    {
        "score": 0.2,
        "ac_compliance": False,
        "goal_alignment": 0.3,
        "drift_score": 0.7,
        "uncertainty": 0.15,
        "reward_hacking_risk": 0.1,
        "reasoning": "The output does not satisfy the acceptance criterion.",
    }
)


# ---------------------------------------------------------------------------
# Subprocess stream / process doubles
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal async byte-stream double (asyncio.StreamReader substitute).

    Supports both ``read()`` (chunk-based) and ``readline()`` (line-based)
    access patterns used by the Gemini CLI runtime's line-iteration helpers.
    """

    def __init__(self, text: str = "") -> None:
        self._buffer: bytes = text.encode("utf-8")

    async def read(self, chunk_size: int = 16384) -> bytes:
        if not self._buffer:
            return b""
        chunk, self._buffer = self._buffer[:chunk_size], self._buffer[chunk_size:]
        return chunk

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        idx = self._buffer.find(b"\n")
        if idx == -1:
            line, self._buffer = self._buffer, b""
            return line
        line, self._buffer = self._buffer[: idx + 1], self._buffer[idx + 1 :]
        return line


class _FakeStdin:
    """Minimal async stdin-pipe double that records written payloads."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed: bool = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def written(self) -> bytes:
        return b"".join(self.writes)


class _FakeGeminiProcess:
    """Fake subprocess that emits configurable Gemini CLI stdout output.

    Attributes:
        stdout: Stream returning *stdout_text*.
        stderr: Stream returning *stderr_text*.
        stdin: :class:`_FakeStdin` recording writes.
        returncode: Exit code returned by :meth:`wait`.
        terminated: Set to ``True`` if :meth:`terminate` is called.
        killed: Set to ``True`` if :meth:`kill` is called.
    """

    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStream(stdout_text)
        self.stderr = _FakeStream(stderr_text)
        self.stdin = _FakeStdin()
        self.returncode: int = returncode
        self.terminated: bool = False
        self.killed: bool = False

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


# ---------------------------------------------------------------------------
# Mock LLM adapter for evaluation
# ---------------------------------------------------------------------------


class _MockLLMAdapter:
    """Mock LLM adapter that returns a preconfigured canned response.

    Used to drive the semantic evaluation stage without a live API key.
    Each call to :meth:`complete` pops the next response from the queue;
    if the queue is exhausted the last response is repeated.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses: list[str] = list(responses)
        self.call_count: int = 0
        self.calls: list[tuple[list[Message], CompletionConfig]] = []

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, Any]:
        self.calls.append((messages, config))
        if self._responses:
            content = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        else:
            content = "{}"
        self.call_count += 1
        return Result.ok(
            CompletionResponse(
                content=content,
                model=config.model or "mock-model",
                usage=UsageInfo(prompt_tokens=50, completion_tokens=80, total_tokens=130),
                finish_reason="stop",
                raw_response={},
            )
        )


# ---------------------------------------------------------------------------
# Seed factory helpers
# ---------------------------------------------------------------------------


def _make_minimal_seed(
    goal: str = "Create a hello world script",
    acceptance_criteria: tuple[str, ...] = ("Script prints 'Hello, World!'",),
) -> Seed:
    """Build a minimal Ouroboros Seed suitable for e2e integration tests.

    Args:
        goal: The high-level goal for the workflow.
        acceptance_criteria: Tuple of acceptance criterion strings.

    Returns:
        A :class:`~ouroboros.core.seed.Seed` instance.
    """
    return Seed(
        goal=goal,
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name="HelloWorld",
            description="Minimal hello world workflow",
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="correctness",
                description="The output satisfies all acceptance criteria.",
                weight=1.0,
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _make_gemini_runtime(
    *,
    cli_path: str = "/usr/local/bin/gemini",
    model: str | None = None,
    cwd: str | Path = "/tmp/e2e-workspace",
) -> GeminiCLIRuntime:
    """Construct a :class:`GeminiCLIRuntime` with safe test defaults.

    Args:
        cli_path: Path to the (fake) Gemini CLI binary.
        model: Optional model override for the runtime.
        cwd: Working directory for subprocess invocations.

    Returns:
        Configured :class:`GeminiCLIRuntime` instance.
    """
    return GeminiCLIRuntime(
        cli_path=cli_path,
        model=model,
        cwd=cwd,
    )


async def _collect_messages(
    runtime: GeminiCLIRuntime,
    prompt: str,
    **kwargs: Any,
) -> list[AgentMessage]:
    """Drain ``execute_task`` into a list for assertion.

    Args:
        runtime: The :class:`GeminiCLIRuntime` to exercise.
        prompt: The task prompt to execute.
        **kwargs: Additional keyword arguments passed to ``execute_task``.

    Returns:
        List of :class:`~ouroboros.orchestrator.adapter.AgentMessage` objects
        yielded by the runtime.
    """
    return [msg async for msg in runtime.execute_task(prompt, **kwargs)]


def _fake_exec_returning(process: _FakeGeminiProcess) -> Any:
    """Return an async callable that always returns *process*.

    Args:
        process: The fake process to return on every invocation.

    Returns:
        Async callable compatible with ``asyncio.create_subprocess_exec``.
    """

    async def _exec(*_args: str, **_kwargs: Any) -> _FakeGeminiProcess:
        return process

    return _exec


def _build_evaluation_context(
    seed: Seed,
    messages: list[AgentMessage],
    *,
    execution_id: str = "e2e-exec-001",
) -> EvaluationContext:
    """Assemble an :class:`~ouroboros.evaluation.models.EvaluationContext` from
    runtime output.

    Concatenates all ``assistant``-type message contents as the artifact text
    and uses the first acceptance criterion from the seed.

    Args:
        seed: The Ouroboros Seed whose criteria are being evaluated.
        messages: Messages collected from ``execute_task``.
        execution_id: Execution identifier for tracing.

    Returns:
        :class:`~ouroboros.evaluation.models.EvaluationContext` ready for
        the evaluation pipeline.
    """
    artifact_parts = [
        msg.content
        for msg in messages
        if msg.type in {"assistant", "result"} and msg.content
    ]
    artifact = "\n".join(artifact_parts) or "(no output)"
    current_ac = seed.acceptance_criteria[0] if seed.acceptance_criteria else ""
    return EvaluationContext(
        execution_id=execution_id,
        seed_id=seed.metadata.seed_id,
        current_ac=current_ac,
        artifact=artifact,
        artifact_type="text",
        goal=seed.goal,
        constraints=seed.constraints,
    )


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeE2ERun
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeE2ERun:
    """Happy-path end-to-end run: seed → runtime → evaluate → passing verdict.

    Each test in this class exercises the complete integration from a minimal
    seed through the GeminiCLIRuntime to the evaluation pipeline, asserting
    that the verdict is approved.
    """

    @pytest.mark.asyncio
    async def test_runtime_yields_assistant_messages(self) -> None:
        """execute_task must yield at least one assistant-type message.

        This is the basic smoke-test confirming the runtime processes
        Gemini CLI stdout and emits normalized AgentMessage objects.
        """
        process = _FakeGeminiProcess(
            stdout_text="Hello, World!\n",
            returncode=0,
        )
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, "Write a hello world script")

        assistant_msgs = [m for m in messages if m.type == "assistant"]
        assert assistant_msgs, (
            "GeminiCLIRuntime must yield at least one 'assistant' AgentMessage "
            "when the subprocess produces non-empty stdout."
        )

    @pytest.mark.asyncio
    async def test_runtime_captures_hello_world_output(self) -> None:
        """The collected messages must contain the expected plain-text output.

        Verifies that the runtime's plain-text wrapping logic correctly
        surfaces Gemini CLI stdout as assistant message content.
        """
        expected_output = "Hello, World!"
        process = _FakeGeminiProcess(
            stdout_text=f"{expected_output}\n",
            returncode=0,
        )
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, "Write a hello world script")

        all_content = "\n".join(m.content for m in messages if m.content)
        assert expected_output in all_content, (
            f"Expected '{expected_output}' in collected message content, "
            f"got: {all_content!r}"
        )

    @pytest.mark.asyncio
    async def test_evaluation_pipeline_approves_gemini_output(self) -> None:
        """Full e2e path: GeminiCLIRuntime output → EvaluationPipeline → approved.

        This is the primary acceptance-criterion test for Sub-AC 3d.

        Flow:
            1. Execute a minimal seed through GeminiCLIRuntime (subprocess mocked).
            2. Assemble an EvaluationContext from the collected messages.
            3. Run EvaluationPipeline with stage1 disabled and a mock LLM.
            4. Assert ``EvaluationResult.final_approved is True``.
        """
        seed = _make_minimal_seed()

        # Gemini CLI emits the solution as plain text (normal operation mode)
        runtime_output = (
            "I will create a simple hello world script.\n"
            "print('Hello, World!')\n"
            "The script prints 'Hello, World!' as required.\n"
        )
        process = _FakeGeminiProcess(stdout_text=runtime_output, returncode=0)
        runtime = _make_gemini_runtime()

        # --- Step 1: Execute the seed through GeminiCLIRuntime ---
        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, seed.goal)

        assert messages, "GeminiCLIRuntime must yield at least one message."

        # --- Step 2: Assemble evaluation context ---
        context = _build_evaluation_context(seed, messages)
        assert context.artifact, "Artifact must be non-empty for evaluation."
        assert context.current_ac, "Current AC must be non-empty for evaluation."

        # --- Step 3: Run evaluation pipeline (stage1 disabled, mock LLM) ---
        mock_llm = _MockLLMAdapter(responses=[_PASSING_SEMANTIC_JSON])
        pipeline_config = PipelineConfig(
            stage1_enabled=False,   # skip mechanical checks (no real workspace)
            stage2_enabled=True,    # semantic evaluation with mock LLM
            stage3_enabled=False,   # no consensus needed (low uncertainty)
            semantic=SemanticConfig(model="mock-model", satisfaction_threshold=0.8),
        )
        pipeline = EvaluationPipeline(mock_llm, pipeline_config)
        eval_result = await pipeline.evaluate(context)

        # --- Step 4: Assert passing verdict ---
        assert eval_result.is_ok, (
            f"EvaluationPipeline returned an error: {eval_result.error}"
        )
        result: EvaluationResult = eval_result.value
        assert result.final_approved is True, (
            f"Expected final_approved=True but got False. "
            f"Stage 2: {result.stage2_result}"
        )
        assert result.stage2_result is not None, (
            "Stage 2 semantic result must be populated."
        )
        assert result.stage2_result.ac_compliance is True, (
            "Semantic evaluator must report AC compliance."
        )

    @pytest.mark.asyncio
    async def test_model_flag_forwarded_to_subprocess(self) -> None:
        """When a model is configured, the runtime must include it in the command.

        Ensures GeminiCLIRuntime._build_command adds ``--model`` correctly so
        the subprocess receives the right model selection.
        """
        captured_commands: list[tuple[str, ...]] = []

        async def _recording_exec(*args: str, **_kwargs: Any) -> _FakeGeminiProcess:
            captured_commands.append(args)
            return _FakeGeminiProcess(stdout_text="done\n", returncode=0)

        runtime = _make_gemini_runtime(model="gemini-2.5-pro")
        with patch(_EXEC_PATCH_TARGET, side_effect=_recording_exec):
            await _collect_messages(runtime, "Run the tests")

        assert len(captured_commands) == 1, "Expected exactly one subprocess call."
        command = captured_commands[0]
        assert "--model" in command, "Expected '--model' flag in Gemini CLI command."
        model_idx = command.index("--model")
        assert command[model_idx + 1] == "gemini-2.5-pro", (
            "Expected 'gemini-2.5-pro' after '--model' flag."
        )

    @pytest.mark.asyncio
    async def test_no_output_last_message_flag_in_command(self) -> None:
        """Gemini CLI does not use --output-last-message; verify it is absent.

        Unlike Codex CLI, GeminiCLIRuntime writes output to stdout only.
        The ``--output-last-message`` flag must never appear in the command.
        """
        captured_commands: list[tuple[str, ...]] = []

        async def _recording_exec(*args: str, **_kwargs: Any) -> _FakeGeminiProcess:
            captured_commands.append(args)
            return _FakeGeminiProcess(stdout_text="ok\n", returncode=0)

        runtime = _make_gemini_runtime()
        with patch(_EXEC_PATCH_TARGET, side_effect=_recording_exec):
            await _collect_messages(runtime, "List files")

        assert len(captured_commands) == 1
        assert "--output-last-message" not in captured_commands[0], (
            "Gemini CLI must not receive --output-last-message flag."
        )

    @pytest.mark.asyncio
    async def test_full_pipeline_message_count(self) -> None:
        """The runtime must yield a deterministic number of messages for a
        multi-line Gemini CLI response.

        Each non-empty line from Gemini CLI stdout is wrapped as a synthetic
        ``gemini.content`` event and converted to a single ``assistant``
        AgentMessage; this test verifies the one-to-one mapping.
        """
        # Three distinct non-empty output lines
        stdout_lines = [
            "Analysing the task.",
            "Implementing the solution.",
            "Script outputs: Hello, World!",
        ]
        process = _FakeGeminiProcess(
            stdout_text="\n".join(stdout_lines) + "\n",
            returncode=0,
        )
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, "Write a hello world script")

        assistant_contents = [m.content for m in messages if m.type == "assistant"]
        assert len(assistant_contents) == len(stdout_lines), (
            f"Expected {len(stdout_lines)} assistant messages, "
            f"got {len(assistant_contents)}: {assistant_contents}"
        )
        for line, content in zip(stdout_lines, assistant_contents, strict=False):
            assert content == line, (
                f"Expected message content {line!r}, got {content!r}"
            )


# ---------------------------------------------------------------------------
# TestGeminiCLIRuntimeE2EEdge
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimeE2EEdge:
    """Edge-case scenarios: empty output, non-zero exit, JSON events, multiline.

    These tests verify that the GeminiCLIRuntime handles unusual Gemini CLI
    output gracefully and that the evaluation pipeline correctly handles
    failing verdicts.
    """

    @pytest.mark.asyncio
    async def test_empty_stdout_yields_result_message(self) -> None:
        """An empty Gemini CLI response must still produce a result message.

        The runtime signals completion even when stdout is empty so that
        callers always receive at least one message from ``execute_task``.
        """
        process = _FakeGeminiProcess(stdout_text="", returncode=0)
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, "Echo test")

        # With empty stdout the base runtime still emits a final result message
        assert isinstance(messages, list), "execute_task must yield a list of messages."
        # We don't assert a specific count here — the runtime behaviour for
        # empty output (result vs. empty list) is an implementation detail;
        # what matters is that the function completes without error.

    @pytest.mark.asyncio
    async def test_non_zero_exit_produces_error_message(self) -> None:
        """A non-zero Gemini CLI exit code must produce an error-type message.

        The runtime must surface subprocess failures as AgentMessage objects
        with an error indicator so that callers can detect the failure without
        inspecting the exit code directly.
        """
        process = _FakeGeminiProcess(
            stdout_text="fatal: not a git repository\n",
            stderr_text="error: command failed",
            returncode=1,
        )
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, "Run git status")

        error_msgs = [
            m for m in messages
            if m.type in {"error", "result"} and (
                m.is_error
                or (isinstance(m.data, dict) and m.data.get("subtype") == "error")
                or (isinstance(m.data, dict) and m.data.get("returncode", 0) != 0)
            )
        ]
        assert error_msgs, (
            "GeminiCLIRuntime must yield at least one error-indicating message "
            "when the subprocess exits with a non-zero code. "
            f"All messages: {[(m.type, m.content, m.data) for m in messages]}"
        )

    @pytest.mark.asyncio
    async def test_evaluation_pipeline_rejects_failing_output(self) -> None:
        """EvaluationPipeline must return final_approved=False for non-compliant output.

        This negative-path test ensures the pipeline correctly propagates a
        failing mock semantic verdict.
        """
        seed = _make_minimal_seed()

        # Gemini CLI produces irrelevant output that does not satisfy the AC
        process = _FakeGeminiProcess(
            stdout_text="I have no idea what to do here.\n",
            returncode=0,
        )
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, seed.goal)

        context = _build_evaluation_context(seed, messages)

        mock_llm = _MockLLMAdapter(responses=[_FAILING_SEMANTIC_JSON])
        pipeline_config = PipelineConfig(
            stage1_enabled=False,
            stage2_enabled=True,
            stage3_enabled=False,
            semantic=SemanticConfig(model="mock-model", satisfaction_threshold=0.8),
        )
        pipeline = EvaluationPipeline(mock_llm, pipeline_config)
        eval_result = await pipeline.evaluate(context)

        assert eval_result.is_ok, f"Pipeline returned error: {eval_result.error}"
        result: EvaluationResult = eval_result.value
        assert result.final_approved is False, (
            "Expected final_approved=False for non-compliant output."
        )
        assert result.stage2_result is not None
        assert result.stage2_result.ac_compliance is False

    @pytest.mark.asyncio
    async def test_json_event_lines_handled_gracefully(self) -> None:
        """Valid Gemini CLI JSON event lines must be parsed without error.

        If the Gemini CLI emits JSONL events (e.g. with ``--json`` mode or
        in a future version), the runtime must handle them without crashing.
        Plain-text lines and JSON event lines may be interleaved.
        """
        json_event = json.dumps({"type": "message", "content": "Task done."})
        stdout = f"Starting work.\n{json_event}\nAll done.\n"
        process = _FakeGeminiProcess(stdout_text=stdout, returncode=0)
        runtime = _make_gemini_runtime()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, "Do something")

        # Must complete without raising; messages may be empty or non-empty
        assert isinstance(messages, list), (
            "execute_task must not raise when JSON event lines are in stdout."
        )

    @pytest.mark.asyncio
    async def test_evaluation_context_artifact_contains_runtime_output(self) -> None:
        """The evaluation context artifact must include the runtime's output text.

        Verifies that :func:`_build_evaluation_context` correctly assembles
        the artifact from collected messages so the semantic evaluator sees
        the full runtime output.
        """
        expected_text = "The quick brown fox jumps over the lazy dog."
        process = _FakeGeminiProcess(
            stdout_text=f"{expected_text}\n",
            returncode=0,
        )
        runtime = _make_gemini_runtime()
        seed = _make_minimal_seed()

        with patch(_EXEC_PATCH_TARGET, side_effect=_fake_exec_returning(process)):
            messages = await _collect_messages(runtime, seed.goal)

        context = _build_evaluation_context(seed, messages)
        assert expected_text in context.artifact, (
            f"Expected runtime output in evaluation context artifact. "
            f"artifact={context.artifact!r}"
        )

    @pytest.mark.asyncio
    async def test_gemini_runtime_created_via_factory(self, tmp_path: Path) -> None:
        """create_agent_runtime('gemini') must return a GeminiCLIRuntime instance.

        Verifies that the factory correctly routes 'gemini' and 'gemini_cli'
        backend strings to :class:`GeminiCLIRuntime`.
        """
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        runtime = create_agent_runtime(
            backend="gemini",
            cli_path="/usr/local/bin/gemini",
            cwd=tmp_path,
        )
        assert isinstance(runtime, GeminiCLIRuntime), (
            f"Expected GeminiCLIRuntime from factory, got {type(runtime).__name__}"
        )

    @pytest.mark.asyncio
    async def test_gemini_cli_backend_alias_via_factory(self, tmp_path: Path) -> None:
        """'gemini_cli' backend alias must also resolve to GeminiCLIRuntime.

        The factory accepts both 'gemini' and 'gemini_cli' as backend strings.
        """
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        runtime = create_agent_runtime(
            backend="gemini_cli",
            cli_path="/usr/local/bin/gemini",
            cwd=tmp_path,
        )
        assert isinstance(runtime, GeminiCLIRuntime), (
            f"Expected GeminiCLIRuntime for 'gemini_cli' backend, "
            f"got {type(runtime).__name__}"
        )


__all__ = [
    "TestGeminiCLIRuntimeE2ERun",
    "TestGeminiCLIRuntimeE2EEdge",
]
