"""Unit tests for the Zcode CLI-backed LLM adapter.

Verifies the zcode-specific overrides: ``--prompt --json --mode`` command
shape (no Codex artifacts), permission mapping, ``--model`` suppression, and
single-JSON-summary response parsing (``response`` / ``usage`` / ``sessionId``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.zcode_cli_adapter import ZcodeCliLLMAdapter

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "zcode"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """Mimics asyncio.subprocess.Process for the communicate() path.

    zcode's adapter reads the whole buffered JSON summary via
    ``process.communicate()`` (unlike Codex's streaming), so the fake returns
    (stdout, stderr) bytes directly.
    """

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # for _terminate_process fallback
        pass

    def terminate(self) -> None:
        pass


def _make_adapter(**kwargs: Any) -> ZcodeCliLLMAdapter:
    """Build an adapter with the zcode CLI path pinned to a .cjs script."""
    return ZcodeCliLLMAdapter(
        cli_path="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        cwd="/tmp",
        **kwargs,
    )


def _patch_subprocess(process: _FakeProcess):
    """Patch asyncio.create_subprocess_exec in the adapter module."""

    async def _fake_create(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    return patch(
        "ouroboros.providers.zcode_cli_adapter.asyncio.create_subprocess_exec",
        side_effect=_fake_create,
    )


def _patch_subprocess_sequence(processes: list[_FakeProcess]):
    """Patch subprocess creation with one fake process per attempt."""

    attempts = list(processes)

    async def _fake_create(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return attempts.pop(0)

    return patch(
        "ouroboros.providers.zcode_cli_adapter.asyncio.create_subprocess_exec",
        side_effect=_fake_create,
    )


# -- command construction ------------------------------------------------


def test_build_command_uses_zcode_flags_not_codex() -> None:
    adapter = _make_adapter()
    cmd = adapter._build_command(
        output_last_message_path="/tmp/out.txt",
        output_schema_path="/tmp/schema.json",
        model="GLM-5.2",
        profile="worker",
        prompt="hello",
    )
    # .cjs script → `node <script>` prefix
    assert cmd[0] == "node"
    assert cmd[1] == "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
    # Real zcode flags present
    assert "--json" in cmd
    assert "--prompt" in cmd
    assert "hello" in cmd
    assert "--mode" in cmd
    assert "edit" in cmd  # acceptEdits → edit
    assert "--cwd" in cmd
    assert "/tmp" in cmd
    # Codex artifacts MUST be absent
    assert "exec" not in cmd
    assert "--output-last-message" not in cmd
    assert "--output-schema" not in cmd
    assert "--ephemeral" not in cmd
    assert "--skip-git-repo-check" not in cmd
    assert "--model" not in cmd
    assert "--profile" not in cmd


def test_build_command_path_executable_no_node_prefix() -> None:
    adapter = ZcodeCliLLMAdapter(cli_path="/usr/local/bin/zcode", cwd="/tmp")
    cmd = adapter._build_command(
        output_last_message_path="",
        output_schema_path=None,
        model=None,
        profile=None,
        prompt="hi",
    )
    # PATH executable invoked directly, not via `node`
    assert cmd[0] == "/usr/local/bin/zcode"
    assert "node" not in cmd


def test_build_command_permission_mode_to_flag() -> None:
    adapter = _make_adapter(permission_mode="bypassPermissions")
    cmd = adapter._build_command(
        output_last_message_path="",
        output_schema_path=None,
        model=None,
        profile=None,
        prompt="hi",
    )
    assert "yolo" in cmd  # bypassPermissions → yolo


def test_build_permission_args_empty() -> None:
    adapter = _make_adapter()
    assert adapter._build_permission_args() == []


def test_feeds_prompt_via_stdin_false() -> None:
    assert _make_adapter()._feeds_prompt_via_stdin() is False
    assert _make_adapter()._requires_process_stdin() is False


# -- permission mode -----------------------------------------------------


def test_permission_mode_acceptEdits_passes_through() -> None:
    assert _make_adapter(permission_mode="acceptEdits")._permission_mode == "acceptEdits"


def test_permission_mode_bypass_passes_through() -> None:
    assert (
        _make_adapter(permission_mode="bypassPermissions")._permission_mode == "bypassPermissions"
    )


def test_permission_mode_default_coerced_to_acceptEdits() -> None:
    adapter = _make_adapter(permission_mode="default")
    assert adapter._permission_mode == "acceptEdits"


def test_permission_mode_none_defaults_to_acceptEdits() -> None:
    assert _make_adapter(permission_mode=None)._permission_mode == "acceptEdits"


def test_permission_mode_invalid_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported Zcode permission mode"):
        _make_adapter(permission_mode="dangerous")


# -- model suppression ---------------------------------------------------


def test_normalize_model_returns_none_and_warns() -> None:
    adapter = _make_adapter()
    # Non-default model is ignored (zcode has no --model flag)
    assert adapter._normalize_model("GLM-5.2") is None
    assert adapter._normalize_model("default") is None
    assert adapter._normalize_model("") is None


def test_build_child_env_strips_legacy_runtime_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "zcode")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "zcode")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "zcode")
    monkeypatch.setenv("CLAUDECODE", "1")

    env = _make_adapter()._build_child_env()

    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env
    assert "OUROBOROS_RUNTIME" not in env
    assert env["CLAUDECODE"] == "1"


@pytest.mark.asyncio
async def test_complete_rejects_missing_explicit_profile_before_subprocess() -> None:
    adapter = _make_adapter()
    config = OuroborosConfig(llm_profiles={})

    with (
        patch("ouroboros.providers.profiles.load_config", return_value=config) as mock_load_config,
        patch(
            "ouroboros.providers.zcode_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=AssertionError("subprocess should not start"),
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="hello")],
            CompletionConfig(model="default", profile="definitely_missing_profile"),
        )

    assert result.is_err
    mock_load_config.assert_called_once()
    assert "Invalid LLM profile configuration" in result.error.message
    assert result.error.details["config_key"] == "llm_profiles.definitely_missing_profile"


@pytest.mark.asyncio
async def test_structured_complete_rejects_missing_explicit_profile_before_subprocess() -> None:
    adapter = _make_adapter()
    config = OuroborosConfig(llm_profiles={})

    with (
        patch("ouroboros.providers.profiles.load_config", return_value=config) as mock_load_config,
        patch(
            "ouroboros.providers.zcode_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=AssertionError("subprocess should not start"),
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="hello")],
            CompletionConfig(
                model="default",
                profile="definitely_missing_profile",
                response_format={"type": "json_object"},
            ),
        )

    assert result.is_err
    mock_load_config.assert_called_once()
    assert "Invalid LLM profile configuration" in result.error.message
    assert result.error.details["config_key"] == "llm_profiles.definitely_missing_profile"


@pytest.mark.asyncio
async def test_complete_resolves_role_profile_before_zcode_request() -> None:
    event = _load("summary_simple.json")
    process = _FakeProcess(stdout=json.dumps(event).encode("utf-8"), returncode=0)
    adapter = _make_adapter()
    config = OuroborosConfig(
        llm_profiles={"fast": {"model": "profile-model", "max_turns": 1}},
        llm_role_profiles={"qa": "fast"},
    )

    with (
        patch("ouroboros.providers.profiles.load_config", return_value=config),
        _patch_subprocess(process),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="say OK")],
            CompletionConfig(model="default", role="qa"),
        )

    assert result.is_ok
    assert result.value.model == "profile-model"


# -- complete() response parsing -----------------------------------------


@pytest.mark.asyncio
async def test_complete_parses_summary_simple() -> None:
    event = _load("summary_simple.json")
    process = _FakeProcess(
        stdout=json.dumps(event).encode("utf-8"),
        returncode=0,
    )
    adapter = _make_adapter()
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="say OK")],
            CompletionConfig(model="default", temperature=0.0, max_tokens=10),
        )
    assert result.is_ok
    resp = result.value
    assert resp.content == "OK"
    # Usage extracted from zcode's usage object (not zeroed like Codex)
    assert resp.usage.prompt_tokens == 8899
    assert resp.usage.completion_tokens == 2
    assert resp.usage.total_tokens == 8901
    # session_id captured from top-level sessionId
    assert resp.raw_response["session_id"] == event["sessionId"]


@pytest.mark.asyncio
async def test_complete_non_json_stdout_returns_error() -> None:
    process = _FakeProcess(stdout=b"not json at all", returncode=0)
    adapter = _make_adapter()
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="x")],
            CompletionConfig(model="default"),
        )
    assert result.is_err
    assert "non-JSON" in result.error.message


@pytest.mark.asyncio
async def test_complete_non_object_json_returns_error() -> None:
    process = _FakeProcess(stdout=b"[1, 2, 3]", returncode=0)
    adapter = _make_adapter()
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="x")],
            CompletionConfig(model="default"),
        )
    assert result.is_err
    assert "non-object" in result.error.message


@pytest.mark.asyncio
async def test_complete_empty_response_returns_error() -> None:
    process = _FakeProcess(
        stdout=b'{"sessionId": "sess_x", "response": ""}',
        returncode=0,
    )
    adapter = _make_adapter()
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="x")],
            CompletionConfig(model="default"),
        )
    assert result.is_err
    assert "Empty response" in result.error.message


@pytest.mark.asyncio
async def test_complete_nonzero_returncode_returns_error() -> None:
    process = _FakeProcess(
        stdout=b'{"response": "ignored"}',
        stderr=b"Unknown option: --bogus",
        returncode=2,
    )
    adapter = _make_adapter()
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="x")],
            CompletionConfig(model="default"),
        )
    assert result.is_err
    assert "Unknown option" in result.error.message
    assert result.error.details["returncode"] == 2


@pytest.mark.asyncio
async def test_complete_missing_usage_defaults_to_zero() -> None:
    """A summary without a ``usage`` object yields zeroed UsageInfo, not a crash."""
    process = _FakeProcess(
        stdout=b'{"sessionId": "sess_x", "response": "hi"}',
        returncode=0,
    )
    adapter = _make_adapter()
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="x")],
            CompletionConfig(model="default"),
        )
    assert result.is_ok
    assert result.value.usage.prompt_tokens == 0
    assert result.value.usage.completion_tokens == 0


@pytest.mark.asyncio
async def test_complete_json_object_response_format_extracts_json() -> None:
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "sessionId": "sess_json",
                "response": '```json\n{"ok": true}\n```',
            }
        ).encode("utf-8"),
        returncode=0,
    )
    adapter = _make_adapter()
    with _patch_subprocess(process) as create_process:
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="return status")],
            CompletionConfig(
                model="default",
                response_format={"type": "json_object"},
            ),
        )
    assert result.is_ok
    assert json.loads(result.value.content) == {"ok": True}

    cmd = create_process.call_args.args
    prompt = cmd[cmd.index("--prompt") + 1]
    assert "Respond with ONLY a valid JSON object" in prompt
    assert create_process.call_count == 1


@pytest.mark.asyncio
async def test_complete_json_schema_response_format_validates_payload() -> None:
    schema = {
        "type": "object",
        "properties": {"score": {"type": "number"}},
        "required": ["score"],
        "additionalProperties": False,
    }
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "sessionId": "sess_schema",
                "response": '{"score": 0.95}',
            }
        ).encode("utf-8"),
        returncode=0,
    )
    adapter = _make_adapter()
    with _patch_subprocess(process) as create_process:
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="score it")],
            CompletionConfig(
                model="default",
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "Score", "schema": schema},
                },
            ),
        )
    assert result.is_ok
    assert json.loads(result.value.content) == {"score": 0.95}

    cmd = create_process.call_args.args
    prompt = cmd[cmd.index("--prompt") + 1]
    assert "JSON schema:" in prompt
    assert '"score"' in prompt
    assert create_process.call_count == 1


@pytest.mark.asyncio
async def test_complete_response_format_retries_then_fails_non_json() -> None:
    processes = [
        _FakeProcess(
            stdout=json.dumps({"sessionId": "sess_1", "response": "plain prose"}).encode("utf-8"),
            returncode=0,
        ),
        _FakeProcess(
            stdout=json.dumps({"sessionId": "sess_2", "response": "still prose"}).encode("utf-8"),
            returncode=0,
        ),
    ]
    adapter = _make_adapter(max_retries=2)
    with _patch_subprocess_sequence(processes) as create_process:
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="return JSON")],
            CompletionConfig(
                model="default",
                response_format={"type": "json_object"},
            ),
        )
    assert result.is_err
    assert "non-conforming" in result.error.message
    assert result.error.details["last_response_preview"] == "still prose"
    assert create_process.call_count == 2


@pytest.mark.asyncio
async def test_complete_json_schema_response_format_rejects_schema_mismatch() -> None:
    schema = {
        "type": "object",
        "properties": {"score": {"type": "number"}},
        "required": ["score"],
        "additionalProperties": False,
    }
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "sessionId": "sess_bad_schema",
                "response": '{"score": "high"}',
            }
        ).encode("utf-8"),
        returncode=0,
    )
    adapter = _make_adapter(max_retries=1)
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="score it")],
            CompletionConfig(
                model="default",
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "Score", "schema": schema},
                },
            ),
        )
    assert result.is_err
    assert "non-conforming" in result.error.message


@pytest.mark.asyncio
async def test_complete_json_object_response_format_rejects_array_payload() -> None:
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "sessionId": "sess_array",
                "response": "[1, 2]",
            }
        ).encode("utf-8"),
        returncode=0,
    )
    adapter = _make_adapter(max_retries=1)
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="return JSON")],
            CompletionConfig(
                model="default",
                response_format={"type": "json_object"},
            ),
        )
    assert result.is_err
    assert "non-conforming" in result.error.message


@pytest.mark.asyncio
async def test_complete_json_schema_response_format_rejects_invalid_schema() -> None:
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "sessionId": "sess_invalid_schema",
                "response": '{"score": 1}',
            }
        ).encode("utf-8"),
        returncode=0,
    )
    adapter = _make_adapter(max_retries=1)
    with _patch_subprocess(process):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="score it")],
            CompletionConfig(
                model="default",
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "Score",
                        "schema": {
                            "type": "not-a-json-schema-type",
                            "properties": {"score": {"type": "number"}},
                        },
                    },
                },
            ),
        )
    assert result.is_err
    assert "non-conforming" in result.error.message


@pytest.mark.asyncio
async def test_complete_unsupported_response_format_returns_error() -> None:
    adapter = _make_adapter()
    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="x")],
        CompletionConfig(
            model="default",
            response_format={"type": "text"},
        ),
    )
    assert result.is_err
    assert "Unsupported Zcode structured response_format request" in result.error.message


# -- factory routing -----------------------------------------------------


def test_factory_creates_zcode_adapter() -> None:
    from ouroboros.providers.factory import create_llm_adapter

    adapter = create_llm_adapter(backend="zcode")
    assert isinstance(adapter, ZcodeCliLLMAdapter)


def test_factory_creates_zcode_adapter_interview_use_case() -> None:
    from ouroboros.providers.factory import create_llm_adapter

    adapter = create_llm_adapter(backend="zcode", use_case="interview")
    assert isinstance(adapter, ZcodeCliLLMAdapter)
