"""One workspace's fan-out surface: the store, the read-back, and the producer.

A fan-out writes its results into a project-local artifact store, and an
advisory producer points a lane at what is already there. Those two sides must
name the same directory, and they are wired from separate arguments in two
composition roots -- the MCP server's and the in-process runtime's tool set --
so either root can wire one side and not the other. That is not hypothetical:
it shipped once, and every lane-level test stayed green because each builds its
own request and so never travels the composition that omitted one.

One function both roots call is what keeps that from recurring by omission.
Where a root genuinely has less to give -- no event store, no engine, no
workspace -- it passes less and the handlers' defaults absorb it. The difference
between the roots is real and kept; how the two sides find each other is not,
and is written once.

**The address is derived once and handed over.** The producer takes the store's
own resolved root rather than deriving the same path from the same workspace a
second time. Two derivations are not one address: the store resolves when it is
constructed and a producer would resolve when a question is asked, so a relative
workspace and a change of process directory in between would aim the reader at a
store the writer never wrote to (RFC Q00/ouroboros#2153).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.fanout import FanoutRegistry
from ouroboros.mcp.tools.fanout_handler import (
    FetchArtifactHandler,
    SubmitFanoutResultsHandler,
    create_fanout_handlers,
)
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler


def create_fanout_wiring(
    *,
    fanout_registry: FanoutRegistry,
    workspace: Path | str | None,
    event_store: Any = None,
    state_dir: Path | None = None,
    handler_event_store: Any = None,
    interview_engine: Any = None,
    suppress_tool_use_prompt_cues: bool = False,
    llm_backend: str | None = None,
    agent_runtime_backend: str | None = None,
    opencode_mode: str | None = None,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
    host_dispatch_bridge: Any = None,
) -> tuple[
    SubmitFanoutResultsHandler,
    FetchArtifactHandler,
    InterviewHandler,
    PMInterviewHandler,
]:
    """Return the submit, fetch and PM handlers for one workspace.

    ``workspace`` of ``None`` is a root that stores nothing: the submit side is
    built without a disposable artifact service, and the producer is told no
    root. That is the honest state rather than a degraded one -- there is
    nothing published to point a lane at, and a lane sent looking would spend a
    tool call learning that.

    Both producers are built here rather than only the PM one: what may be
    reused is a fact about the system, and a fact is the same fact whichever
    interview needed it (RFC #2153). Building them together is also what keeps
    a root from wiring one and forgetting the other, which is how this shipped
    dead the first time.

    ``event_store`` reaches artifact publication; ``handler_event_store``
    reaches the two handlers. They are separate because the two composition roots
    differ there today, and collapsing them would hand one root an event store
    its PM handler has never had.
    """
    if workspace is None:
        submit = SubmitFanoutResultsHandler(fanout_registry=fanout_registry)
        fetch = FetchArtifactHandler()
    else:
        submit, fetch = create_fanout_handlers(
            fanout_registry,
            # Coerced here because callers legitimately hold either: a runtime
            # passes the string workspace it was configured with, the server a
            # path it already resolved.
            Path(workspace),
            event_store,
            ensure_ready=ensure_ready,
        )
    # Execution-kind submissions route to the parked HostDispatchRuntime
    # waiter; a root composed without a bridge leaves them failing closed.
    submit.host_dispatch_bridge = host_dispatch_bridge
    interview = InterviewHandler(
        interview_engine=interview_engine,
        event_store=handler_event_store,
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        fanout_registry=fanout_registry,
        suppress_tool_use_prompt_cues=suppress_tool_use_prompt_cues,
        findings_store=submit.artifact_store,
    )
    pm_interview = PMInterviewHandler(
        data_dir=state_dir,
        event_store=handler_event_store,
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        fanout_registry=fanout_registry,
        findings_store=submit.artifact_store,
    )
    return submit, fetch, interview, pm_interview


__all__ = ["create_fanout_wiring"]
