"""GJC agent runtime backed by the supported Coordinator MCP / SDK lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

import anyio

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.gjc.sdk_client import (
    GjcCoordinatorClient,
    GjcCoordinatorError,
    GjcCoordinatorQuestion,
)
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    ResolvedWorkerCwd,
    RuntimeCapabilities,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
    resolve_worker_cwd,
    worker_cwd_failure_message,
)
from ouroboros.orchestrator.skill_intercept import SkillInterceptor

log = get_logger(__name__)
CoordinatorClientFactory = Callable[..., GjcCoordinatorClient]


class GjcRuntime:
    """Agent runtime that maps Ouroboros turns onto GJC SDK sessions."""

    _runtime_handle_backend = "gjc"
    _runtime_backend = "gjc"
    _requires_memory_gate = False
    _provider_name = "gjc"
    _log_namespace = "gjc_runtime"
    _display_name = "GJC"
    _default_cli_name = "gjc"
    _default_llm_backend = "gjc"

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | ResolvedWorkerCwd | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
        coordinator_client_factory: CoordinatorClientFactory = GjcCoordinatorClient,
        **_kwargs: Any,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = permission_mode
        self._model = model
        self._cwd = resolve_worker_cwd(cwd)
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend
        self._skills_dir = Path(skills_dir).expanduser() if skills_dir is not None else None
        self._timeout = stdout_idle_timeout_seconds or startup_output_timeout_seconds or 600.0
        self._coordinator_client_factory = coordinator_client_factory
        self._interceptor = SkillInterceptor(
            cwd=self._cwd,
            runtime_backend=self._runtime_backend,
            runtime_handle_backend=self._runtime_handle_backend,
            permission_mode=self._permission_mode,
            llm_backend=self._llm_backend,
            log_namespace=self._log_namespace,
            skills_dir=self._skills_dir,
            skill_dispatcher=skill_dispatcher,
        )
        log.info(
            f"{self._log_namespace}.initialized",
            cli_path=self._cli_path,
            cwd=self._cwd,
            model=model,
            transport="coordinator_mcp",
        )

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

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
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.TRANSLATED,
            empty_tool_restriction_support=ParamSupport.IGNORED,
            permission_mode_support=ParamSupport.IGNORED,
        )

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = shutil.which(self._default_cli_name) or self._default_cli_name
        path = Path(candidate).expanduser()
        return str(path) if path.exists() else candidate

    def _build_runtime_handle(
        self,
        session_id: str | None,
        turn_id: str | None,
        current_handle: RuntimeHandle | None,
        question: GjcCoordinatorQuestion | None = None,
        *,
        operation_phase: str,
        idempotency_key: str | None = None,
    ) -> RuntimeHandle:
        metadata = dict(current_handle.metadata) if current_handle is not None else {}
        if turn_id:
            metadata["turn_id"] = turn_id
        else:
            metadata.pop("turn_id", None)
        metadata["transport"] = "gjc-coordinator-mcp"
        metadata["gjc_operation_phase"] = operation_phase
        if idempotency_key:
            metadata["gjc_idempotency_key"] = idempotency_key
        else:
            metadata.pop("gjc_idempotency_key", None)
        if question is None:
            metadata.pop("pending_question", None)
        else:
            metadata["pending_question"] = {
                "question_id": question.question_id,
                "answer_binding": question.answer_binding,
                "prompt": question.prompt,
                "options": list(question.options),
                "multi": question.multi,
            }
        handle = RuntimeHandle(
            backend=self._runtime_handle_backend,
            kind="agent_runtime",
            native_session_id=session_id,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            metadata=metadata,
        )
        if session_id:
            # Broker-owned GJC sessions outlive the coordinator connection, so
            # every session-bearing handle must expose a real terminate path or
            # the run-level reclamation sweeps silently skip gjc workers.
            handle = handle.bind_controls(terminate_callback=self._terminate_session)
        return handle

    async def _terminate_session(self, handle: RuntimeHandle) -> bool:
        session_id = handle.native_session_id
        if not session_id:
            return False
        client = self._coordinator_client_factory(
            cli_path=self._cli_path,
            cwd=self._cwd,
            timeout=self._timeout,
        )
        try:
            await client.connect()
            await client.stop_session(session_id)
            return True
        except GjcCoordinatorError as exc:
            log.warning(
                f"{self._log_namespace}.terminate_failed",
                session_id=session_id,
                code=exc.code,
            )
            return False
        finally:
            try:
                await client.close()
            except GjcCoordinatorError:
                pass

    @staticmethod
    def _compose_prompt(prompt: str, tools: list[str] | None, system_prompt: str | None) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(f"## System Instructions\n{system_prompt}")
        if tools:
            parts.append(
                "## Tooling Guidance\nPrefer these tools:\n"
                + "\n".join(f"- {tool}" for tool in tools)
            )
        parts.append(prompt)
        return "\n\n".join(part for part in parts if part.strip())

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        cwd_failure = worker_cwd_failure_message(
            self._cwd,
            runtime_backend=self._runtime_backend,
            resume_handle=resume_handle,
        )
        if cwd_failure is not None:
            yield cwd_failure
            return

        intercepted_messages = await self._interceptor.maybe_dispatch(prompt, resume_handle)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                yield AgentMessage(
                    type=message.type,
                    content=message.content,
                    data=message.data,
                    tool_name=message.tool_name,
                    resume_handle=message.resume_handle,
                )
            return

        composed_prompt = self._compose_prompt(prompt, tools, system_prompt)
        requested_session_id = (
            resume_handle.native_session_id if resume_handle else resume_session_id
        )
        session_id: str | None = requested_session_id
        client: GjcCoordinatorClient | None = None
        keep_session_for_resume = False
        session_stopped = False
        handle = resume_handle
        metadata = resume_handle.metadata if resume_handle else {}
        phase = metadata.get("gjc_operation_phase")
        prior_turn_id = metadata.get("turn_id")
        prior_key = metadata.get("gjc_idempotency_key")
        try:
            client = self._coordinator_client_factory(
                cli_path=self._cli_path,
                cwd=self._cwd,
                timeout=self._timeout,
            )
            await client.connect()
            pending = metadata.get("pending_question")
            if requested_session_id and isinstance(pending, dict):
                question_id = pending.get("question_id")
                answer_binding = pending.get("answer_binding")
                if not all(
                    isinstance(value, str) and value
                    for value in (question_id, answer_binding, prior_turn_id)
                ):
                    raise GjcCoordinatorError(
                        "GJC resume handle contains an invalid pending question.",
                        code="invalid_resume_handle",
                    )
                question = GjcCoordinatorQuestion(
                    session_id=requested_session_id,
                    turn_id=prior_turn_id,
                    question_id=question_id,
                    answer_binding=answer_binding,
                    prompt=str(pending.get("prompt") or ""),
                    options=tuple(
                        value for value in pending.get("options", []) if isinstance(value, str)
                    ),
                    multi=pending.get("multi") is True,
                )
                operation_key = (
                    prior_key
                    if phase == "answer_pending" and isinstance(prior_key, str) and prior_key
                    else f"ouroboros-answer-{uuid4().hex}"
                )
                handle = self._build_runtime_handle(
                    requested_session_id,
                    prior_turn_id,
                    resume_handle,
                    question,
                    operation_phase="answer_pending",
                    idempotency_key=operation_key,
                )
                await client.submit_question_answer(
                    question,
                    prompt,
                    idempotency_key=operation_key,
                )
                turn_id = prior_turn_id
            elif (
                requested_session_id
                and phase == "awaiting"
                and isinstance(prior_turn_id, str)
                and prior_turn_id
            ):
                # The prior mutation already returned a durable turn identity.
                # Resume observation instead of dispatching duplicate work.
                turn_id = prior_turn_id
            elif requested_session_id:
                operation_key = (
                    prior_key
                    if phase == "prompt_pending" and isinstance(prior_key, str) and prior_key
                    else f"ouroboros-prompt-{uuid4().hex}"
                )
                handle = self._build_runtime_handle(
                    requested_session_id,
                    None,
                    resume_handle,
                    operation_phase="prompt_pending",
                    idempotency_key=operation_key,
                )
                turn_id = await client.send_prompt(
                    requested_session_id,
                    composed_prompt,
                    idempotency_key=operation_key,
                )
            else:
                operation_key = (
                    prior_key
                    if phase == "start_pending" and isinstance(prior_key, str) and prior_key
                    else f"ouroboros-start-{uuid4().hex}"
                )
                handle = self._build_runtime_handle(
                    None,
                    None,
                    resume_handle,
                    operation_phase="start_pending",
                    idempotency_key=operation_key,
                )
                session = await client.start_session(
                    composed_prompt,
                    model=self._model,
                    idempotency_key=operation_key,
                )
                session_id = session.session_id
                turn_id = session.turn_id
            handle = self._build_runtime_handle(
                session_id,
                turn_id,
                handle,
                operation_phase="awaiting",
            )
            turn = await client.await_turn(session_id, turn_id)
            if turn.status == "waiting_for_answer":
                keep_session_for_resume = True
                question = turn.question
                answer_key = f"ouroboros-answer-{uuid4().hex}"
                handle = self._build_runtime_handle(
                    session_id,
                    turn_id,
                    handle,
                    question,
                    operation_phase="answer_pending",
                    idempotency_key=answer_key,
                )
                yield AgentMessage(
                    type="result",
                    content=question.prompt if question else "GJC is waiting for user input.",
                    data={
                        "subtype": "error",
                        "error_type": "GjcQuestionRequired",
                        "recoverable": True,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "question_id": question.question_id if question else None,
                        "options": list(question.options) if question else [],
                    },
                    resume_handle=handle,
                )
                return
            terminal_phase = "completed" if turn.succeeded else "failed"
            handle = self._build_runtime_handle(
                session_id,
                turn_id,
                handle,
                operation_phase=terminal_phase,
            )
            content = (
                (turn.text or await client.read_last_assistant(session_id))
                if turn.succeeded
                else None
            )
            # The turn is terminal and non-resumable: reclaim the broker-owned
            # worker session BEFORE publishing the result, so a consumer that
            # stops iterating at the terminal message (or never closes the
            # generator) cannot leave the session running. Question turns and
            # coordinator errors keep the session alive for resume.
            session_stopped = True
            try:
                await client.stop_session(session_id)
            except GjcCoordinatorError:
                pass
            if not turn.succeeded:
                yield AgentMessage(
                    type="result",
                    content=turn.error or f"GJC turn ended with status {turn.status}",
                    data={
                        "subtype": "error",
                        "error_type": "GjcTurnError",
                        "status": turn.status,
                    },
                    resume_handle=handle,
                )
                return
            yield AgentMessage(
                type="result",
                content=content or "GJC task completed.",
                data={"subtype": "success", "transport": "gjc-coordinator-mcp"},
                resume_handle=handle,
            )
        except GjcCoordinatorError as exc:
            keep_session_for_resume = True
            yield AgentMessage(
                type="result",
                content=str(exc),
                data={
                    "subtype": "error",
                    "error_type": type(exc).__name__,
                    "code": exc.code,
                },
                resume_handle=handle,
            )
        finally:
            # Runs under caller cancellation and generator finalization too, so
            # the cleanup awaits are shielded: a session bound after
            # ``start_session`` must be reclaimed even when the consumer never
            # drains the stream or cancels while ``await_turn`` blocks. Only
            # explicitly resumable states (question turns, coordinator errors)
            # keep the broker-owned session alive.
            if client is not None:
                with anyio.CancelScope(shield=True):
                    if (
                        session_id is not None
                        and not keep_session_for_resume
                        and (not session_stopped)
                    ):
                        try:
                            await client.stop_session(session_id)
                        except GjcCoordinatorError:
                            pass
                    try:
                        await client.close()
                    except GjcCoordinatorError:
                        pass

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
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
                    details={"messages": [message.content for message in messages]},
                )
            )
        return Result.ok(
            TaskResult(
                success=True,
                final_message=final_message,
                messages=tuple(messages),
                session_id=final_handle.native_session_id if final_handle else None,
                resume_handle=final_handle,
            )
        )


__all__ = ["GjcRuntime"]
