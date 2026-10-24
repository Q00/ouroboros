"""The public entry points share runner construction and checkpointing."""

from typing import Any

from ouroboros.orchestrator import runner_factory


def test_builder_initializes_checkpoint_and_common_limits(monkeypatch: Any) -> None:
    initialized: list[object] = []
    captured: dict[str, object] = {}

    class FakeCheckpointStore:
        def initialize(self) -> None:
            initialized.append(self)

    def fake_runner(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runner_factory, "CheckpointStore", FakeCheckpointStore)
    monkeypatch.setattr(runner_factory, "OrchestratorRunner", fake_runner)
    monkeypatch.setattr(runner_factory, "get_max_parallel_workers", lambda: 7)

    adapter = object()
    event_store = object()
    runner = runner_factory.build_orchestrator_runner(
        adapter=adapter,  # type: ignore[arg-type]
        event_store=event_store,  # type: ignore[arg-type]
        console=object(),  # type: ignore[arg-type]
        mcp_manager=None,
        mcp_tool_prefix="",
        debug=False,
        task_workspace=None,
        enable_decomposition=True,
        max_decomposition_depth=None,
        max_parallel_workers=None,
        fat_harness_mode=False,
        base_model_tier=None,
        efficiency_mode=None,
        frugality_assurance=None,
        inherited_runtime_handle=None,
        inherited_tools=None,
        session_signal_hub=None,
    )

    assert runner is not None
    assert captured["adapter"] is adapter
    assert captured["event_store"] is event_store
    assert captured["checkpoint_store"] is initialized[0]
    assert captured["max_parallel_workers"] == 7
    assert captured["max_decomposition_depth"] == runner_factory.DEFAULT_MAX_DECOMPOSITION_DEPTH


def test_builder_preserves_legacy_resume_without_a_checkpoint_store(monkeypatch: Any) -> None:
    def forbidden_store() -> object:
        raise AssertionError("a legacy resumed run must not create a checkpoint store")

    captured: dict[str, object] = {}

    def fake_runner(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runner_factory, "CheckpointStore", forbidden_store)
    monkeypatch.setattr(runner_factory, "OrchestratorRunner", fake_runner)

    runner_factory.build_orchestrator_runner(
        adapter=object(),  # type: ignore[arg-type]
        event_store=object(),  # type: ignore[arg-type]
        console=object(),  # type: ignore[arg-type]
        mcp_manager=None,
        mcp_tool_prefix="",
        debug=False,
        task_workspace=None,
        enable_decomposition=True,
        max_decomposition_depth=2,
        max_parallel_workers=3,
        fat_harness_mode=False,
        base_model_tier=None,
        efficiency_mode=None,
        frugality_assurance=None,
        inherited_runtime_handle=None,
        inherited_tools=None,
        session_signal_hub=None,
        checkpoint_store_enabled=False,
    )

    assert captured["checkpoint_store"] is None
    assert captured["max_decomposition_depth"] == 2
    assert captured["max_parallel_workers"] == 3
