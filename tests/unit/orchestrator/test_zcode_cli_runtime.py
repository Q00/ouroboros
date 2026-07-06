"""Tests for :class:`ZcodeCLIRuntime`.

Pinned to the **measured** behaviour of
``node zcode.cjs --prompt ... --json`` (captured against a live run with a
working model config), not to an assumed event schema:

- zcode emits a SINGLE pretty-printed JSON summary object (not an NDJSON stream):
  ``{sessionId, traceId, turnId, response, usage, eventCount, projection}``.
- Intermediate tool calls are reflected only in ``eventCount``/``usage``; they
  do not appear as discrete stdout events.
- Real CLI flags are ``--json``, ``--prompt``, ``--cwd``, ``--mode``, ``--resume``
  (there is **no** ``--non-interactive`` and **no** ``--approval-mode``).
- The CLI is invoked as ``node <cli_path>``.

Captured fixtures live under ``tests/fixtures/zcode/``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.orchestrator.adapter import ParamSupport
from ouroboros.orchestrator.zcode_cli_runtime import ZcodeCLIRuntime

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "zcode"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def runtime() -> ZcodeCLIRuntime:
    return ZcodeCLIRuntime(
        cli_path="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        permission_mode="acceptEdits",
    )


# -- capabilities ----------------------------------------------------------


def test_capabilities_declared_correctly(runtime: ZcodeCLIRuntime) -> None:
    caps = runtime.capabilities
    assert caps.structured_output is True
    assert caps.targeted_resume is True
    assert caps.reasoning_effort_support == ParamSupport.IGNORED


# -- _convert_event: measured single-JSON summary --------------------------


def test_convert_event_simple_summary_to_assistant(runtime: ZcodeCLIRuntime) -> None:
    event = _load("summary_simple.json")
    msgs = runtime._convert_event(event, current_handle=None)
    assert len(msgs) == 1
    assert msgs[0].type == "assistant"
    assert msgs[0].content == "OK"
    assert msgs[0].data.get("terminal") is True
    assert msgs[0].data.get("usage", {}).get("totalTokens") == 8901
    assert event["sessionId"].startswith("sess_")


def test_convert_event_tool_summary_to_assistant(runtime: ZcodeCLIRuntime) -> None:
    """Tool-invoking prompts still surface as one assistant message; the tool
    call is reflected only in eventCount/usage, not as a separate event."""
    event = _load("summary_with_tool.json")
    msgs = runtime._convert_event(event, current_handle=None)
    assert len(msgs) == 1
    assert msgs[0].type == "assistant"
    assert msgs[0].content.startswith("Created")
    assert msgs[0].data.get("eventCount") == 51
    assert msgs[0].data.get("usage", {}).get("modelRequestCount") == 2


def test_convert_event_empty_response_returns_nothing(runtime: ZcodeCLIRuntime) -> None:
    assert runtime._convert_event({"sessionId": "sess_x"}, None) == []
    assert runtime._convert_event({"response": ""}, None) == []
    assert runtime._convert_event({"response": None}, None) == []


def test_convert_event_truncates_oversized_response(runtime: ZcodeCLIRuntime) -> None:
    long = "x" * 5_000_000
    msgs = runtime._convert_event({"response": long}, None)
    assert len(msgs) == 1
    assert len(msgs[0].content) <= 5_000_000  # InputValidator clamps it


# -- _extract_event_session_id: top-level sessionId ------------------------


def test_extract_session_id_from_top_level(runtime: ZcodeCLIRuntime) -> None:
    event = _load("summary_simple.json")
    assert runtime._extract_event_session_id(event) == event["sessionId"]


def test_extract_session_id_missing_returns_none(runtime: ZcodeCLIRuntime) -> None:
    # No top-level sessionId; inherited resolver returns None for a bare dict.
    assert runtime._extract_event_session_id({"foo": "bar"}) is None


# -- _build_command: real zcode flags --------------------------------------


def test_build_command_uses_real_flags(runtime: ZcodeCLIRuntime) -> None:
    cmd = runtime._build_command("/tmp/unused", prompt="hi")
    # Invoked via node on the zcode.cjs script.
    assert cmd[0] == "node"
    assert cmd[1].endswith("zcode.cjs")
    assert "--json" in cmd
    assert "--prompt" in cmd
    assert "hi" in cmd
    # acceptEdits -> --mode edit (NOT --approval-mode, NOT auto_edit).
    assert "--mode" in cmd
    assert "edit" in cmd
    assert "--approval-mode" not in cmd
    assert "--non-interactive" not in cmd
    assert "auto_edit" not in cmd


def test_build_command_bypass_permissions_maps_to_yolo() -> None:
    rt = ZcodeCLIRuntime(
        cli_path="/tmp/zcode.cjs",
        permission_mode="bypassPermissions",
    )
    cmd = rt._build_command("/tmp/unused", prompt="hi")
    assert "yolo" in cmd
    assert "--mode" in cmd


def test_build_command_includes_cwd_and_resume() -> None:
    rt = ZcodeCLIRuntime(
        cli_path="/tmp/zcode.cjs",
        permission_mode="acceptEdits",
        cwd="/tmp",
    )
    cmd = rt._build_command(
        "/tmp/unused",
        prompt="hi",
        resume_session_id="sess_resume_me",
    )
    assert "--cwd" in cmd
    assert "/tmp" in cmd
    assert "--resume" in cmd
    assert "sess_resume_me" in cmd


# -- _iter_stream_lines: whole stdout as one line --------------------------


class _FakeStream:
    """Mimics asyncio.StreamReader at the granularity the parent helper uses.

    The parent ``iter_runtime_stream_lines`` reads in fixed-size chunks via
    ``stream.read(chunk_size)`` until EOF (empty bytes), so the fake yields the
    payload on the first read and signals EOF afterwards.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._consumed = False

    async def read(self, n: int = -1) -> bytes:  # noqa: ARG002 - matches StreamReader.read
        if self._consumed:
            return b""
        self._consumed = True
        return self._payload


class _HangingStream:
    """Mimics a subprocess that never produces output (auth prompt / stall)."""

    async def read(self, n: int = -1) -> bytes:  # noqa: ARG002
        await asyncio.sleep(3600)
        return b""


@pytest.mark.asyncio
async def test_iter_stream_lines_yields_whole_buffer(runtime: ZcodeCLIRuntime) -> None:
    """A pretty-printed multi-line JSON document is yielded as ONE line so the
    inherited pipeline json-parses the complete object."""
    payload = b'{\n  "sessionId": "sess_x",\n  "response": "OK"\n}\n'
    out = [line async for line in runtime._iter_stream_lines(_FakeStream(payload))]
    assert len(out) == 1
    assert json.loads(out[0])["response"] == "OK"


@pytest.mark.asyncio
async def test_iter_stream_lines_empty_stdout_yields_nothing(runtime: ZcodeCLIRuntime) -> None:
    out = [line async for line in runtime._iter_stream_lines(_FakeStream(b"   "))]
    assert out == []


@pytest.mark.asyncio
async def test_iter_stream_lines_raises_on_silent_subprocess(
    runtime: ZcodeCLIRuntime,
) -> None:
    """A silent/hung subprocess must raise TimeoutError, not wedge forever.

    This guards the BLOCKING finding: the override must forward
    ``first_chunk_timeout_seconds`` so the parent watchdog fires when zcode
    stays alive but emits nothing. The orchestrator's ``execute_task`` relies
    on this ``TimeoutError`` to terminate the subprocess.
    """
    with pytest.raises(TimeoutError, match="produced no stdout"):
        _ = [
            line
            async for line in runtime._iter_stream_lines(
                _HangingStream(),
                first_chunk_timeout_seconds=0.05,
            )
        ]


# -- config schema: persisted runtime_backend / cli_path --------------------


def test_config_accepts_zcode_runtime_backend() -> None:
    from ouroboros.config.models import OrchestratorConfig

    cfg = OrchestratorConfig(runtime_backend="zcode")
    assert cfg.runtime_backend == "zcode"


def test_config_accepts_zcode_cli_path_and_expands() -> None:
    from ouroboros.config.models import OrchestratorConfig

    cfg = OrchestratorConfig(zcode_cli_path="~/zcode.cjs")
    assert cfg.zcode_cli_path is not None
    assert "~" not in cfg.zcode_cli_path  # expand_cli_path validator ran
    assert cfg.zcode_cli_path.endswith("zcode.cjs")


def test_validate_runtime_backend_accepts_zcode() -> None:
    from ouroboros.config.models import _validate_runtime_backend

    assert _validate_runtime_backend("zcode", field_name="test") == "zcode"
