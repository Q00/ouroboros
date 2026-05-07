"""Pin the current ``ooo auto --runtime <backend>`` semantics.

Documented in ``docs/auto-runtime-semantics.md``: ``--runtime`` is the same
value for both authoring (in-process MCP handler) and run-handoff
(dispatcher), and plugin/subagent dispatch in the run handoff is gated on
opencode plugin mode. These tests pin the actual CLI-to-handler wiring —
not just dataclass field assignment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.cli.commands import auto as auto_cli
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    StartExecuteSeedHandler,
)
from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin


def _spy_handler_constructions(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Replace each handler constructor in the CLI module with a spy that
    records the ``agent_runtime_backend`` it received and returns a stub
    object. The pipeline's run is also short-circuited so no real backend is
    invoked.
    """
    captured: dict[str, list[Any]] = {
        "interview": [],
        "generate_seed": [],
        "execute_seed": [],
        "start_execute": [],
    }

    def _make_spy(name: str, sentinel: Any):
        def _spy(*args: Any, **kwargs: Any) -> Any:
            captured[name].append(kwargs.get("agent_runtime_backend"))
            return sentinel

        return _spy

    interview_stub = object()
    generate_stub = object()
    execute_stub = object()
    start_execute_stub = object()

    monkeypatch.setattr(auto_cli, "InterviewHandler", _make_spy("interview", interview_stub))
    monkeypatch.setattr(auto_cli, "GenerateSeedHandler", _make_spy("generate_seed", generate_stub))
    monkeypatch.setattr(auto_cli, "ExecuteSeedHandler", _make_spy("execute_seed", execute_stub))
    monkeypatch.setattr(
        auto_cli, "StartExecuteSeedHandler", _make_spy("start_execute", start_execute_stub)
    )

    # The CLI also wraps each handler in HandlerInterviewBackend / HandlerSeedGenerator /
    # HandlerRunStarter; replace those with no-ops so AutoPipeline construction does not
    # trip over the stubs.
    monkeypatch.setattr(auto_cli, "HandlerInterviewBackend", lambda *_a, **_kw: object())
    monkeypatch.setattr(auto_cli, "HandlerSeedGenerator", lambda *_a, **_kw: object())
    monkeypatch.setattr(auto_cli, "HandlerRunStarter", lambda *_a, **_kw: object())

    # Short-circuit the pipeline so no real backend runs. AutoPipelineResult only
    # needs ``status`` / ``auto_session_id`` / ``phase`` for the CLI rendering path,
    # but we never reach printing because _run_auto returns the result and the test
    # just asserts on captured constructor args.
    from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult

    async def _stub_run(self: AutoPipeline, state: AutoPipelineState) -> AutoPipelineResult:
        return AutoPipelineResult(
            status="blocked",
            auto_session_id=state.auto_session_id,
            phase=state.phase.value,
            blocker="stubbed for runtime-wiring contract test",
        )

    monkeypatch.setattr(AutoPipeline, "run", _stub_run)
    return captured


@pytest.mark.parametrize(
    "runtime",
    ["claude", "codex", "opencode", "hermes", "gemini", "copilot", "kiro"],
)
def test_run_auto_threads_runtime_through_all_four_handlers(
    runtime: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end CLI wiring contract (#690): ``_run_auto`` must construct all
    four MCP handlers — InterviewHandler, GenerateSeedHandler,
    ExecuteSeedHandler, StartExecuteSeedHandler — with the same
    ``agent_runtime_backend`` derived from ``--runtime``.

    Bot review on #722 flagged that asserting on manually-instantiated
    handlers does not protect this contract: a future change could stop
    threading ``runtime`` into one of the construction sites and the older
    test would still pass. This test patches the constructors with spies,
    runs ``_run_auto``, and asserts on the captured kwargs.
    """
    captured = _spy_handler_constructions(monkeypatch)
    monkeypatch.setattr(AutoStore, "__init__", lambda self, root=None: None)
    monkeypatch.setattr(AutoStore, "root", tmp_path, raising=False)
    monkeypatch.setattr(AutoStore, "save", lambda self, state: tmp_path / "noop.json")
    # _run_auto resolves opencode_mode from get_opencode_mode() for the opencode
    # runtime; pin a stable value so the test is deterministic.
    monkeypatch.setattr(auto_cli, "get_opencode_mode", lambda: "subprocess")

    asyncio.run(
        auto_cli._run_auto(
            goal="end-to-end runtime wiring test",
            resume=None,
            runtime=runtime,
            max_interview_rounds=1,
            max_repair_rounds=1,
            skip_run=True,
        )
    )

    # The interview handler in opencode mode is constructed with
    # ``opencode_mode="subprocess"`` (CLI rewrites "plugin" → "subprocess" for
    # authoring), but ``agent_runtime_backend`` is the input runtime in every
    # case. Authoring handlers and run-handoff handlers receive the same value.
    assert captured["interview"] == [runtime]
    assert captured["generate_seed"] == [runtime]
    assert captured["execute_seed"] == [runtime]
    assert captured["start_execute"] == [runtime]


@pytest.mark.parametrize(
    "runtime",
    ["claude", "codex", "hermes", "gemini", "copilot", "kiro"],
)
def test_runtime_propagates_to_authoring_and_run_handoff(runtime: str) -> None:
    """Lower-level invariant: each handler stores the runtime it was built
    with. Complements the end-to-end wiring test above so an unrelated
    refactor that only breaks one side of the contract still fails loudly.
    """
    interview = InterviewHandler(agent_runtime_backend=runtime)
    generate = GenerateSeedHandler(agent_runtime_backend=runtime)
    execute = ExecuteSeedHandler(agent_runtime_backend=runtime)
    start_execute = StartExecuteSeedHandler(execute_handler=execute, agent_runtime_backend=runtime)

    assert interview.agent_runtime_backend == runtime
    assert generate.agent_runtime_backend == runtime
    assert execute.agent_runtime_backend == runtime
    assert start_execute.agent_runtime_backend == runtime
    assert (
        interview.agent_runtime_backend
        == generate.agent_runtime_backend
        == execute.agent_runtime_backend
        == start_execute.agent_runtime_backend
    )


@pytest.mark.parametrize(
    "runtime",
    ["claude", "codex", "hermes", "gemini", "copilot", "kiro"],
)
def test_runtime_persisted_on_state_round_trip(runtime: str, tmp_path) -> None:
    """``state.runtime_backend`` survives JSON round-trip — handlers that
    read this field on resume see the original value."""
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = runtime
    store.save(state)

    loaded = store.load(state.auto_session_id)
    assert loaded.runtime_backend == runtime


@pytest.mark.parametrize(
    "runtime,opencode_mode,expected",
    [
        ("claude", None, False),
        ("codex", None, False),
        ("codex", "plugin", False),  # plugin mode irrelevant for non-opencode
        ("opencode", None, False),
        ("opencode", "subprocess", False),
        ("opencode", "plugin", True),
    ],
)
def test_should_dispatch_via_plugin_matrix(
    runtime: str, opencode_mode: str | None, expected: bool
) -> None:
    """Plugin/subagent dispatch is opt-in via opencode plugin mode only."""
    assert should_dispatch_via_plugin(runtime, opencode_mode) is expected


def test_codex_runtime_does_not_imply_plugin_dispatch() -> None:
    """Regression: ``--runtime codex`` MUST NOT trigger plugin dispatch.
    The first interview question is generated in-process by the authoring
    handler that talks to Codex; it does not become a Codex subagent task."""
    assert should_dispatch_via_plugin("codex", None) is False
    assert should_dispatch_via_plugin("codex", "plugin") is False
    assert should_dispatch_via_plugin("codex", "subprocess") is False
