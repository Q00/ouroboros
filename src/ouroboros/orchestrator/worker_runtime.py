"""Provider-neutral leader-driven worker runtime.

This is the concrete realization of ``SubagentOrchestration.EXTERNAL_LEADER_DRIVEN``
(see :mod:`ouroboros.orchestrator.adapter`): ouroboros is the LEADER and drives
an addressable, resumable worker session DIRECTLY — it spawns a session, holds
its native id, and continues it across turns. The orchestration brain
(ParallelExecutor / AgentProcess / EventStore / validate_evidence) is untouched;
this class is pure transport below the ``AgentRuntime`` Protocol seam.

The whole point is GENERALITY: a provider becomes a worker pool by supplying a
thin :class:`LeaderDrivenWorkerTransport` (spawn + resume), not a bespoke
runtime. Codex (``codex mcp-server``), Claude (``claude -p --resume``), and any
future resumable provider plug into the SAME ``LeaderDrivenWorkerRuntime`` and
the SAME pool code — only the transport differs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import os
from typing import Any, Protocol

from ouroboros.core.errors import ProviderError
from ouroboros.core.session_signal import SessionSignalCapabilities
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
    SubagentOrchestration,
    TaskResult,
)
from ouroboros.orchestrator.subagent_label import derive_session_label


@dataclass(frozen=True, slots=True)
class WorkerTurn:
    """Normalized result of one worker turn from a transport.

    Attributes:
        text: The worker's final assistant text for this turn.
        session_id: Backend-native, resumable session id (codex threadId, claude
            session_id). ``None`` if the transport could not surface one — the
            turn still returns text but cannot be resumed.
        is_error: Whether the turn failed.
        error: Human-readable error message when ``is_error``.
        tool_events: Optional ordered (tool_name, detail) pairs observed during
            the turn, surfaced as ``assistant`` messages for TUI/telemetry.
        usage: Optional provider-reported token counters for attribution. Values
            remain raw here and are validated by the proof producer before use.
    """

    text: str
    session_id: str | None = None
    is_error: bool = False
    error: str | None = None
    tool_events: tuple[tuple[str, str], ...] = ()
    usage: dict[str, Any] | None = None


class LeaderDrivenWorkerTransport(Protocol):
    """Thin per-provider transport that spawns and resumes a worker session.

    Implementations own ONLY the provider mechanics (how to start a session and
    how to continue one); all ``AgentRuntime`` boilerplate lives in
    :class:`LeaderDrivenWorkerRuntime`.
    """

    @property
    def backend_name(self) -> str:
        """Canonical backend id stamped into the emitted ``RuntimeHandle``."""
        ...

    async def spawn(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        cwd: str | None,
        permission_mode: str | None,
        model: str | None,
        reasoning_effort: str | None,
        fork_from_session_id: str | None = None,
        label: str | None = None,
    ) -> WorkerTurn:
        """Start a NEW worker session and run the first turn.

        ``fork_from_session_id`` — when set, the new worker session should be
        FORKED from this (host) session so it inherits the host's context while
        getting a fresh, independent session id (it must NOT mutate the host
        session). Transports that cannot fork a host session of their provider
        ignore it and spawn a clean session instead.

        ``label`` — a human-readable name (e.g. ``"ooo: <task>"``) the transport
        attaches to the session so a human can find/manage the sub-agent in their
        provider's session picker. Ignored if the provider has no label surface.
        """
        ...

    async def resume(
        self,
        *,
        session_id: str,
        prompt: str,
        permission_mode: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> WorkerTurn:
        """Continue an existing worker, applying supported per-turn controls."""
        ...


class LeaderDrivenWorkerRuntime:
    """``AgentRuntime`` that drives any provider's worker session via a transport.

    Structurally satisfies the ``AgentRuntime`` Protocol (no inheritance). The
    same instance shape works for every provider; ``transport`` is the only
    moving part.
    """

    def __init__(
        self,
        *,
        transport: LeaderDrivenWorkerTransport,
        runtime_backend: str,
        llm_backend: str,
        cwd: str | os.PathLike[str] | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        reasoning_effort_support: ParamSupport = ParamSupport.IGNORED,
        enforceable_reasoning_efforts: frozenset[str] | None = None,
        model_override_support: ParamSupport = ParamSupport.IGNORED,
        targeted_resume: bool = True,
    ) -> None:
        self._transport = transport
        self._runtime_backend = runtime_backend
        self._llm_backend = llm_backend
        self._cwd = os.fspath(cwd) if cwd is not None else None
        self._permission_mode = permission_mode
        self._model = model
        self._reasoning_effort_support = reasoning_effort_support
        self._enforceable_reasoning_efforts = enforceable_reasoning_efforts
        self._model_override_support = model_override_support
        self._targeted_resume = targeted_resume
        self._provider_name = runtime_backend

    # -- AgentRuntime Protocol properties ---------------------------------

    @property
    def runtime_backend(self) -> str:
        return self._runtime_backend

    @property
    def llm_backend(self) -> str | None:
        return self._llm_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def capabilities(self) -> RuntimeCapabilities:
        # Native passthrough: the worker runs with whatever its provider is
        # natively configured with — ouroboros does not restrict its tools
        # (tool_restriction_support=IGNORED). The system prompt is delivered as a
        # native directive by the transport (codex developer-instructions /
        # claude system prompt), hence NATIVE. This runtime IS the concrete
        # EXTERNAL_LEADER_DRIVEN worker pool member.
        return replace(
            FULL_CAPABILITIES,
            targeted_resume=self._targeted_resume,
            system_prompt_support=ParamSupport.NATIVE,
            tool_restriction_support=ParamSupport.IGNORED,
            reasoning_effort_support=self._reasoning_effort_support,
            enforceable_reasoning_efforts=self._enforceable_reasoning_efforts,
            model_override_support=self._model_override_support,
            subagent_orchestration=SubagentOrchestration.EXTERNAL_LEADER_DRIVEN,
            session_signals=SessionSignalCapabilities(
                inform_delivery=self._targeted_resume,
                background_reply=self._targeted_resume,
                after_turn_delivery=self._targeted_resume,
            ),
        )

    # -- Execution --------------------------------------------------------

    def _build_handle(self, session_id: str | None) -> RuntimeHandle | None:
        if not self._targeted_resume or not session_id:
            return None
        return RuntimeHandle(
            backend=self._runtime_backend,
            kind="agent_runtime",
            native_session_id=session_id,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            updated_at=datetime.now(UTC).isoformat(),
        )

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Spawn (or resume) a worker turn and stream normalized messages.

        ``tools`` is accepted for Protocol compatibility but intentionally NOT
        enforced — native passthrough lets the worker use whatever its provider
        natively exposes.

        ``model`` is a per-call model-tier override; when set it wins over the
        constructor pin (``self._model``), while ``None`` falls back to that pin.
        The effective model and ``reasoning_effort`` are forwarded on both spawn
        and resume. Each transport applies only controls it can enforce and must
        advertise that truth through its runtime capabilities.
        """
        # A handle carrying ``fork_session`` is the HOST session delegated by the
        # parent (its native_session_id is the human's LIVE conversation). It is a
        # fork SOURCE, never a resume target: resuming it would append worker turns
        # to the human's live transcript and corrupt it. So we branch the worker
        # off it (transport spawns with ``--fork-session``) instead, yielding a
        # fresh child session id. Only a handle WITHOUT fork_session (one of our
        # own prior worker sessions) is a real resume target.
        fork_from_session_id: str | None = None
        prior_session_id: str | None = None
        if resume_handle is not None and resume_handle.metadata.get("fork_session"):
            fork_from_session_id = resume_handle.native_session_id
        elif resume_handle is not None:
            prior_session_id = resume_handle.native_session_id
        prior_session_id = prior_session_id or resume_session_id

        try:
            if prior_session_id:
                turn = await self._transport.resume(
                    session_id=prior_session_id,
                    prompt=prompt,
                    permission_mode=self._permission_mode,
                    model=model or self._model,
                    reasoning_effort=reasoning_effort,
                )
            else:
                turn = await self._transport.spawn(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    cwd=self._cwd,
                    permission_mode=self._permission_mode,
                    model=model or self._model,
                    reasoning_effort=reasoning_effort,
                    fork_from_session_id=fork_from_session_id,
                    label=derive_session_label(prompt),
                )
        except Exception as exc:  # transport failure → terminal error message
            handle = self._build_handle(prior_session_id)
            yield AgentMessage(
                type="result",
                content=f"{self._runtime_backend} worker turn failed: {exc}",
                data={"subtype": "error", "error_type": type(exc).__name__},
                resume_handle=handle,
            )
            return

        handle = self._build_handle(turn.session_id or prior_session_id)

        # Init message announces an addressable, resumable session (first turn only).
        if handle is not None and not prior_session_id and turn.session_id:
            yield AgentMessage(
                type="system",
                content=f"Session initialized: {turn.session_id}",
                data={"subtype": "init", "session_id": turn.session_id},
                resume_handle=handle,
            )

        for tool_name, detail in turn.tool_events:
            yield AgentMessage(
                type="assistant",
                content=detail,
                tool_name=tool_name,
                data={"tool_input": {}},
                resume_handle=handle,
            )

        # On an error turn the text is usually empty — surface the error string as
        # the message content so callers (and execute_task_to_result) see the cause.
        result_content = turn.text or (turn.error or "") if turn.is_error else turn.text
        yield AgentMessage(
            type="result",
            content=result_content,
            data={
                "subtype": "error" if turn.is_error else "success",
                "session_id": turn.session_id,
                **({"error": turn.error} if turn.error else {}),
                **({"usage": turn.usage} if turn.usage is not None else {}),
            },
            resume_handle=handle,
        )

    async def aclose(self) -> None:
        """Release any persistent worker connections held by the transport.

        Best-effort teardown for transports that keep warm sessions (e.g. the
        codex-mcp persistent pool). Idle sessions also self-close via their TTL,
        so this is a prompt-cleanup convenience rather than a correctness
        requirement. No-op for stateless transports (e.g. the Claude subprocess).
        """
        closer = getattr(self._transport, "aclose", None)
        if closer is not None:
            await closer()

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Collect the streamed turn into a single ``TaskResult``."""
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        final_handle = resume_handle
        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)
            if message.resume_handle is not None:
                final_handle = message.resume_handle
            if message.is_final:
                final_message = message.content
                success = not message.is_error
        if not success:
            return Result.err(
                ProviderError(
                    message=final_message,
                    provider=self._provider_name,
                    details={"messages": [m.content for m in messages]},
                )
            )
        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=final_handle.native_session_id if final_handle else None,
                resume_handle=final_handle,
            )
        )


__all__ = [
    "LeaderDrivenWorkerRuntime",
    "LeaderDrivenWorkerTransport",
    "WorkerTurn",
]
