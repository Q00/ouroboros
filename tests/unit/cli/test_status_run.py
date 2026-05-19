"""Tests for ``ouroboros status run`` — Wave-1 #946 S2 thin CLI surface.

These tests pin the contract that the CLI is a thin wrapper over
``ouroboros_query_projection``:

* The same arguments produce byte-identical JSON between the MCP handler
  and the CLI ``--json`` output (golden test).
* Exit codes follow the documented convention: ``0`` for success, ``2``
  for an unknown run anchor, ``64`` for malformed input.
* The positional ``RUN_ID`` argument is shorthand for ``--execution-id``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

runner = CliRunner(env={"COLUMNS": "240"})


def _golden_meta(run_id: str = "run_golden", execution_id: str = "exec_golden") -> dict:
    """Build a representative projection meta payload for golden comparisons."""

    return {
        "session_id": None,
        "execution_id": execution_id,
        "seed_id": "seed_golden",
        "seed_id_source": "event",
        "event_count": 3,
        "limit": None,
        "run": {
            "run_id": run_id,
            "seed_id": "seed_golden",
            "goal": "Golden goal",
            "schema_version": 1,
        },
        "stages": [
            {
                "stage_id": "stage_golden",
                "run_id": run_id,
                "kind": "execute",
            }
        ],
        "steps": [
            {
                "step_id": "step_golden",
                "run_id": run_id,
                "stage_id": "stage_golden",
                "kind": "tool_call",
                "name": "shell",
                "ok": True,
            }
        ],
        "artifacts": [],
        "verdicts": [],
    }


class _RecordingHandler:
    """Stand-in for ``ProjectionQueryHandler`` that records call args."""

    def __init__(self, result: Result):
        self.result = result
        self.last_arguments: dict | None = None


def _patched_runner(handler: _RecordingHandler):
    async def fake_handle(_self, arguments):  # noqa: D401 - test double
        handler.last_arguments = dict(arguments)
        return handler.result

    return patch(
        "ouroboros.cli.commands.status.ProjectionQueryHandler.handle",
        fake_handle,
    )


def test_status_run_json_matches_mcp_handler_output() -> None:
    """Golden test: CLI ``--json`` must reproduce the MCP meta payload exactly."""

    meta = _golden_meta()
    mcp_payload = MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="Run Projection\nRun: run_golden"),),
        meta=meta,
    )
    handler = _RecordingHandler(Result.ok(mcp_payload))

    with _patched_runner(handler):
        result = runner.invoke(
            app,
            ["status", "run", "exec_golden", "--json"],
        )

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == meta
    # The CLI must serialize via the same key ordering it documents.
    assert result.output.rstrip("\n") == json.dumps(meta, indent=2, sort_keys=True)


def test_status_run_positional_maps_to_execution_id() -> None:
    """The positional ``RUN_ID`` is shorthand for ``--execution-id``."""

    mcp_payload = MCPToolResult(content=(), meta=_golden_meta())
    handler = _RecordingHandler(Result.ok(mcp_payload))

    with _patched_runner(handler):
        result = runner.invoke(app, ["status", "run", "exec_golden", "--json"])

    assert result.exit_code == 0
    assert handler.last_arguments == {"execution_id": "exec_golden"}


def test_status_run_unknown_run_id_exits_with_code_2() -> None:
    """Unknown run anchors map to exit code ``2``."""

    handler = _RecordingHandler(Result.err(MCPToolError("No events found for projection query")))

    with _patched_runner(handler):
        result = runner.invoke(app, ["status", "run", "exec_missing", "--json"])

    assert result.exit_code == 2
    assert "No events found" in result.output


def test_status_run_missing_selector_exits_with_code_64() -> None:
    """Malformed input (no selector at all) maps to exit code ``64``."""

    result = runner.invoke(app, ["status", "run", "--json"])

    assert result.exit_code == 64
    assert "required" in result.output.lower()


def test_status_run_conflicting_selectors_exit_with_code_64() -> None:
    """RUN_ID combined with --session-id is malformed input."""

    result = runner.invoke(
        app,
        ["status", "run", "exec_golden", "--session-id", "session_xyz", "--json"],
    )

    assert result.exit_code == 64
    assert "session" in result.output.lower()


def test_status_run_conflicting_execution_id_exits_with_code_64() -> None:
    """RUN_ID and a different --execution-id must be flagged as malformed."""

    result = runner.invoke(
        app,
        [
            "status",
            "run",
            "exec_golden",
            "--execution-id",
            "exec_other",
            "--json",
        ],
    )

    assert result.exit_code == 64
    assert "different" in result.output.lower()


def test_status_run_session_id_only_still_supported() -> None:
    """Legacy ``--session-id`` invocations remain valid (no positional)."""

    mcp_payload = MCPToolResult(content=(), meta=_golden_meta())
    handler = _RecordingHandler(Result.ok(mcp_payload))

    with _patched_runner(handler):
        result = runner.invoke(
            app,
            ["status", "run", "--session-id", "session_legacy", "--json"],
        )

    assert result.exit_code == 0
    assert handler.last_arguments == {"session_id": "session_legacy"}
