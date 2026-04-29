"""Hermes inner-LM adapter for one RLM recursive step.

The RLM scaffold owns recursion and state mutation. This adapter only executes
one bounded Hermes runtime call and normalizes the result into structured
output, errors, and replay metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
from time import perf_counter
from typing import Any, Literal

from ouroboros.core.errors import ProviderError
from ouroboros.orchestrator.adapter import AgentRuntime, RuntimeHandle, TaskResult
from ouroboros.rlm.contracts import (
    RLM_HERMES_DECOMPOSE_AC_MODE,
    RLM_HERMES_EXECUTE_ATOMIC_MODE,
    RLM_HERMES_OUTPUT_SCHEMA_VERSION,
    RLM_HERMES_SYNTHESIZE_PARENT_MODE,
    RLMHermesACDecompositionResult,
    RLMHermesContractError,
    RLMHermesControl,
    RLMHermesEvidenceReference,
    RLMHermesResidualGap,
)
from ouroboros.rlm.trace import hash_trace_text

RLM_HERMES_INPUT_SCHEMA_VERSION = "rlm.hermes.input.v1"
RLM_HERMES_SUMMARIZE_CHUNK_MODE = "summarize_chunk"

type RLMHermesStepMode = Literal[
    "decompose_ac",
    "execute_atomic",
    "summarize_chunk",
    "synthesize_parent",
]

RLM_HERMES_STEP_MODES = frozenset(
    {
        RLM_HERMES_DECOMPOSE_AC_MODE,
        RLM_HERMES_EXECUTE_ATOMIC_MODE,
        RLM_HERMES_SUMMARIZE_CHUNK_MODE,
        RLM_HERMES_SYNTHESIZE_PARENT_MODE,
    }
)

RLM_HERMES_RECURSIVE_STEP_ERROR_TYPES = frozenset(
    {
        "adapter_error",
        "adapter_exception",
        "runtime_unsuccessful",
        "invalid_json",
        "contract_error",
    }
)


@dataclass(frozen=True, slots=True)
class RLMHermesRecursiveStepRequest:
    """One bounded recursive step request sent to Hermes."""

    prompt: str
    mode: str | None = None
    rlm_node_id: str | None = None
    ac_node_id: str | None = None
    call_id: str | None = None
    subcall_id: str | None = None
    parent_call_id: str | None = None
    trace_id: str | None = None
    parent_trace_id: str | None = None
    causal_parent_event_id: str | None = None
    depth: int = 0
    selected_chunk_ids: tuple[str, ...] = ()
    generated_child_ac_node_ids: tuple[str, ...] = ()
    resume_handle: RuntimeHandle | None = field(default=None, compare=False, repr=False)
    resume_session_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            msg = "RLM Hermes recursive step prompt must be a non-empty string"
            raise ValueError(msg)
        if self.mode is not None and self.mode not in RLM_HERMES_STEP_MODES:
            msg = f"RLM Hermes recursive step mode must be one of {sorted(RLM_HERMES_STEP_MODES)}"
            raise ValueError(msg)
        if isinstance(self.depth, bool) or not isinstance(self.depth, int):
            msg = "RLM Hermes recursive step depth must be an integer"
            raise TypeError(msg)
        if self.depth < 0:
            msg = "RLM Hermes recursive step depth must be non-negative"
            raise ValueError(msg)
        object.__setattr__(self, "selected_chunk_ids", _string_tuple(self.selected_chunk_ids))
        object.__setattr__(
            self,
            "generated_child_ac_node_ids",
            _string_tuple(self.generated_child_ac_node_ids),
        )

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        *,
        mode: str | None = None,
        rlm_node_id: str | None = None,
        ac_node_id: str | None = None,
        call_id: str | None = None,
        subcall_id: str | None = None,
        parent_call_id: str | None = None,
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        causal_parent_event_id: str | None = None,
        depth: int | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> RLMHermesRecursiveStepRequest:
        """Create a request, preferring explicit values over prompt envelope values."""
        metadata = _metadata_from_prompt(prompt)
        return cls(
            prompt=prompt,
            mode=mode or _string_or_none(metadata.get("mode")),
            rlm_node_id=rlm_node_id or _string_or_none(metadata.get("rlm_node_id")),
            ac_node_id=ac_node_id or _string_or_none(metadata.get("ac_node_id")),
            call_id=call_id or _string_or_none(metadata.get("call_id")),
            subcall_id=subcall_id or _string_or_none(metadata.get("subcall_id")),
            parent_call_id=parent_call_id or _string_or_none(metadata.get("parent_call_id")),
            trace_id=trace_id or _string_or_none(metadata.get("trace_id")),
            parent_trace_id=parent_trace_id or _string_or_none(metadata.get("parent_trace_id")),
            causal_parent_event_id=causal_parent_event_id
            or _string_or_none(metadata.get("causal_parent_event_id")),
            depth=depth if depth is not None else _int_or_zero(metadata.get("depth")),
            selected_chunk_ids=_string_tuple(metadata.get("selected_chunk_ids")),
            generated_child_ac_node_ids=_string_tuple(
                metadata.get("generated_child_ac_node_ids")
            ),
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        )


@dataclass(frozen=True, slots=True)
class RLMHermesRecursiveStepMetadata:
    """Replay metadata produced at the Hermes recursive-step boundary."""

    mode: str | None
    rlm_node_id: str | None
    ac_node_id: str | None
    call_id: str | None
    subcall_id: str | None
    parent_call_id: str | None
    trace_id: str | None
    parent_trace_id: str | None
    causal_parent_event_id: str | None
    depth: int
    selected_chunk_ids: tuple[str, ...] = ()
    generated_child_ac_node_ids: tuple[str, ...] = ()
    runtime_backend: str | None = None
    llm_backend: str | None = None
    working_directory: str | None = None
    permission_mode: str | None = None
    session_id: str | None = None
    resume_handle_present: bool = False
    task_success: bool | None = None
    message_count: int = 0
    prompt_hash: str | None = None
    response_hash: str | None = None
    system_prompt_hash: str | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata for traces, tests, or command output."""
        return {
            "mode": self.mode,
            "rlm_node_id": self.rlm_node_id,
            "ac_node_id": self.ac_node_id,
            "call_id": self.call_id,
            "subcall_id": self.subcall_id,
            "parent_call_id": self.parent_call_id,
            "trace_id": self.trace_id,
            "parent_trace_id": self.parent_trace_id,
            "causal_parent_event_id": self.causal_parent_event_id,
            "depth": self.depth,
            "selected_chunk_ids": list(self.selected_chunk_ids),
            "generated_child_ac_node_ids": list(self.generated_child_ac_node_ids),
            "runtime_backend": self.runtime_backend,
            "llm_backend": self.llm_backend,
            "working_directory": self.working_directory,
            "permission_mode": self.permission_mode,
            "session_id": self.session_id,
            "resume_handle_present": self.resume_handle_present,
            "task_success": self.task_success,
            "message_count": self.message_count,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "system_prompt_hash": self.system_prompt_hash,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class RLMHermesRecursiveStepError:
    """Normalized failure returned by the Hermes recursive-step adapter."""

    error_type: str
    message: str
    provider: str = "hermes"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.error_type not in RLM_HERMES_RECURSIVE_STEP_ERROR_TYPES:
            msg = (
                "RLM Hermes recursive step error_type must be one of "
                f"{sorted(RLM_HERMES_RECURSIVE_STEP_ERROR_TYPES)}"
            )
            raise ValueError(msg)
        object.__setattr__(self, "details", dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the normalized adapter error."""
        return {
            "error_type": self.error_type,
            "message": self.message,
            "provider": self.provider,
            "details": dict(self.details),
        }

    def to_provider_error(self) -> ProviderError:
        """Convert to the repository's provider-error type."""
        return ProviderError(self.message, provider=self.provider, details=self.to_dict())


@dataclass(frozen=True, slots=True)
class RLMHermesRecursiveStepResult:
    """Normalized output of one Hermes recursive step."""

    success: bool
    metadata: RLMHermesRecursiveStepMetadata
    output: dict[str, Any] | None = None
    structured_output: RLMHermesACDecompositionResult | dict[str, Any] | None = None
    raw_completion: str = ""
    error: RLMHermesRecursiveStepError | None = None
    task_result: TaskResult | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.success and self.error is not None:
            msg = "successful RLM Hermes recursive step results cannot carry an error"
            raise ValueError(msg)
        if not self.success and self.error is None:
            msg = "failed RLM Hermes recursive step results require an error"
            raise ValueError(msg)
        if self.output is not None:
            object.__setattr__(self, "output", dict(self.output))
        if isinstance(self.structured_output, Mapping):
            object.__setattr__(self, "structured_output", dict(self.structured_output))

    @property
    def is_ok(self) -> bool:
        """Return True when the Hermes step produced accepted structured output."""
        return self.success

    @property
    def is_err(self) -> bool:
        """Return True when adapter, runtime, or output-contract validation failed."""
        return not self.success

    @property
    def errors(self) -> tuple[RLMHermesRecursiveStepError, ...]:
        """Return a stable tuple for callers that aggregate step errors."""
        return () if self.error is None else (self.error,)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the recursive-step result without the raw TaskResult object."""
        return {
            "success": self.success,
            "metadata": self.metadata.to_dict(),
            "output": self.output,
            "raw_completion": self.raw_completion,
            "error": self.error.to_dict() if self.error is not None else None,
        }


class RLMHermesInnerLMAdapter:
    """Execute bounded RLM recursive steps through an existing Hermes runtime."""

    def __init__(
        self,
        hermes_runtime: AgentRuntime,
        *,
        system_prompt: str,
    ) -> None:
        self._hermes_runtime = hermes_runtime
        self._system_prompt = system_prompt

    async def execute_recursive_step(
        self,
        request: RLMHermesRecursiveStepRequest,
    ) -> RLMHermesRecursiveStepResult:
        """Execute one Hermes RPC-style recursive step and normalize its output."""
        started_at = perf_counter()
        metadata = _metadata_from_request(
            request,
            runtime=self._hermes_runtime,
            system_prompt=self._system_prompt,
        )

        try:
            runtime_result = await self._hermes_runtime.execute_task_to_result(
                prompt=request.prompt,
                tools=[],
                system_prompt=self._system_prompt,
                resume_handle=request.resume_handle,
                resume_session_id=request.resume_session_id,
            )
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            return _failed_step_result(
                metadata=replace(metadata, elapsed_ms=elapsed_ms),
                raw_completion="",
                error_type="adapter_exception",
                message=str(exc),
                details={"exception_type": type(exc).__name__},
            )

        elapsed_ms = int((perf_counter() - started_at) * 1000)

        if runtime_result.is_err:
            error = runtime_result.error
            provider = error.provider if isinstance(error, ProviderError) else "hermes"
            details: dict[str, Any] = {}
            if isinstance(error, ProviderError):
                details.update(dict(error.details))
            details["cause"] = str(error)
            return _failed_step_result(
                metadata=replace(metadata, task_success=False, elapsed_ms=elapsed_ms),
                raw_completion="",
                error_type="adapter_error",
                message=error.message if isinstance(error, ProviderError) else str(error),
                provider=provider or "hermes",
                details=details,
            )

        task_result = runtime_result.value
        raw_completion = task_result.final_message or ""
        completed_metadata = _metadata_with_task_result(
            metadata,
            request=request,
            task_result=task_result,
            raw_completion=raw_completion,
            elapsed_ms=elapsed_ms,
        )

        if not task_result.success:
            return _failed_step_result(
                metadata=completed_metadata,
                raw_completion=raw_completion,
                error_type="runtime_unsuccessful",
                message="Hermes recursive step did not complete successfully",
                details={"completion_preview": raw_completion[:500]},
                task_result=task_result,
            )

        try:
            output, structured_output = _parse_and_validate_output(
                raw_completion,
                request=request,
            )
        except json.JSONDecodeError as exc:
            return _failed_step_result(
                metadata=completed_metadata,
                raw_completion=raw_completion,
                error_type="invalid_json",
                message=f"Hermes recursive step output is not valid JSON: {exc.msg}",
                details={"line": exc.lineno, "column": exc.colno},
                task_result=task_result,
            )
        except RLMHermesContractError as exc:
            return _failed_step_result(
                metadata=completed_metadata,
                raw_completion=raw_completion,
                error_type="contract_error",
                message=str(exc),
                details={"completion_preview": raw_completion[:500]},
                task_result=task_result,
            )

        return RLMHermesRecursiveStepResult(
            success=True,
            output=output,
            structured_output=structured_output,
            raw_completion=raw_completion,
            metadata=completed_metadata,
            task_result=task_result,
        )


async def execute_hermes_recursive_step(
    *,
    hermes_runtime: AgentRuntime,
    request: RLMHermesRecursiveStepRequest,
    system_prompt: str,
) -> RLMHermesRecursiveStepResult:
    """Execute one RLM Hermes recursive step without instantiating the adapter explicitly."""
    adapter = RLMHermesInnerLMAdapter(hermes_runtime, system_prompt=system_prompt)
    return await adapter.execute_recursive_step(request)


def _failed_step_result(
    *,
    metadata: RLMHermesRecursiveStepMetadata,
    raw_completion: str,
    error_type: str,
    message: str,
    provider: str = "hermes",
    details: Mapping[str, Any] | None = None,
    task_result: TaskResult | None = None,
) -> RLMHermesRecursiveStepResult:
    return RLMHermesRecursiveStepResult(
        success=False,
        raw_completion=raw_completion,
        metadata=metadata,
        error=RLMHermesRecursiveStepError(
            error_type=error_type,
            message=message,
            provider=provider,
            details=dict(details or {}),
        ),
        task_result=task_result,
    )


def _metadata_from_request(
    request: RLMHermesRecursiveStepRequest,
    *,
    runtime: AgentRuntime,
    system_prompt: str,
) -> RLMHermesRecursiveStepMetadata:
    return RLMHermesRecursiveStepMetadata(
        mode=request.mode,
        rlm_node_id=request.rlm_node_id,
        ac_node_id=request.ac_node_id,
        call_id=request.call_id,
        subcall_id=request.subcall_id,
        parent_call_id=request.parent_call_id,
        trace_id=request.trace_id,
        parent_trace_id=request.parent_trace_id,
        causal_parent_event_id=request.causal_parent_event_id,
        depth=request.depth,
        selected_chunk_ids=request.selected_chunk_ids,
        generated_child_ac_node_ids=request.generated_child_ac_node_ids,
        runtime_backend=_string_or_none(getattr(runtime, "runtime_backend", None)),
        llm_backend=_string_or_none(getattr(runtime, "llm_backend", None)),
        working_directory=_string_or_none(getattr(runtime, "working_directory", None)),
        permission_mode=_string_or_none(getattr(runtime, "permission_mode", None)),
        resume_handle_present=request.resume_handle is not None,
        prompt_hash=hash_trace_text(request.prompt),
        system_prompt_hash=hash_trace_text(system_prompt),
    )


def _metadata_with_task_result(
    metadata: RLMHermesRecursiveStepMetadata,
    *,
    request: RLMHermesRecursiveStepRequest,
    task_result: TaskResult,
    raw_completion: str,
    elapsed_ms: int,
) -> RLMHermesRecursiveStepMetadata:
    return replace(
        metadata,
        subcall_id=metadata.subcall_id or _subcall_id_from_task_result(task_result),
        session_id=task_result.session_id,
        resume_handle_present=request.resume_handle is not None
        or task_result.resume_handle is not None,
        task_success=task_result.success,
        message_count=len(task_result.messages),
        response_hash=hash_trace_text(raw_completion),
        elapsed_ms=elapsed_ms,
    )


def _parse_and_validate_output(
    raw_completion: str,
    *,
    request: RLMHermesRecursiveStepRequest,
) -> tuple[dict[str, Any], RLMHermesACDecompositionResult | dict[str, Any]]:
    parsed = json.loads(raw_completion)
    if not isinstance(parsed, Mapping):
        msg = "Hermes recursive step output must be a JSON object"
        raise RLMHermesContractError(msg)

    expected_mode = request.mode
    if expected_mode == RLM_HERMES_DECOMPOSE_AC_MODE:
        structured = RLMHermesACDecompositionResult.from_dict(
            parsed,
            expected_rlm_node_id=request.rlm_node_id,
            expected_ac_node_id=request.ac_node_id,
        )
        _validate_allowed_evidence_references(
            structured.to_dict(),
            allowed_evidence_ids=_allowed_evidence_ids_from_prompt(request.prompt),
        )
        return structured.to_dict(), structured

    output = _validate_common_structured_output(
        parsed,
        expected_mode=expected_mode,
        expected_rlm_node_id=request.rlm_node_id,
        expected_ac_node_id=request.ac_node_id,
    )
    _validate_allowed_evidence_references(
        output,
        allowed_evidence_ids=_allowed_evidence_ids_from_prompt(request.prompt),
    )
    return output, output


def _validate_common_structured_output(
    data: Mapping[str, Any],
    *,
    expected_mode: str | None,
    expected_rlm_node_id: str | None,
    expected_ac_node_id: str | None,
) -> dict[str, Any]:
    schema_version = _required_str(data, "schema_version")
    if schema_version != RLM_HERMES_OUTPUT_SCHEMA_VERSION:
        msg = f"schema_version must be {RLM_HERMES_OUTPUT_SCHEMA_VERSION}"
        raise RLMHermesContractError(msg)

    mode = _required_str(data, "mode")
    if mode not in RLM_HERMES_STEP_MODES:
        msg = f"mode must be one of {sorted(RLM_HERMES_STEP_MODES)}"
        raise RLMHermesContractError(msg)
    if expected_mode is not None and mode != expected_mode:
        msg = f"mode mismatch: expected {expected_mode}, got {mode}"
        raise RLMHermesContractError(msg)

    rlm_node_id = _required_str(data, "rlm_node_id")
    if expected_rlm_node_id is not None and rlm_node_id != expected_rlm_node_id:
        msg = f"rlm_node_id mismatch: expected {expected_rlm_node_id}, got {rlm_node_id}"
        raise RLMHermesContractError(msg)

    ac_node_id = _required_str(data, "ac_node_id")
    if expected_ac_node_id is not None and ac_node_id != expected_ac_node_id:
        msg = f"ac_node_id mismatch: expected {expected_ac_node_id}, got {ac_node_id}"
        raise RLMHermesContractError(msg)

    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        msg = "confidence must be a number"
        raise RLMHermesContractError(msg)
    if not 0.0 <= float(confidence) <= 1.0:
        msg = "confidence must be between 0.0 and 1.0"
        raise RLMHermesContractError(msg)

    result = data.get("result")
    if not isinstance(result, Mapping):
        msg = "result must be an object"
        raise RLMHermesContractError(msg)

    evidence_items = _required_sequence(data.get("evidence_references", []), "evidence_references")
    residual_items = _required_sequence(data.get("residual_gaps", []), "residual_gaps")
    evidence_references = tuple(
        RLMHermesEvidenceReference.from_dict(
            _required_mapping(item, "evidence_references[]")
        )
        for item in evidence_items
    )
    residual_gaps = tuple(
        RLMHermesResidualGap.from_dict(_required_mapping(item, "residual_gaps[]"))
        for item in residual_items
    )

    control_raw = data.get("control")
    control = (
        RLMHermesControl.from_dict(_required_mapping(control_raw, "control"))
        if control_raw is not None
        else None
    )

    output = dict(data)
    output["confidence"] = float(confidence)
    output["result"] = dict(result)
    output["evidence_references"] = [reference.to_dict() for reference in evidence_references]
    output["residual_gaps"] = [gap.to_dict() for gap in residual_gaps]
    if control is not None:
        output["control"] = control.to_dict()
    return output


def _validate_allowed_evidence_references(
    output: Mapping[str, Any],
    *,
    allowed_evidence_ids: frozenset[str],
) -> None:
    if not allowed_evidence_ids:
        return

    evidence_references = output.get("evidence_references", [])
    if isinstance(evidence_references, str) or not isinstance(evidence_references, Sequence):
        msg = "evidence_references must be an array"
        raise RLMHermesContractError(msg)

    unknown_ids = [
        reference.get("chunk_id")
        for reference in evidence_references
        if isinstance(reference, Mapping)
        and isinstance(reference.get("chunk_id"), str)
        and reference.get("chunk_id") not in allowed_evidence_ids
    ]
    if unknown_ids:
        msg = f"evidence_references cite IDs outside supplied context: {sorted(set(unknown_ids))}"
        raise RLMHermesContractError(msg)


def _allowed_evidence_ids_from_prompt(prompt: str) -> frozenset[str]:
    envelope = _json_mapping_or_empty(prompt)
    context = _mapping_or_empty(envelope.get("context"))
    trace = _mapping_or_empty(envelope.get("trace"))

    allowed: set[str] = set(_string_tuple(trace.get("selected_chunk_ids")))
    for chunk in _sequence_or_empty(context.get("chunks")):
        if isinstance(chunk, Mapping):
            allowed.update(_string_tuple(chunk.get("chunk_id")))
    for summary in _sequence_or_empty(context.get("summaries")):
        if isinstance(summary, Mapping):
            allowed.update(_string_tuple(summary.get("summary_id")))
            allowed.update(_string_tuple(summary.get("source_chunk_ids")))
    for child_result in _sequence_or_empty(context.get("child_results")):
        if isinstance(child_result, Mapping):
            allowed.update(_string_tuple(child_result.get("child_result_id")))
            allowed.update(_string_tuple(child_result.get("chunk_id")))
            allowed.update(_string_tuple(child_result.get("call_id")))

    return frozenset(allowed)


def _metadata_from_prompt(prompt: str) -> dict[str, Any]:
    envelope = _json_mapping_or_empty(prompt)
    call_context = _mapping_or_empty(envelope.get("call_context"))
    rlm_node = _mapping_or_empty(envelope.get("rlm_node"))
    ac_node = _mapping_or_empty(envelope.get("ac_node"))
    trace = _mapping_or_empty(envelope.get("trace"))

    return {
        "mode": envelope.get("mode"),
        "rlm_node_id": rlm_node.get("id") or envelope.get("rlm_node_id"),
        "ac_node_id": ac_node.get("id") or envelope.get("ac_node_id"),
        "call_id": call_context.get("call_id") or trace.get("call_id"),
        "subcall_id": trace.get("subcall_id") or envelope.get("subcall_id"),
        "parent_call_id": call_context.get("parent_call_id") or trace.get("parent_call_id"),
        "trace_id": trace.get("trace_id"),
        "parent_trace_id": trace.get("parent_trace_id"),
        "causal_parent_event_id": trace.get("causal_parent_event_id"),
        "depth": call_context.get("depth") if "depth" in call_context else trace.get("depth"),
        "selected_chunk_ids": trace.get("selected_chunk_ids"),
        "generated_child_ac_node_ids": trace.get("generated_child_ac_node_ids"),
    }


def _subcall_id_from_task_result(task_result: TaskResult) -> str | None:
    for message in reversed(task_result.messages):
        data = message.data
        subcall_id = _string_or_none(data.get("subcall_id"))
        if subcall_id is not None:
            return subcall_id
    return None


def _json_mapping_or_empty(payload: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence_or_empty(value: object) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return value


def _required_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f"{field_name} must be an object"
        raise RLMHermesContractError(msg)
    return value


def _required_sequence(value: object, field_name: str) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        msg = f"{field_name} must be an array"
        raise RLMHermesContractError(msg)
    return value


def _required_str(data: Mapping[str, Any], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} must be a non-empty string"
        raise RLMHermesContractError(msg)
    return value


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Mapping) or not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) and value >= 0 else 0
