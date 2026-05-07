"""Adapters from AutoPipeline interfaces to existing Ouroboros handlers."""

from __future__ import annotations

from pathlib import Path

import yaml

from ouroboros.auto.interview_driver import InterviewBackend, InterviewTurn
from ouroboros.core.seed import Seed
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.tools.authoring_handlers import (
    _DATA_DIR,
    GenerateSeedHandler,
    InterviewHandler,
)
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.types import MCPToolResult


class HandlerError(RuntimeError):
    """Raised when an MCP handler returns an error result."""


class PartialInterviewStartError(HandlerError):
    """Raised when interview start failed but a session_id was persisted server-side.

    Carries the persisted ``session_id`` so callers (e.g. the auto interview
    driver) can record it on durable state and resume the same interview
    after a transient first-question failure such as an LLM timeout.
    See Q00/ouroboros#687.
    """

    def __init__(self, message: str, *, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


def _unwrap(result, *, tool_name: str) -> MCPToolResult:
    if result.is_err:
        error: MCPServerError = result.error
        raise HandlerError(f"{tool_name} failed: {error}")
    value = result.value
    if value.is_error:
        text = value.content[0].text if value.content else "handler returned error"
        raise HandlerError(f"{tool_name} failed: {text}")
    return value


class HandlerInterviewBackend(InterviewBackend):
    """InterviewBackend backed by ``ouroboros_interview`` handler calls."""

    def __init__(self, handler: InterviewHandler, *, cwd: str) -> None:
        self.handler = handler
        self.cwd = cwd

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:
        arguments: dict[str, str] = {"initial_context": goal, "cwd": cwd or self.cwd}
        if interview_id:
            arguments["interview_id"] = interview_id
        outcome = await self.handler.handle(arguments)
        # Recoverable error path: handler persisted state but failed to
        # produce the first question.  Surface the session_id so the auto
        # driver can save it on the AutoPipelineState before reporting
        # blocked.
        if not outcome.is_err:
            value = outcome.value
            if value.is_error:
                meta = value.meta or {}
                session_id = _optional_str(meta.get("session_id")) or interview_id
                if session_id:
                    text = (
                        value.content[0].text
                        if value.content
                        else "ouroboros_interview returned error"
                    )
                    raise PartialInterviewStartError(
                        f"ouroboros_interview failed: {text}",
                        session_id=session_id,
                    )
        result = _unwrap(outcome, tool_name="ouroboros_interview")
        return _turn_from_result(result)

    async def answer(self, session_id: str, answer: str) -> InterviewTurn:
        result = _unwrap(
            await self.handler.handle({"session_id": session_id, "answer": answer}),
            tool_name="ouroboros_interview",
        )
        return _turn_from_result(result, fallback_session_id=session_id)

    async def resume(self, session_id: str) -> InterviewTurn:
        result = _unwrap(
            await self.handler.handle({"session_id": session_id}),
            tool_name="ouroboros_interview",
        )
        return _turn_from_result(result, fallback_session_id=session_id)

    def is_session_persisted(self, session_id: str) -> bool:
        """Return True when ``interview_<session_id>.json`` exists on disk.

        Used by ``AutoInterviewDriver`` to decide whether a pre-allocated
        id may be retained on auto state after a driver-level
        ``asyncio.wait_for`` cancel — without this probe the driver cannot
        distinguish "handler crashed before persisting" from "handler
        persisted then got cancelled".  See Q00/ouroboros#687.
        """
        if not session_id:
            return False
        state_dir = self.handler.data_dir or _DATA_DIR
        return (state_dir / f"interview_{session_id}.json").exists()


class HandlerSeedGenerator:
    """Callable seed generator backed by ``ouroboros_generate_seed``."""

    def __init__(self, handler: GenerateSeedHandler) -> None:
        self.handler = handler

    async def __call__(self, session_id: str) -> Seed:
        result = _unwrap(
            await self.handler.handle({"session_id": session_id}),
            tool_name="ouroboros_generate_seed",
        )
        text = result.content[0].text if result.content else ""
        seed_yaml = _extract_seed_yaml(text)
        raw = yaml.safe_load(seed_yaml)
        if not isinstance(raw, dict):
            raise HandlerError("ouroboros_generate_seed returned non-object Seed YAML")
        return Seed.from_dict(raw)


class HandlerRunStarter:
    """Callable run starter backed by ``ouroboros_start_execute_seed``."""

    def __init__(self, handler: StartExecuteSeedHandler, *, cwd: str) -> None:
        self.handler = handler
        self.cwd = cwd

    async def __call__(self, seed: Seed) -> dict[str, object]:
        seed_yaml = yaml.dump(
            seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        result = _unwrap(
            await self.handler.handle({"seed_content": seed_yaml, "cwd": self.cwd}),
            tool_name="ouroboros_start_execute_seed",
        )
        meta = result.meta or {}
        run_meta: dict[str, object] = {
            "job_id": _optional_str(meta.get("job_id")),
            "session_id": _optional_str(meta.get("session_id")),
            "execution_id": _optional_str(meta.get("execution_id")),
        }
        if isinstance(meta.get("_subagent"), dict):
            run_meta["_subagent"] = meta["_subagent"]
        return run_meta


def load_seed(path: str | Path) -> Seed:
    """Load a persisted auto-generated Seed."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HandlerError(f"Seed file is not an object: {path}")
    return Seed.from_dict(raw)


def save_seed(seed: Seed, *, seeds_dir: Path | None = None) -> str:
    """Persist an auto-generated Seed in the standard seed directory."""
    directory = seeds_dir or (Path.home() / ".ouroboros" / "seeds")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{seed.metadata.seed_id}.yaml"
    path.write_text(
        yaml.dump(seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(path)


def _turn_from_result(
    result: MCPToolResult, *, fallback_session_id: str | None = None
) -> InterviewTurn:
    meta = result.meta or {}
    session_id = _optional_str(meta.get("session_id")) or fallback_session_id
    if not session_id:
        raise HandlerError("ouroboros_interview did not return a session_id")
    text = result.content[0].text if result.content else ""
    return InterviewTurn(
        question=_extract_interview_question(text, session_id=session_id),
        session_id=session_id,
        seed_ready=bool(meta.get("seed_ready")),
        completed=bool(meta.get("completed")),
    )


def _extract_interview_question(text: str, *, session_id: str) -> str:
    """Strip this session's human-readable interview envelope from handler text."""
    stripped = text.strip()
    if not stripped:
        return ""
    if "\n\n" in stripped:
        head, tail = stripped.split("\n\n", 1)
        if head in {
            f"Interview started. Session ID: {session_id}",
            f"Session {session_id}",
        }:
            return tail.strip()
    return stripped


def _extract_seed_yaml(text: str) -> str:
    marker = "--- Seed YAML ---"
    if marker not in text:
        raise HandlerError("Seed response did not include Seed YAML marker")
    return text.split(marker, 1)[1].strip()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
