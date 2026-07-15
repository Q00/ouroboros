"""Tests for :class:`ZcodeCLIRuntime`.

Pinned to the **measured** behaviour of an official ZCode app-bundle
``zcode.cjs --prompt ... --json`` run through its bundled Electron/Node runtime
(captured against a live run with a working model config), not to an assumed
event schema:

- zcode emits a SINGLE pretty-printed JSON summary object (not an NDJSON stream):
  ``{sessionId, traceId, turnId, response, usage, eventCount, projection}``.
- Intermediate tool calls are reflected only in ``eventCount``/``usage``; they
  do not appear as discrete stdout events.
- Real CLI flags are ``--json``, ``--prompt``, ``--cwd``, ``--mode``, ``--resume``
  (there is **no** ``--non-interactive`` and **no** ``--approval-mode``).
- Official app bundles use their Electron/Node runtime; standalone scripts use
  ``node <cli_path>`` and PATH executables run directly.

Captured fixtures live under ``tests/fixtures/zcode/``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import plistlib
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
        cli_path="/tmp/zcode.cjs",
        permission_mode="acceptEdits",
    )


def _fake_electron_node_bundle(tmp_path: Path) -> tuple[Path, Path]:
    contents = tmp_path / "ZCode.app" / "Contents"
    cli_path = contents / "Resources" / "glm" / "zcode.cjs"
    electron_node = contents / "MacOS" / "ZCode"
    cli_path.parent.mkdir(parents=True)
    electron_node.parent.mkdir(parents=True)
    cli_path.write_text("// zcode", encoding="utf-8")
    cli_path.with_name(".node-bundle-meta.json").write_text(
        json.dumps(
            {
                "runtime": "electron-node",
                "entry": "zcode.cjs",
                "platform": "darwin-arm64",
            }
        ),
        encoding="utf-8",
    )
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": "ZCode"}, stream)
    electron_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    electron_node.chmod(0o755)
    return cli_path, electron_node


# -- capabilities ----------------------------------------------------------


def test_capabilities_declared_correctly(runtime: ZcodeCLIRuntime) -> None:
    caps = runtime.capabilities
    assert caps.structured_output is True
    assert caps.targeted_resume is True
    assert caps.reasoning_effort_support == ParamSupport.IGNORED


def test_direct_runtime_uses_completion_capable_llm_backend() -> None:
    """Zcode is runtime-only, so direct construction needs a valid fallback
    for built-in interview/seed/evaluate/qa handlers."""
    from ouroboros.providers import resolve_llm_backend

    runtime = ZcodeCLIRuntime(cli_path="/tmp/zcode.cjs")

    assert runtime.llm_backend == "claude_code"
    assert resolve_llm_backend(runtime.llm_backend) == "claude_code"


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


@pytest.mark.parametrize(
    "event",
    [
        {"sessionId": "sess_x"},
        {"response": ""},
        {"response": "   "},
        {"response": None},
    ],
)
def test_convert_event_empty_response_is_protocol_error(
    runtime: ZcodeCLIRuntime,
    event: dict[str, Any],
) -> None:
    msgs = runtime._convert_event(event, None)
    assert len(msgs) == 1
    assert msgs[0].type == "result"
    assert msgs[0].is_error
    assert "non-empty response" in msgs[0].content
    assert msgs[0].data["protocol_error"] == "missing_response"


@pytest.mark.asyncio
async def test_execute_task_empty_response_fails_closed_without_generic_success(
    tmp_path: Path,
) -> None:
    fake_zcode = tmp_path / "zcode"
    fake_zcode.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' \'{"sessionId":"sess_empty","response":""}\'\n',
        encoding="utf-8",
    )
    fake_zcode.chmod(0o755)
    runtime = ZcodeCLIRuntime(cli_path=fake_zcode)

    messages = [message async for message in runtime.execute_task("hi")]

    final_messages = [message for message in messages if message.is_final]
    assert len(final_messages) == 1
    assert final_messages[0].is_error
    assert final_messages[0].data["protocol_error"] == "missing_response"
    assert "Zcode CLI task completed" not in final_messages[0].content


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


def test_build_command_app_bundle_uses_bundled_electron_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path, electron_node = _fake_electron_node_bundle(tmp_path)
    monkeypatch.setenv("NODE_OPTIONS", "--require ./evil.js")
    runtime = ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")

    cmd = runtime._build_command("/tmp/unused", prompt="hi")

    assert cmd[:2] == [str(electron_node), str(cli_path)]
    assert cmd[2:7] == ["--json", "--prompt", "hi", "--mode", "edit"]
    assert "--cwd" in cmd
    child_env = runtime._build_child_env()
    assert child_env["ELECTRON_RUN_AS_NODE"] == "1"
    assert "NODE_OPTIONS" not in child_env


def test_app_bundle_detection_does_not_depend_on_resource_depth(tmp_path: Path) -> None:
    cli_path, electron_node = _fake_electron_node_bundle(tmp_path)
    nested_dir = cli_path.parent / "nested" / "deeper"
    nested_dir.mkdir(parents=True)
    nested_cli = nested_dir / cli_path.name
    nested_metadata = nested_dir / ".node-bundle-meta.json"
    cli_path.replace(nested_cli)
    cli_path.with_name(".node-bundle-meta.json").replace(nested_metadata)

    runtime = ZcodeCLIRuntime(cli_path=nested_cli, permission_mode="acceptEdits")

    assert runtime._build_command("/tmp/unused", prompt="hi")[:2] == [
        str(electron_node),
        str(nested_cli),
    ]


def test_build_command_plain_cjs_uses_system_node_without_electron_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "zcode.cjs"
    cli_path.write_text("// standalone zcode", encoding="utf-8")
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setenv("NODE_OPTIONS", "--require ./evil.js")
    runtime = ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")

    cmd = runtime._build_command("/tmp/unused", prompt="hi")

    assert cmd[:2] == ["node", str(cli_path)]
    child_env = runtime._build_child_env()
    assert "ELECTRON_RUN_AS_NODE" not in child_env
    assert "NODE_OPTIONS" not in child_env


@pytest.mark.parametrize(
    ("metadata_content", "error_match"),
    [
        (None, "missing or unreadable"),
        ("{", "invalid JSON"),
        (json.dumps([]), "must be a JSON object"),
        (json.dumps({"runtime": "node", "entry": "zcode.cjs"}), "unsupported runtime"),
        (
            json.dumps({"runtime": "electron-node", "entry": "other.cjs"}),
            "entry does not match",
        ),
        (json.dumps({"runtime": "electron-node"}), "entry does not match"),
    ],
)
def test_app_bundle_invalid_node_metadata_fails_closed(
    tmp_path: Path,
    metadata_content: str | None,
    error_match: str,
) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    metadata_path = cli_path.with_name(".node-bundle-meta.json")
    if metadata_content is None:
        metadata_path.unlink()
    else:
        metadata_path.write_text(metadata_content, encoding="utf-8")

    with pytest.raises(RuntimeError, match=error_match):
        ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")


def test_app_bundle_unreadable_node_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    metadata_path = cli_path.with_name(".node-bundle-meta.json")
    original_read_text = Path.read_text

    def _raise_for_metadata(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == metadata_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_metadata)

    with pytest.raises(RuntimeError, match="missing or unreadable"):
        ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")


def test_app_bundle_non_dictionary_plist_fails_closed(tmp_path: Path) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    info_plist = cli_path.parents[2] / "Info.plist"
    with info_plist.open("wb") as stream:
        plistlib.dump(["not", "a", "dictionary"], stream)

    with pytest.raises(RuntimeError, match="must be a dictionary"):
        ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")


@pytest.mark.parametrize("executable_name", ["../ZCode", "nested/ZCode", r"..\ZCode", ".."])
def test_app_bundle_rejects_executable_path_traversal(
    tmp_path: Path,
    executable_name: str,
) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    info_plist = cli_path.parents[2] / "Info.plist"
    with info_plist.open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable_name}, stream)

    with pytest.raises(RuntimeError, match="without path separators"):
        ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")


def test_app_bundle_missing_electron_runtime_fails_with_actionable_error(
    tmp_path: Path,
) -> None:
    cli_path, electron_node = _fake_electron_node_bundle(tmp_path)
    electron_node.unlink()

    with pytest.raises(RuntimeError, match="electron-node CLI"):
        ZcodeCLIRuntime(cli_path=cli_path, permission_mode="acceptEdits")


def test_build_command_bypass_permissions_maps_to_yolo() -> None:
    rt = ZcodeCLIRuntime(
        cli_path="/tmp/zcode.cjs",
        permission_mode="bypassPermissions",
    )
    cmd = rt._build_command("/tmp/unused", prompt="hi")
    assert "yolo" in cmd
    assert "--mode" in cmd


# -- _build_command: NO --model flag, ever (zcode has no model CLI flag) -----
# zcode has NO ``--model`` flag — verified on 0.14.5, 0.15.0, and 0.15.2, where
# ``--model`` is a hard ``Unknown option`` rejection that aborts the run before
# zcode does any work (reproduced: ``node zcode.cjs --model glm-4.6 --version``
# → "Unknown option '--model'"). The constructor warns when a non-default model
# is requested so the request-vs-config gap is visible, but the value is never
# forwarded. These tests pin that contract: regardless of what ``_model`` holds,
# the built command must NOT contain ``--model``. This inverts the earlier
# (wrong) test ``test_build_command_forwards_intentional_non_default_model``,
# which asserted ``--model glm-4.6`` WAS forwarded — that assertion locked in a
# regression and shipped a hard-failing flag.


def test_build_command_omits_model_flag_when_unset(runtime: ZcodeCLIRuntime) -> None:
    runtime._model = None
    cmd = runtime._build_command("/tmp/unused", prompt="hi")
    assert "--model" not in cmd


def test_build_command_strips_default_sentinel_model(runtime: ZcodeCLIRuntime) -> None:
    runtime._model = "default"
    cmd = runtime._build_command("/tmp/unused", prompt="hi")
    assert "--model" not in cmd


def test_build_command_never_emits_model_flag_even_when_explicit_non_default(
    runtime: ZcodeCLIRuntime,
) -> None:
    """A non-default model must NEVER produce ``--model``.

    zcode has no ``--model`` flag; emitting it aborts the run. This is the
    inverted regression for the blocking defect: the earlier test asserted the
    opposite (that ``glm-4.6`` WAS forwarded) and thereby guaranteed a
    hard-failing flag for any non-default model config.
    """
    runtime._model = "glm-4.6"
    cmd = runtime._build_command("/tmp/unused", prompt="hi")
    assert "--model" not in cmd
    # And the model id must not leak in as a bare positional either.
    assert "glm-4.6" not in cmd


def test_constructor_model_is_not_promoted_to_effective_identity() -> None:
    runtime = ZcodeCLIRuntime(cli_path="/tmp/zcode.cjs", model="glm-4.6")

    identity = runtime.execution_identity_contract()

    assert runtime._model is None
    assert identity["requested_model"] == "glm-4.6"
    assert identity["effective_model_observed"] is False


def test_build_command_ignores_per_call_model_override(runtime: ZcodeCLIRuntime) -> None:
    """The shared runtime API may supply a per-call model even though Zcode
    cannot enforce one; accepting and dropping it must not fail preparation."""
    cmd = runtime._build_command(
        "/tmp/unused",
        prompt="hi",
        model="glm-5.2",
    )

    assert "--model" not in cmd
    assert "glm-5.2" not in cmd


@pytest.mark.asyncio
async def test_execute_task_accepts_ignored_per_call_model(tmp_path: Path) -> None:
    """Exercise the public API, not only the command helper: a routed model
    must not fail preparation for a runtime that truthfully declares it ignored."""
    fake_zcode = tmp_path / "zcode"
    fake_zcode.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' \'{"sessionId":"sess_fake","response":"OK"}\'\n',
        encoding="utf-8",
    )
    fake_zcode.chmod(0o755)
    runtime = ZcodeCLIRuntime(cli_path=fake_zcode)

    messages = [message async for message in runtime.execute_task("hi", model="glm-5.2")]

    assert any(message.type == "assistant" and message.content == "OK" for message in messages)
    assert not any("unexpected keyword argument 'model'" in message.content for message in messages)


@pytest.mark.asyncio
async def test_execute_task_reuses_captured_session_id_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public execute path must carry a returned session into --resume.

    Helper tests cover session-id extraction and command construction separately;
    this integration test locks the handoff between them so a future base-runtime
    change cannot silently drop Zcode's reconnect handle.
    """
    args_log = tmp_path / "zcode-args.log"
    fake_zcode = tmp_path / "zcode"
    fake_zcode.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "$ZCODE_ARGS_LOG"\n'
        'printf \'%s\\n\' "--invocation-end--" >> "$ZCODE_ARGS_LOG"\n'
        'printf \'%s\\n\' \'{"sessionId":"sess_fake","response":"OK"}\'\n',
        encoding="utf-8",
    )
    fake_zcode.chmod(0o755)
    monkeypatch.setenv("ZCODE_ARGS_LOG", str(args_log))
    runtime = ZcodeCLIRuntime(cli_path=fake_zcode)

    first_messages = [message async for message in runtime.execute_task("first")]
    resume_handle = next(
        message.resume_handle for message in first_messages if message.resume_handle is not None
    )
    assert resume_handle.backend == "zcode_cli"
    assert resume_handle.native_session_id == "sess_fake"

    _ = [
        message
        async for message in runtime.execute_task(
            "second",
            resume_handle=resume_handle,
        )
    ]

    recorded = args_log.read_text(encoding="utf-8").splitlines()
    separators = [index for index, value in enumerate(recorded) if value == "--invocation-end--"]
    assert len(separators) == 2
    first_args = recorded[: separators[0]]
    second_args = recorded[separators[0] + 1 : separators[1]]
    assert "--resume" not in first_args
    resume_index = second_args.index("--resume")
    assert second_args[resume_index : resume_index + 2] == ["--resume", "sess_fake"]


# -- startup timeout: disabled by default for zcode's buffered output ---------
# zcode ``--prompt --json`` stays silent until its whole summary lands, so the
# inherited 60s "first chunk" watchdog would cap the ENTIRE task at 60s on any
# caller that doesn't pass an explicit override (e.g. a direct
# ``create_agent_runtime(backend="zcode")``). The class-level default is
# therefore ``None`` (disabled); an explicit positive override still wins.


def test_startup_output_timeout_disabled_by_default() -> None:
    rt = ZcodeCLIRuntime(cli_path="/tmp/zcode.cjs", permission_mode="acceptEdits")
    assert rt._startup_output_timeout_seconds is None


def test_startup_output_timeout_explicit_override_still_wins() -> None:
    """A caller that explicitly wants a cap can still set one."""
    rt = ZcodeCLIRuntime(
        cli_path="/tmp/zcode.cjs",
        permission_mode="acceptEdits",
        startup_output_timeout_seconds=30,
    )
    assert rt._startup_output_timeout_seconds == 30


def test_startup_output_timeout_execute_seed_zero_contract() -> None:
    """The execute-seed path forwards ``0`` to disable the guard — the class
    default must not interfere with that (``0`` → ``None`` via the parent)."""
    rt = ZcodeCLIRuntime(
        cli_path="/tmp/zcode.cjs",
        permission_mode="acceptEdits",
        startup_output_timeout_seconds=0,
    )
    assert rt._startup_output_timeout_seconds is None


def test_build_command_path_executable_invoked_directly_without_node() -> None:
    """A PATH-resolved `zcode` executable (not a .cjs script) is invoked
    directly. ``node <executable>`` would try to parse the binary as JS and
    fail before zcode ever runs, so the builder must drop the ``node`` prefix
    for non-script paths.
    """
    rt = ZcodeCLIRuntime(
        cli_path="/usr/local/bin/zcode",  # executable wrapper, no .cjs
        permission_mode="acceptEdits",
    )
    cmd = rt._build_command("/tmp/unused", prompt="hi")
    assert cmd[0] == "/usr/local/bin/zcode"
    assert "node" not in cmd
    assert "--json" in cmd
    assert "--prompt" in cmd
    assert "ELECTRON_RUN_AS_NODE" not in rt._build_child_env()


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


def test_cli_runtime_enums_accept_zcode() -> None:
    from ouroboros.cli.commands.init import AgentRuntimeBackend as InitBackend
    from ouroboros.cli.commands.mcp import AgentRuntimeBackend as McpBackend
    from ouroboros.cli.commands.run import AgentRuntimeBackend as RunBackend

    assert InitBackend("zcode") is InitBackend.ZCODE
    assert McpBackend("zcode") is McpBackend.ZCODE
    assert RunBackend("zcode") is RunBackend.ZCODE


def test_auto_cli_runtime_enum_accepts_zcode() -> None:
    from ouroboros.cli.commands.auto import AgentRuntimeBackend as AutoBackend

    assert AutoBackend("zcode") is AutoBackend.ZCODE
