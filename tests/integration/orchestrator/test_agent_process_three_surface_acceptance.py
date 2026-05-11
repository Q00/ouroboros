"""Three-surface AgentProcess acceptance test — issue #518.

Verifies that evolve_step, ralph, and execute_seed all emit identical
lifecycle patterns when wrapped in AgentProcess.spawn:

    RUNNING (continue) directive on spawn
    CONVERGE (converge) directive on success

And that a cooperative cancel from outside cleanly stops each surface.

NOTE: evolve_step real wrapping lands in PR-D.  This test exercises a
trivial AgentProcess-wrapped coroutine that mimics evolve_step's shape
(spawn → await work → return) so the three-surface contract is pinned
without a hard dependency on PR-D having landed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ouroboros.orchestrator.agent_process import AgentProcess, AgentProcessStatus

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[Any] = []

    async def append(self, event: Any) -> None:
        self.appended.append(event)


def _directives(store: _FakeEventStore) -> list[str]:
    return [
        e.data["directive"]
        for e in store.appended
        if getattr(e, "type", None) == "control.directive.emitted"
    ]


async def _wait_for_status(handle, status: AgentProcessStatus, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if handle.status() is status:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(
                f"Handle status did not reach {status!r} within {timeout}s; "
                f"current: {handle.status()!r}"
            )
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Surface simulators
# ---------------------------------------------------------------------------


async def _evolve_step_surface(ap: AgentProcess) -> AgentProcessStatus:
    """Mimics evolve_step's shape: spawn → do work → return success.

    Real wrapping lands in PR-D.  This stand-in confirms the lifecycle
    contract without a hard dependency on that PR.
    """
    result_box: list[str] = []

    async def _work(handle) -> None:
        await handle.wait_unpaused()
        if handle.should_cancel():
            return
        # Simulate a unit of evolve_step work.
        await asyncio.sleep(0)
        result_box.append("done")

    handle = await ap.spawn(intent="evolve_step", work_fn=_work)
    return await handle.wait_until_complete(timeout=2.0)


async def _ralph_surface(ap: AgentProcess) -> AgentProcessStatus:
    """Mimics one ralph iteration wrapped in AgentProcess."""
    result_box: list[str] = []

    async def _work(handle) -> None:
        await handle.wait_unpaused()
        if handle.should_cancel():
            return
        await asyncio.sleep(0)
        result_box.append("done")

    handle = await ap.spawn(intent="ralph_iteration:1", work_fn=_work)
    return await handle.wait_until_complete(timeout=2.0)


async def _execute_seed_surface(ap: AgentProcess) -> AgentProcessStatus:
    """Mimics execute_seed wrapped in AgentProcess (StartExecuteSeedHandler shape)."""
    result_box: list[str] = []

    async def _work(handle) -> None:
        await handle.wait_unpaused()
        if handle.should_cancel():
            return
        await asyncio.sleep(0)
        result_box.append("done")

    handle = await ap.spawn(intent="execute_seed", work_fn=_work)
    return await handle.wait_until_complete(timeout=2.0)


# ---------------------------------------------------------------------------
# Acceptance test: identical lifecycle pattern on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_surfaces_emit_running_then_converge_on_success() -> None:
    """Each surface emits RUNNING on spawn and CONVERGE on success."""
    for surface_fn, label in [
        (_evolve_step_surface, "evolve_step"),
        (_ralph_surface, "ralph"),
        (_execute_seed_surface, "execute_seed"),
    ]:
        store = _FakeEventStore()
        ap = AgentProcess(event_store=store)

        final = await surface_fn(ap)

        directives = _directives(store)
        assert final is AgentProcessStatus.COMPLETED, f"{label}: expected COMPLETED, got {final!r}"
        assert directives[0] == "continue", (
            f"{label}: first directive must be 'continue' (RUNNING), got {directives[0]!r}"
        )
        assert directives[-1] == "converge", (
            f"{label}: last directive must be 'converge' (CONVERGE), got {directives[-1]!r}"
        )


# ---------------------------------------------------------------------------
# Acceptance test: cooperative cancel stops each surface cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_surfaces_cancel_cleanly() -> None:
    """A cooperative cancel from outside terminates each surface without raising."""
    for intent_label, label in [
        ("evolve_step", "evolve_step"),
        ("ralph_iteration:1", "ralph"),
        ("execute_seed", "execute_seed"),
    ]:
        store = _FakeEventStore()
        ap = AgentProcess(event_store=store)

        started = asyncio.Event()
        release = asyncio.Event()

        async def _long_work(handle, _started=started, _release=release) -> None:
            _started.set()
            await handle.wait_unpaused()
            if handle.should_cancel():
                return
            # Block until cancelled.
            while not handle.should_cancel():
                await asyncio.sleep(0.005)

        handle = await ap.spawn(intent=intent_label, work_fn=_long_work)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        await handle.cancel(reason=f"test cancel: {label}")
        final = await handle.wait_until_complete(timeout=2.0)

        assert final is AgentProcessStatus.CANCELLED, (
            f"{label}: expected CANCELLED after cancel(), got {final!r}"
        )
        directives = _directives(store)
        assert "cancel" in directives, (
            f"{label}: expected a 'cancel' directive after cancel(), got {directives!r}"
        )
