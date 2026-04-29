"""Reusable RLM test doubles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult
from ouroboros.rlm.trace import hash_trace_text

_RLM_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "rlm"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _task_result(value: TaskResult | str) -> TaskResult:
    if isinstance(value, TaskResult):
        return value
    return TaskResult(success=True, final_message=value, messages=())


@dataclass(frozen=True, slots=True)
class RLMHermesMockExchange:
    """One deterministic Hermes RPC exchange captured by the fixture."""

    prompt: str
    completion: str
    mode: str
    call_id: str
    subcall_id: str | None
    parent_call_id: str | None
    depth: int
    trace_id: str | None
    parent_trace_id: str | None
    selected_chunk_ids: tuple[str, ...]
    generated_child_ac_node_ids: tuple[str, ...]
    success: bool

    def completion_payload(self) -> dict[str, Any]:
        """Return the JSON completion emitted by the mock runtime."""
        payload = json.loads(self.completion)
        return payload if isinstance(payload, dict) else {}


class DeterministicRLMHermesRuntime:
    """Hermes RPC test double that emits call-context-derived completions."""

    runtime_backend = "hermes"
    llm_backend = "hermes"
    working_directory = None
    permission_mode = None

    def __init__(
        self,
        *,
        responses: Sequence[TaskResult | str] | None = None,
        responses_by_call_id: Mapping[str, TaskResult | str] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exchanges: list[RLMHermesMockExchange] = []
        self._responses = [_task_result(response) for response in responses or ()]
        self._responses_by_call_id = {
            call_id: _task_result(response)
            for call_id, response in (responses_by_call_id or {}).items()
        }

    @property
    def prompts_by_call_id(self) -> dict[str, str]:
        """Return captured prompts keyed by RLM call ID."""
        return {exchange.call_id: exchange.prompt for exchange in self.exchanges}

    @property
    def completions_by_call_id(self) -> dict[str, str]:
        """Return emitted completions keyed by RLM call ID."""
        return {exchange.call_id: exchange.completion for exchange in self.exchanges}

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        """Capture a Hermes RPC-style call and return deterministic JSON output."""
        envelope = self._prompt_envelope(prompt)
        call_context = _mapping(envelope.get("call_context"))
        trace = _mapping(envelope.get("trace"))

        fallback_index = len(self.calls) + 1
        call_id = self._string_or_default(
            call_context.get("call_id"), f"mock_call_{fallback_index:03d}"
        )
        subcall_id = self._optional_string(trace.get("subcall_id"))
        parent_call_id = self._optional_string(call_context.get("parent_call_id"))
        depth = self._int_or_default(call_context.get("depth"), 0)
        mode = self._string_or_default(envelope.get("mode"), "execute_atomic")
        selected_chunk_ids = _string_tuple(trace.get("selected_chunk_ids"))
        generated_child_ac_node_ids = _string_tuple(trace.get("generated_child_ac_node_ids"))

        self.calls.append(
            {
                "prompt": prompt,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_handle": resume_handle,
                "resume_session_id": resume_session_id,
            }
        )

        task_result = self._next_result(
            prompt=prompt,
            envelope=envelope,
            call_id=call_id,
            parent_call_id=parent_call_id,
            depth=depth,
            mode=mode,
            selected_chunk_ids=selected_chunk_ids,
            generated_child_ac_node_ids=generated_child_ac_node_ids,
            subcall_id=subcall_id,
        )
        self.exchanges.append(
            RLMHermesMockExchange(
                prompt=prompt,
                completion=task_result.final_message,
                mode=mode,
                call_id=call_id,
                subcall_id=subcall_id,
                parent_call_id=parent_call_id,
                depth=depth,
                trace_id=self._optional_string(trace.get("trace_id")),
                parent_trace_id=self._optional_string(trace.get("parent_trace_id")),
                selected_chunk_ids=selected_chunk_ids,
                generated_child_ac_node_ids=generated_child_ac_node_ids,
                success=task_result.success,
            )
        )
        return Result.ok(task_result)

    def _next_result(
        self,
        *,
        prompt: str,
        envelope: Mapping[str, Any],
        call_id: str,
        parent_call_id: str | None,
        depth: int,
        mode: str,
        selected_chunk_ids: tuple[str, ...],
        generated_child_ac_node_ids: tuple[str, ...],
        subcall_id: str | None,
    ) -> TaskResult:
        if self._responses:
            return self._responses.pop(0)
        if call_id in self._responses_by_call_id:
            return self._responses_by_call_id[call_id]
        return TaskResult(
            success=True,
            final_message=self._completion_json(
                prompt=prompt,
                envelope=envelope,
                call_id=call_id,
                parent_call_id=parent_call_id,
                depth=depth,
                mode=mode,
                selected_chunk_ids=selected_chunk_ids,
                generated_child_ac_node_ids=generated_child_ac_node_ids,
                subcall_id=subcall_id,
            ),
            messages=(),
        )

    def _completion_json(
        self,
        *,
        prompt: str,
        envelope: Mapping[str, Any],
        call_id: str,
        parent_call_id: str | None,
        depth: int,
        mode: str,
        selected_chunk_ids: tuple[str, ...],
        generated_child_ac_node_ids: tuple[str, ...],
        subcall_id: str | None,
    ) -> str:
        context = _mapping(envelope.get("context"))
        child_results = context.get("child_results")
        child_call_ids = []
        if isinstance(child_results, Sequence) and not isinstance(child_results, str):
            for child_result in child_results:
                child_result_mapping = _mapping(child_result)
                child_call_id = child_result_mapping.get("call_id")
                if isinstance(child_call_id, str):
                    child_call_ids.append(child_call_id)

        payload = {
            "schema_version": "rlm.hermes.output.v1",
            "mode": mode,
            "verdict": "passed",
            "confidence": 0.9,
            "result": {
                "summary": f"deterministic {mode} completion for {call_id}",
                "call_id": call_id,
                "subcall_id": subcall_id,
                "parent_call_id": parent_call_id,
                "depth": depth,
                "prompt_hash": hash_trace_text(prompt),
                "selected_chunk_ids": list(selected_chunk_ids),
                "generated_child_ac_node_ids": list(generated_child_ac_node_ids),
                "child_call_ids": child_call_ids,
            },
            "evidence_references": [
                {
                    "chunk_id": chunk_id,
                    "claim": f"{call_id} consumed {chunk_id}",
                }
                for chunk_id in selected_chunk_ids
            ],
            "residual_gaps": [],
        }
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _prompt_envelope(prompt: str) -> Mapping[str, Any]:
        try:
            parsed = json.loads(prompt)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _string_or_default(value: object, default: str) -> str:
        return value if isinstance(value, str) and value else default

    @staticmethod
    def _int_or_default(value: object, default: int) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else default


@pytest.fixture
def deterministic_rlm_hermes_runtime() -> DeterministicRLMHermesRuntime:
    """Return a deterministic Hermes runtime for nested RLM loop tests."""
    return DeterministicRLMHermesRuntime()


@pytest.fixture
def minimal_recursive_run_fixture() -> dict[str, Any]:
    """Return the minimal recursive RLM replay fixture payload."""
    return json.loads(
        (_RLM_FIXTURES_DIR / "minimal_recursive_run.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def long_context_truncation_fixture() -> dict[str, Any]:
    """Return the deterministic long-context truncation fixture payload."""
    return json.loads(
        (_RLM_FIXTURES_DIR / "long_context_truncation.json").read_text(encoding="utf-8")
    )
