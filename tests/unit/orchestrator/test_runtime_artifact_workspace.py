"""Runtime-local MCP handlers keep disposable artifacts in the selected workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime


@pytest.mark.parametrize(
    ("runtime_type", "cli_name"),
    [
        (CodexCliRuntime, "codex"),
        (OpenCodeRuntime, "opencode"),
        (HermesCliRuntime, "hermes"),
    ],
    ids=("codex", "opencode", "hermes"),
)
def test_builtin_handlers_use_runtime_cwd_when_launcher_cwd_differs(
    runtime_type: type[Any],
    cli_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "launcher"
    project = tmp_path / "runtime-project"
    launcher.mkdir()
    project.mkdir()
    monkeypatch.chdir(launcher)

    runtime = runtime_type(cli_path=cli_name, cwd=project)
    handlers = runtime._get_builtin_mcp_handlers()
    submit = handlers["ouroboros_submit_fanout_results"]
    fetch = handlers["ouroboros_fetch_artifact"]

    assert submit.disposable_memory is not None
    assert submit.disposable_memory.artifact_store.root == (
        project.resolve() / ".ouroboros" / "artifacts"
    )
    assert submit.disposable_memory.artifact_store.root != (
        launcher.resolve() / ".ouroboros" / "artifacts"
    )
    assert fetch.disposable_memory.artifact_store.root == (
        project.resolve() / ".ouroboros" / "artifacts"
    )
