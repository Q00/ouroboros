"""Single-request LiteLLM worker for frugality-proof effect isolation."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import sys
from typing import Any

import litellm

from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.litellm_adapter import (
    _HTTPX_DEPENDENCY_VERSION,
    _LITELLM_DEPENDENCY_VERSION,
    _OPENAI_DEPENDENCY_VERSION,
    _PROOF_LITELLM_MAX_RETRIES,
    _PROOF_TRANSPORT_CONTRACT,
    _PROOF_WORKER_PROTOCOL,
    LiteLLMAdapter,
    _worker_runtime_authority,
)

_MAX_INPUT_BYTES = 4 * 1024 * 1024
_COMPLETION_KWARGS = frozenset(
    {
        "api_base",
        "api_key",
        "max_retries",
        "max_tokens",
        "messages",
        "model",
        "reasoning_effort",
        "response_format",
        "stop",
        "temperature",
        "timeout",
        "top_p",
    }
)
_REQUIRED_COMPLETION_KWARGS = frozenset(
    {
        "api_base",
        "api_key",
        "max_retries",
        "max_tokens",
        "messages",
        "model",
        "temperature",
        "timeout",
    }
)
_PAYLOAD_KEYS = frozenset({"client_type", "completion_kwargs", "config", "protocol"})


def _proof_report(
    *,
    authority_verified: bool,
    client_cleanup_succeeded: bool,
    client_type: object,
) -> dict[str, object]:
    worker_authority = _worker_runtime_authority(sys.executable)
    return {
        "authority_verified": authority_verified,
        "client_cleanup_succeeded": client_cleanup_succeeded,
        "client_type": client_type,
        "httpx_dependency_version": _HTTPX_DEPENDENCY_VERSION,
        "litellm_dependency_version": _LITELLM_DEPENDENCY_VERSION,
        "openai_dependency_version": _OPENAI_DEPENDENCY_VERSION,
        "worker_executable_resolved_path": (worker_authority.resolved_executable_path),
        "worker_executable_content_sha256": (worker_authority.executable_content_sha256),
        "worker_python_version": worker_authority.python_version,
        "worker_module": worker_authority.worker_module,
        "worker_implementation_sha256": (worker_authority.worker_implementation_sha256),
        "adapter_module": worker_authority.adapter_module,
        "adapter_implementation_sha256": (worker_authority.adapter_implementation_sha256),
        "ouroboros_implementation_contract": (worker_authority.ouroboros_implementation_contract),
        "ouroboros_implementation_sha256": (worker_authority.ouroboros_implementation_sha256),
        "transport_contract": _PROOF_TRANSPORT_CONTRACT,
    }


def _failure_report(
    *,
    authority_verified: bool = False,
    client_cleanup_succeeded: bool = False,
    client_type: object = None,
) -> dict[str, object]:
    return {
        "ok": False,
        "proof": _proof_report(
            authority_verified=authority_verified,
            client_cleanup_succeeded=client_cleanup_succeeded,
            client_type=client_type,
        ),
        "response": None,
    }


def _validated_payload(raw: object) -> tuple[dict[str, Any], CompletionConfig, str] | None:
    if not isinstance(raw, dict) or set(raw) != _PAYLOAD_KEYS:
        return None
    if raw.get("protocol") != _PROOF_WORKER_PROTOCOL:
        return None
    kwargs = raw.get("completion_kwargs")
    config_raw = raw.get("config")
    client_type = raw.get("client_type")
    if (
        not isinstance(kwargs, dict)
        or not _REQUIRED_COMPLETION_KWARGS.issubset(kwargs)
        or not set(kwargs).issubset(_COMPLETION_KWARGS)
        or not isinstance(config_raw, dict)
        or not isinstance(client_type, str)
        or kwargs.get("max_retries") != _PROOF_LITELLM_MAX_RETRIES
        or not isinstance(kwargs.get("api_key"), str)
        or not kwargs["api_key"]
        or not isinstance(kwargs.get("api_base"), str)
        or not kwargs["api_base"]
    ):
        return None
    try:
        config = CompletionConfig(**config_raw)
    except (TypeError, ValueError):
        return None
    if kwargs.get("model") != config.model:
        return None
    return kwargs, config, client_type


async def _run(raw: object) -> dict[str, object]:
    validated = _validated_payload(raw)
    if validated is None:
        return _failure_report()
    kwargs, config, expected_client_type = validated
    adapter = LiteLLMAdapter(
        api_key=kwargs["api_key"],
        api_base=kwargs["api_base"],
        timeout=kwargs["timeout"],
        max_retries=1,
        frugality_proof=True,
    )
    provider = adapter._extract_provider(config.model)
    initial_authority = adapter._dependency_authority(
        config.model,
        api_key=kwargs["api_key"],
    )
    sealed_client = adapter._create_sealed_client(
        provider=provider,
        api_key=kwargs["api_key"],
        api_base=kwargs["api_base"],
        timeout=kwargs["timeout"],
    )
    if sealed_client is None or sealed_client.client_type != expected_client_type:
        return _failure_report(client_type=expected_client_type)

    response: dict[str, object] | None = None
    call_succeeded = False
    cleanup_succeeded = False
    try:
        with redirect_stdout(sys.stderr):
            raw_response = await litellm.acompletion(
                **kwargs,
                client=sealed_client.value,
            )
        parsed = adapter._parse_response(raw_response, config)
        response = {
            "content": parsed.content,
            "finish_reason": parsed.finish_reason,
            "model": parsed.model,
            "usage": {
                "completion_tokens": parsed.usage.completion_tokens,
                "prompt_tokens": parsed.usage.prompt_tokens,
                "total_tokens": parsed.usage.total_tokens,
            },
        }
        call_succeeded = True
    except BaseException:  # noqa: BLE001 — never expose provider or credential details
        call_succeeded = False
    finally:
        try:
            await sealed_client.close()
            cleanup_succeeded = True
        except BaseException:  # noqa: BLE001 — cleanup status is proof evidence
            cleanup_succeeded = False

    final_authority = adapter._dependency_authority(
        config.model,
        api_key=kwargs["api_key"],
    )
    authority_verified = bool(
        initial_authority.defaults_verified and initial_authority == final_authority
    )
    return {
        "ok": call_succeeded,
        "proof": _proof_report(
            authority_verified=authority_verified,
            client_cleanup_succeeded=cleanup_succeeded,
            client_type=sealed_client.client_type,
        ),
        "response": response,
    }


def main() -> int:
    raw_bytes = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw_bytes) > _MAX_INPUT_BYTES:
        report = _failure_report()
    else:
        try:
            payload = json.loads(raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            report = _failure_report()
        else:
            report = asyncio.run(_run(payload))
    sys.stdout.write(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
