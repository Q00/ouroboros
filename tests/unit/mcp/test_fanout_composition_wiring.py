"""Both composition roots point the producer at the store they write to.

Ouroboros builds its handlers in two places, and the split is not accidental:
``create_ouroboros_server`` wires the MCP server a client connects to, with an
event store, interview engines and a job manager behind it, while
``get_ouroboros_tools`` wires the lighter set an in-process runtime dispatches
to. Neither is a copy of the other — they inject different services.

What must not differ is where the two sides of a fan-out look. The submit side
publishes findings into a project-local store; the advisory producer points a
lane at them. Wired from separate arguments, a root can wire one and not the
other — which is what shipped once, invisibly, because every lane-level test
builds its own request and so never travels a composition at all (RFC #2153,
first implementation trap).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.definitions import get_ouroboros_tools
from ouroboros.mcp.tools.fanout_handler import SubmitFanoutResultsHandler
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler


def _publishes_into(handlers: Any) -> Any:
    """Return the store the submit side would publish into, if it can publish."""
    submit = next(
        (handler for handler in handlers if isinstance(handler, SubmitFanoutResultsHandler)),
        None,
    )
    return None if submit is None else submit.artifact_store


def _readers(handlers: Any) -> dict[str, Any]:
    """Return what each advisory producer would read from, keyed by class.

    **Both** producers, deliberately. The previous version of this helper looked
    only for the PM one, and that is exactly how a root wiring PM and forgetting
    the interview passed — a helper that searches for one thing cannot report
    the other missing (RFC #2153).
    """
    found: dict[str, Any] = {}
    for handler in handlers:
        if isinstance(handler, PMInterviewHandler | InterviewHandler):
            found[type(handler).__name__] = handler.findings_store
    assert set(found) == {"PMInterviewHandler", "InterviewHandler"}, (
        f"both producers must be registered; found {sorted(found)}"
    )
    return found


def test_the_server_points_the_producer_at_the_store_it_writes_to(tmp_path: Path) -> None:
    """The composition that ships, compared side against side by value.

    Equality is the assertion, not "both are set": two roots agreeing a path
    exists is not agreeing on which one, and a producer sent to a neighbouring
    directory reads exactly like a project where nothing has been found.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = create_ouroboros_server(
        project_dir=workspace,
        state_dir=tmp_path / "state",
        durable_jobs=False,
    )
    handlers = list(server._tool_handlers.values())

    publishes = _publishes_into(handlers)
    assert publishes is not None, "the server can always publish; this cannot be absent"
    assert all(reader is publishes for reader in _readers(handlers).values())


def test_the_runtime_tool_set_points_the_producer_at_the_store_it_writes_to(
    tmp_path: Path,
) -> None:
    """The other root, held to the same equality."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handlers = list(get_ouroboros_tools(project_dir=workspace))

    publishes = _publishes_into(handlers)
    assert publishes is not None, "a workspace was given, so publication is configured"
    assert all(reader is publishes for reader in _readers(handlers).values())


def test_a_root_that_cannot_store_findings_is_not_asked_to_carry_an_address() -> None:
    """No workspace means no store, and no store means nothing to point at.

    Held so the invariant above cannot later be tightened into demanding a
    workspace everywhere — that would be a rule with no defect behind it.
    """
    handlers = list(get_ouroboros_tools())

    assert _publishes_into(handlers) is None
    assert all(reader is None for reader in _readers(handlers).values())


def test_a_relative_workspace_survives_a_change_of_working_directory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The address is fixed when the store is built, not when a question is asked.

    Both roots accept a relative workspace. If each side turned it into an
    absolute path itself — the store when constructed, the producer when a lane
    was about to be told where to look — anything moving the process directory
    in between would put the reader in a store the writer never wrote to. The
    fix is not to canonicalise more carefully at both ends but to derive once
    and hand over, and this is what holds that.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.chdir(tmp_path)
    handlers = list(get_ouroboros_tools(project_dir=Path("workspace")))
    publishes = _publishes_into(handlers)
    assert publishes is not None

    monkeypatch.chdir(elsewhere)
    for reader in _readers(handlers).values():
        assert reader is publishes
        assert reader.root == workspace / ".ouroboros" / "artifacts"
