"""Unit tests for the RLM Hermes inner-LM recursive-step adapter."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle, TaskResult
from ouroboros.rlm import (
    RLM_HERMES_DECOMPOSE_AC_MODE,
    RLM_HERMES_EXECUTE_ATOMIC_MODE,
    RLM_HERMES_OUTPUT_SCHEMA_VERSION,
    RLMHermesACDecompositionResult,
    RLMHermesInnerLMAdapter,
    RLMHermesRecursiveStepRequest,
    execute_hermes_recursive_step,
)

SYSTEM_PROMPT = "Hermes is the bounded inner LM. Do not call Ouroboros."


class _FakeHermesRuntime:
    """Capture one Hermes runtime call and return a prebuilt result."""

    runtime_backend = "hermes_cli"
    llm_backend = "claude_code"
    working_directory = "/tmp/ouroboros-rlm"
    permission_mode = "default"

    def __init__(self, result: Result[TaskResult, ProviderError]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        self.calls.append(
            {
                "prompt": prompt,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_handle": resume_handle,
                "resume_session_id": resume_session_id,
            }
        )
        return self.result


def _prompt(
    *,
    mode: str = RLM_HERMES_EXECUTE_ATOMIC_MODE,
    rlm_node_id: str = "rlm_node_1",
    ac_node_id: str = "ac_1",
    chunk_ids: tuple[str, ...] = ("chunk-a",),
) -> str:
    return json.dumps(
        {
            "schema_version": "rlm.hermes.input.v1",
            "mode": mode,
            "call_context": {
                "call_id": "rlm_call_1",
                "parent_call_id": "rlm_call_parent",
                "depth": 2,
            },
            "rlm_node": {"id": rlm_node_id},
            "ac_node": {"id": ac_node_id},
            "context": {
                "chunks": [
                    {
                        "chunk_id": chunk_id,
                        "source_path": "src/example.py",
                        "start_line": 1,
                        "end_line": 2,
                        "content": "VALUE = 1",
                    }
                    for chunk_id in chunk_ids
                ],
                "summaries": [],
                "child_results": [],
            },
            "trace": {
                "trace_id": "rlm_trace_1",
                "subcall_id": "rlm_subcall_prompt",
                "call_id": "rlm_call_1",
                "parent_trace_id": "rlm_trace_parent",
                "causal_parent_event_id": "rlm_call_parent",
                "depth": 2,
                "selected_chunk_ids": list(chunk_ids),
                "generated_child_ac_node_ids": ["ac_child_1"],
            },
        },
        sort_keys=True,
    )


def _atomic_output(
    *,
    mode: str = RLM_HERMES_EXECUTE_ATOMIC_MODE,
    rlm_node_id: str = "rlm_node_1",
    ac_node_id: str = "ac_1",
    chunk_id: str = "chunk-a",
) -> str:
    return json.dumps(
        {
            "schema_version": RLM_HERMES_OUTPUT_SCHEMA_VERSION,
            "mode": mode,
            "rlm_node_id": rlm_node_id,
            "ac_node_id": ac_node_id,
            "verdict": "passed",
            "confidence": 0.91,
            "result": {"summary": "Executed one bounded recursive step."},
            "evidence_references": [{"chunk_id": chunk_id, "claim": "Covered the chunk."}],
            "residual_gaps": [],
        },
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_inner_lm_adapter_executes_step_and_returns_output_metadata() -> None:
    """The adapter calls Hermes once and returns structured output plus metadata."""
    prompt = _prompt()
    runtime_handle = RuntimeHandle(backend="hermes_cli", native_session_id="session-native")
    final_message = _atomic_output()
    runtime = _FakeHermesRuntime(
        Result.ok(
            TaskResult(
                success=True,
                final_message=final_message,
                messages=(
                    AgentMessage(
                        type="result",
                        content=final_message,
                        data={"subtype": "success", "subcall_id": "rlm_subcall_runtime"},
                    ),
                ),
                session_id="session-native",
                resume_handle=runtime_handle,
            )
        )
    )

    request = RLMHermesRecursiveStepRequest.from_prompt(prompt)
    result = await execute_hermes_recursive_step(
        hermes_runtime=runtime,
        request=request,
        system_prompt=SYSTEM_PROMPT,
    )

    assert result.success is True
    assert result.output is not None
    assert result.output["result"]["summary"] == "Executed one bounded recursive step."
    assert result.error is None
    assert result.raw_completion == final_message
    assert result.metadata.mode == RLM_HERMES_EXECUTE_ATOMIC_MODE
    assert result.metadata.rlm_node_id == "rlm_node_1"
    assert result.metadata.ac_node_id == "ac_1"
    assert result.metadata.call_id == "rlm_call_1"
    assert result.metadata.subcall_id == "rlm_subcall_prompt"
    assert result.metadata.parent_call_id == "rlm_call_parent"
    assert result.metadata.trace_id == "rlm_trace_1"
    assert result.metadata.selected_chunk_ids == ("chunk-a",)
    assert result.metadata.generated_child_ac_node_ids == ("ac_child_1",)
    assert result.metadata.runtime_backend == "hermes_cli"
    assert result.metadata.llm_backend == "claude_code"
    assert result.metadata.session_id == "session-native"
    assert result.metadata.resume_handle_present is True
    assert result.metadata.task_success is True
    assert result.metadata.message_count == 1
    assert result.metadata.prompt_hash is not None
    assert result.metadata.response_hash is not None
    assert result.metadata.system_prompt_hash is not None

    assert runtime.calls == [
        {
            "prompt": prompt,
            "tools": [],
            "system_prompt": SYSTEM_PROMPT,
            "resume_handle": None,
            "resume_session_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_inner_lm_adapter_returns_adapter_errors_without_raising() -> None:
    """Provider-level failures are normalized as failed recursive-step results."""
    runtime = _FakeHermesRuntime(
        Result.err(
            ProviderError(
                "adapter down",
                provider="hermes",
                details={"error_type": "UnitTestFailure"},
            )
        )
    )

    result = await RLMHermesInnerLMAdapter(runtime, system_prompt=SYSTEM_PROMPT).execute_recursive_step(
        RLMHermesRecursiveStepRequest.from_prompt(_prompt())
    )

    assert result.success is False
    assert result.output is None
    assert result.error is not None
    assert result.error.error_type == "adapter_error"
    assert result.error.message == "adapter down"
    assert result.error.details["error_type"] == "UnitTestFailure"
    assert result.metadata.task_success is False
    assert result.errors == (result.error,)


@pytest.mark.asyncio
async def test_inner_lm_adapter_returns_invalid_json_errors() -> None:
    """Successful Hermes process output still has to satisfy the JSON contract."""
    runtime = _FakeHermesRuntime(
        Result.ok(TaskResult(success=True, final_message="{not-json", messages=()))
    )

    result = await RLMHermesInnerLMAdapter(runtime, system_prompt=SYSTEM_PROMPT).execute_recursive_step(
        RLMHermesRecursiveStepRequest.from_prompt(_prompt())
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.error_type == "invalid_json"
    assert "not valid JSON" in result.error.message
    assert result.raw_completion == "{not-json"
    assert result.metadata.task_success is True


@pytest.mark.asyncio
async def test_inner_lm_adapter_parses_decomposition_contract_output() -> None:
    """The decomposition mode uses the stricter AC decomposition contract model."""
    prompt = _prompt(mode=RLM_HERMES_DECOMPOSE_AC_MODE)
    completion = json.dumps(
        {
            "schema_version": RLM_HERMES_OUTPUT_SCHEMA_VERSION,
            "mode": RLM_HERMES_DECOMPOSE_AC_MODE,
            "rlm_node_id": "rlm_node_1",
            "ac_node_id": "ac_1",
            "verdict": "atomic",
            "confidence": 0.86,
            "result": {"summary": "The AC is already atomic."},
            "evidence_references": [{"chunk_id": "chunk-a", "claim": "Bounded evidence."}],
            "residual_gaps": [],
            "artifacts": [
                {
                    "artifact_type": "decomposition",
                    "is_atomic": True,
                    "atomic_rationale": "The AC has one bounded verification target.",
                    "proposed_child_acs": [],
                }
            ],
            "control": {
                "requires_retry": False,
                "suggested_next_mode": "execute_atomic",
                "must_not_recurse": False,
            },
        },
        sort_keys=True,
    )
    runtime = _FakeHermesRuntime(
        Result.ok(TaskResult(success=True, final_message=completion, messages=()))
    )

    result = await RLMHermesInnerLMAdapter(runtime, system_prompt=SYSTEM_PROMPT).execute_recursive_step(
        RLMHermesRecursiveStepRequest.from_prompt(prompt)
    )

    assert result.success is True
    assert isinstance(result.structured_output, RLMHermesACDecompositionResult)
    assert result.structured_output.verdict == "atomic"
    assert result.output is not None
    assert result.output["artifacts"][0]["is_atomic"] is True


@pytest.mark.asyncio
async def test_inner_lm_adapter_rejects_evidence_outside_supplied_context() -> None:
    """Hermes citations must point at context selected by the outer scaffold."""
    runtime = _FakeHermesRuntime(
        Result.ok(
            TaskResult(
                success=True,
                final_message=_atomic_output(chunk_id="chunk-outside-context"),
                messages=(),
            )
        )
    )

    result = await RLMHermesInnerLMAdapter(runtime, system_prompt=SYSTEM_PROMPT).execute_recursive_step(
        RLMHermesRecursiveStepRequest.from_prompt(_prompt(chunk_ids=("chunk-a",)))
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.error_type == "contract_error"
    assert "outside supplied context" in result.error.message
