"""PM Interview Handler for MCP server.

Mirrors the existing InterviewHandler pattern from definitions.py but wraps
PMInterviewEngine instead of InterviewEngine.  The handler adds a thin MCP
layer on top of the engine: flat optional parameters, pm_meta persistence,
and deferred/decide-later diff computation.

The diff computation is the core value-add of this handler: before calling
``ask_next_question`` it snapshots the lengths of the engine's
``deferred_items`` and ``decide_later_items`` lists, and after the call
it slices the new entries to produce accurate per-call diffs that are
returned in the response metadata.

Interview completion is determined **solely** by the engine — either by
ambiguity scoring (score ≤ 0.2 means requirements are clear enough) or by
ambiguity scoring.  User controls when to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.ambiguity import AMBIGUITY_THRESHOLD
from ouroboros.bigbang.interview import (
    InterviewRound,
    InterviewState,
    InterviewStateStore,
)
from ouroboros.bigbang.pm_completion import (
    build_pm_completion_summary,
    maybe_complete_pm_interview,
)
from ouroboros.bigbang.pm_document import save_pm_document
from ouroboros.bigbang.pm_interview import PMInterviewEngine, PMInterviewStateStore
from ouroboros.config import get_clarification_model
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.layers.gate import AgentMode, get_agent_mode
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.mcp.tools.question_specs import build_question_ui_meta
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore
from ouroboros.pm.handoff import build_pm_dev_handoff_next_step
from ouroboros.providers import create_llm_adapter

log = structlog.get_logger()

# Hard cap on interview rounds in MCP mode.  The engine's ambiguity scorer
# should trigger completion well before this, but this prevents runaway loops.


_DATA_DIR = Path.home() / ".ouroboros" / "data"


def _count_answered_rounds(state: InterviewState) -> int:
    """Return the number of answered rounds."""
    return sum(1 for round_ in state.rounds if round_.user_response is not None)


def _last_classification(engine: PMInterviewEngine) -> str | None:
    """Return the output_type string of the engine's last classification, or None.

    Delegates to ``engine.get_last_classification()``.
    """
    return engine.get_last_classification()


def _detect_action(arguments: dict[str, Any]) -> str:
    """Auto-detect the action from parameter presence when action param is omitted.

    Detection rules (evaluated in order):
    1. If ``action`` is explicitly provided, return it as-is.
    2. If ``selected_repos`` **and** ``initial_context`` both present →
       ``"start"`` (backward-compat 1-step, AC 8).
    3. If ``selected_repos`` is present (without ``initial_context``) →
       ``"select_repos"`` (2-step start step 2).
    4. If ``initial_context`` is present → ``"start"``
    5. If ``session_id`` is present (with or without ``answer``) → ``"resume"``
    6. Otherwise → ``"unknown"`` (caller should return an error).
    """
    explicit = arguments.get("action")
    if explicit:
        return explicit

    if arguments.get("selected_repos") is not None:
        # Backward compat (AC 8): when both initial_context and selected_repos
        # are present, treat as 1-step start so the caller skips step 1.
        if arguments.get("initial_context"):
            return "start"
        return "select_repos"

    if arguments.get("initial_context"):
        return "start"

    if arguments.get("session_id"):
        return "resume"

    return "unknown"


@dataclass
class PMInterviewHandler:
    """Handler for the ouroboros_pm_interview MCP tool.

    Manages PM-focused interviews with question classification,
    deferred item tracking, and per-call diff computation.

    Interview completion is determined by the engine's ambiguity
    scorer (score ≤ 0.2).  User controls when to stop.

    The handler wraps PMInterviewEngine and adds:
    - Flat MCP parameter interface (session_id, action, answer, cwd, initial_context)
    - pm_meta_{session_id}.json persistence for PM-specific state
    - Deferred/decide-later diff computation per ask_next_question call
    - Automatic completion detection via ambiguity scoring
    """

    pm_engine: PMInterviewEngine | None = field(default=None, repr=False)
    data_dir: Path | None = field(default=None, repr=False)
    llm_adapter: Any | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_mode: AgentMode | None = field(default=None, repr=False)
    _pm_state_store: PMInterviewStateStore | None = field(default=None, repr=False)
    _interview_state_store: InterviewStateStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition with flat optional parameters."""
        return MCPToolDefinition(
            name="ouroboros_pm_interview",
            description=(
                "PM interview for product requirements gathering. "
                "Start with initial_context, inspect state with session_id, "
                "record a completed native PM turn with action='record_turn', "
                "or generate PM seed with action='generate'."
            ),
            parameters=(
                MCPToolParameter(
                    name="initial_context",
                    type=ToolInputType.STRING,
                    description="Initial product description to start a new PM interview",
                    required=False,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Session ID to resume an existing PM interview",
                    required=False,
                ),
                MCPToolParameter(
                    name="answer",
                    type=ToolInputType.STRING,
                    description=(
                        "PM's response to the current interview question. "
                        "Used with action=record_turn or compatibility resume flows."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description=(
                        "Action to perform. Native state actions: start, state, record_turn, score, complete. "
                        "Legacy action=record remains for backward compatibility, but native flows should use record_turn. "
                        "Compatibility/internal actions: select_repos, resume, generate. "
                        "When omitted, behavior is auto-routed by agent mode."
                    ),
                    required=False,
                    enum=(
                        "start",
                        "state",
                        "record_turn",
                        "record",
                        "score",
                        "complete",
                        "select_repos",
                        "resume",
                        "generate",
                    ),
                ),
                MCPToolParameter(
                    name="type",
                    type=ToolInputType.STRING,
                    description=(
                        "Legacy-only subtype for action=record: 'question' or 'answer'. "
                        "Native callers should use action=record_turn instead."
                    ),
                    required=False,
                    enum=("question", "answer"),
                ),
                MCPToolParameter(
                    name="question",
                    type=ToolInputType.STRING,
                    description="Question text. Used with action=record_turn.",
                    required=False,
                ),
                MCPToolParameter(
                    name="classification",
                    type=ToolInputType.STRING,
                    description="PM classification for the recorded question: passthrough, reframed, deferred, decide_later.",
                    required=False,
                    enum=("passthrough", "reframed", "deferred", "decide_later"),
                ),
                MCPToolParameter(
                    name="original_question",
                    type=ToolInputType.STRING,
                    description="Original technical question when recording a reframed PM-facing question.",
                    required=False,
                ),
                MCPToolParameter(
                    name="deferred_this_round",
                    type=ToolInputType.ARRAY,
                    description="Original technical questions deferred to the dev interview on this turn.",
                    required=False,
                    items={"type": "string"},
                ),
                MCPToolParameter(
                    name="decide_later_this_round",
                    type=ToolInputType.ARRAY,
                    description="Original technical questions intentionally left for later decisions on this turn.",
                    required=False,
                    items={"type": "string"},
                ),
                MCPToolParameter(
                    name="goal_clarity",
                    type=ToolInputType.NUMBER,
                    description="Goal clarity score (0.0-1.0). Used with action=score.",
                    required=False,
                ),
                MCPToolParameter(
                    name="constraint_clarity",
                    type=ToolInputType.NUMBER,
                    description="Constraint clarity score (0.0-1.0). Used with action=score.",
                    required=False,
                ),
                MCPToolParameter(
                    name="success_criteria_clarity",
                    type=ToolInputType.NUMBER,
                    description="Success criteria clarity score (0.0-1.0). Used with action=score.",
                    required=False,
                ),
                MCPToolParameter(
                    name="context_clarity",
                    type=ToolInputType.NUMBER,
                    description="Codebase context clarity score (0.0-1.0). Used with action=score for brownfield.",
                    required=False,
                ),
                MCPToolParameter(
                    name="is_brownfield",
                    type=ToolInputType.BOOLEAN,
                    description="Whether this is a brownfield PM interview. Affects scoring weights.",
                    required=False,
                ),
                MCPToolParameter(
                    name="ambiguity_score",
                    type=ToolInputType.NUMBER,
                    description=(
                        "Explicit ambiguity score override. "
                        "Used with action=record_turn or action=complete."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="cwd",
                    type=ToolInputType.STRING,
                    description=(
                        "Working directory for PM document output. "
                        "Defaults to current working directory. "
                        "Brownfield context is loaded from DB (is_default=true)."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="selected_repos",
                    type=ToolInputType.ARRAY,
                    description=(
                        "List of repository paths selected for brownfield context "
                        "(2-step start: returned by step 1, sent back in step 2). "
                        "All repos are assigned role=main. "
                        "When provided with initial_context, starts the interview "
                        "with the selected brownfield repos."
                    ),
                    required=False,
                    items={"type": "string"},
                ),
            ),
        )

    def _get_engine(self) -> PMInterviewEngine:
        """Return the injected engine or create a new one using the server's configured backend."""
        if self.pm_engine is not None:
            return self.pm_engine
        adapter = self.llm_adapter or create_llm_adapter(
            backend=self.llm_backend,
            max_turns=1,
            use_case="interview",
            allowed_tools=[],
        )
        model = get_clarification_model(self.llm_backend)
        return PMInterviewEngine.create(
            llm_adapter=adapter,
            state_dir=self.data_dir or _DATA_DIR,
            model=model,
        )

    def _get_pm_state_store(self) -> PMInterviewStateStore:
        """Return the PM metadata store for this handler."""
        if self._pm_state_store is None:
            self._pm_state_store = PMInterviewStateStore(data_dir=self.data_dir or _DATA_DIR)
        return self._pm_state_store

    def _get_interview_state_store(self) -> InterviewStateStore:
        """Return a reusable interview state store for native PM actions."""
        if self._interview_state_store is None:
            self._interview_state_store = InterviewStateStore(
                state_dir=self.data_dir or _DATA_DIR,
            )
        return self._interview_state_store

    def _load_pm_session_meta(self, session_id: str) -> dict[str, Any] | None:
        """Load PM session metadata without mutating the engine."""
        return self._get_pm_state_store().load_meta_dict(session_id)

    def _restore_pm_session_meta(
        self,
        engine: PMInterviewEngine,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Load PM session metadata and restore it into the engine."""
        meta = self._load_pm_session_meta(session_id)
        if meta:
            engine.restore_meta(meta)
        return meta

    def _save_pm_session_meta(
        self,
        session_id: str,
        *,
        engine: PMInterviewEngine | None = None,
        cwd: str = "",
        status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Persist PM session metadata for the current handler session."""
        store = self._get_pm_state_store()
        meta = store.build_meta(engine=engine, cwd=cwd, status=status, extra=extra)
        path = store.save_meta(session_id, meta)
        log.debug("pm_handler.meta_saved", session_id=session_id, path=str(path))

    async def _load_state_or_error(
        self,
        engine: PMInterviewEngine | InterviewStateStore,
        session_id: str,
    ) -> tuple[InterviewState | None, Result[MCPToolResult, MCPServerError] | None]:
        """Load interview state and convert failures to MCP tool errors."""
        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return None, Result.err(
                MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
            )
        return load_result.value, None

    async def _load_state_and_restore_meta_or_error(
        self,
        engine: PMInterviewEngine,
        session_id: str,
    ) -> tuple[InterviewState | None, Result[MCPToolResult, MCPServerError] | None]:
        """Load interview state and restore PM metadata into the engine."""
        state, error = await self._load_state_or_error(engine, session_id)
        if error is not None or state is None:
            return None, error
        self._restore_pm_session_meta(engine, session_id)
        return state, None

    @staticmethod
    def _ambiguity_gate_response(
        session_id: str,
        ambiguity_score: float | None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Build a response refusing premature PM completion."""
        score_display = f"{ambiguity_score:.2f}" if ambiguity_score is not None else "unknown"
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"Cannot complete yet — ambiguity score {score_display} exceeds "
                            f"threshold {AMBIGUITY_THRESHOLD:.2f}. "
                            "Please ask a few more PM questions to clarify the product."
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "ambiguity_score": ambiguity_score,
                    "seed_ready": False,
                },
            )
        )

    async def _persist_state_and_meta_or_error(
        self,
        engine: PMInterviewEngine,
        state: InterviewState,
        *,
        session_id: str,
        cwd: str,
        failure_message: str,
        status: str | None = None,
    ) -> Result[MCPToolResult, MCPServerError] | None:
        """Persist interview state and PM metadata, returning an MCP error on failure."""
        save_result = await engine.save_state(state)
        if isinstance(save_result, Result) and save_result.is_err:
            return Result.err(
                MCPToolError(
                    f"{failure_message}: {save_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )
        self._save_pm_session_meta(
            session_id,
            engine=engine,
            cwd=cwd,
            status=status,
        )
        return None

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a PM interview request."""
        action = arguments.get("action")

        try:
            if action:
                return await self._dispatch_action(action, arguments, get_agent_mode(self.agent_mode))

            return await self._handle_internal_compat(arguments)

        except Exception as e:
            log.error("pm_handler.unexpected_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"PM interview failed: {e}",
                    tool_name="ouroboros_pm_interview",
                )
            )

    async def _dispatch_action(
        self,
        action: str,
        arguments: dict[str, Any],
        effective_mode: AgentMode,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Dispatch an explicit PM action."""
        if action == "start":
            return await self._action_start(arguments)
        if action == "state":
            return await self._action_state(arguments)
        if action == "record_turn":
            return await self._action_record_turn(arguments)
        if action == "record":
            return await self._action_record(arguments)
        if action == "score":
            return await self._action_score(arguments)
        if action == "complete":
            return await self._action_complete(arguments)
        if action == "generate":
            session_id = arguments.get("session_id")
            if not session_id:
                return Result.err(
                    MCPToolError(
                        "session_id is required for action=generate",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            cwd = arguments.get("cwd") or os.getcwd()
            return await self._handle_generate(self._get_engine(), session_id, cwd)
        if action == "select_repos":
            selected_repos: list[str] | None = arguments.get("selected_repos")
            if selected_repos is None:
                return Result.err(
                    MCPToolError(
                        "selected_repos is required for action=select_repos",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            return await self._handle_select_repos(
                self._get_engine(),
                selected_repos,
                arguments.get("session_id"),
                arguments.get("initial_context"),
                arguments.get("cwd") or os.getcwd(),
            )
        if action == "resume":
            session_id = arguments.get("session_id")
            if not session_id:
                return Result.err(
                    MCPToolError(
                        "session_id is required for action=resume",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            cwd = arguments.get("cwd") or os.getcwd()
            if effective_mode == AgentMode.NATIVE:
                return await self._handle_answer_native(
                    self._get_engine(),
                    session_id,
                    arguments.get("answer"),
                    cwd,
                )
            return await self._handle_answer(
                self._get_engine(),
                session_id,
                arguments.get("answer"),
                cwd,
            )
        return Result.err(
            MCPToolError(
                f"Unknown action: {action}",
                tool_name="ouroboros_pm_interview",
            )
        )

    async def _handle_internal_compat(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Internal compatibility mode: full PM interview loop with internal LLM calls."""
        initial_context = arguments.get("initial_context")
        session_id = arguments.get("session_id")
        answer = arguments.get("answer")
        cwd = arguments.get("cwd") or os.getcwd()
        selected_repos: list[str] | None = arguments.get("selected_repos")
        action = _detect_action(arguments)
        engine = self._get_engine()

        if action == "generate" and session_id:
            return await self._handle_generate(engine, session_id, cwd)

        if action == "select_repos" and selected_repos is not None:
            return await self._handle_select_repos(
                engine,
                selected_repos,
                session_id,
                initial_context,
                cwd,
            )

        if action == "start" and initial_context:
            return await self._handle_start(
                engine,
                initial_context,
                cwd,
                selected_repos=selected_repos,
            )

        if action == "resume" and session_id:
            return await self._handle_answer(engine, session_id, answer, cwd)

        return Result.err(
            MCPToolError(
                "Must provide initial_context to start, or session_id to resume/generate",
                tool_name="ouroboros_pm_interview",
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Native action methods (state-only, no internal question generation)
    # ──────────────────────────────────────────────────────────────

    async def _action_start(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Start a PM interview session without generating the first question."""
        initial_context = arguments.get("initial_context")
        if not initial_context:
            return Result.err(
                MCPToolError(
                    "initial_context is required for action=start",
                    tool_name="ouroboros_pm_interview",
                )
            )

        cwd = arguments.get("cwd") or os.getcwd()
        selected_repos: list[str] | None = arguments.get("selected_repos")

        bf_result = await self._load_brownfield_repos(selected_repos)
        if bf_result.is_err:
            return bf_result
        brownfield_repos = bf_result.value or None

        store = self._get_interview_state_store()
        result = await store.start_interview(initial_context, cwd=cwd)
        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        state = result.value
        if brownfield_repos:
            state.is_brownfield = True
            state.codebase_paths = [
                {"path": repo["path"], "role": repo.get("role", "main")}
                for repo in brownfield_repos
            ]
            state.mark_updated()

        save_result = await store.save_state(state)
        if save_result.is_err:
            log.warning("pm_handler.native_start_save_failed", error=str(save_result.error))

        self._save_pm_session_meta(
            state.interview_id,
            cwd=cwd,
            status="interview_started",
            extra={
                "initial_context": initial_context,
                "brownfield_repos": brownfield_repos or [],
                "codebase_context": "",
            },
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview started. Session ID: {state.interview_id}\n\n"
                            "Ask the PM first. After the PM answers, persist the full turn "
                            "with action=record_turn."
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": state.interview_id,
                    "status": "interview_started",
                    "is_brownfield": state.is_brownfield,
                    "round_count": len(state.rounds),
                    "answered_rounds": _count_answered_rounds(state),
                    "deferred_count": 0,
                    "decide_later_count": 0,
                    "pending_reframe": None,
                },
            )
        )

    async def _action_state(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Return the current PM interview transcript and compact metadata."""
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required for action=state",
                    tool_name="ouroboros_pm_interview",
                )
            )

        store = self._get_interview_state_store()
        state, error = await self._load_state_or_error(store, session_id)
        if error is not None or state is None:
            return error

        meta_payload = self._load_pm_session_meta(session_id) or {}
        transcript_lines = [
            f"Session: {state.interview_id}",
            f"Status: {state.status}",
            f"Initial context: {state.initial_context}",
            "",
            "Transcript:",
        ]
        for round_ in state.rounds:
            transcript_lines.append(f"Q{round_.round_number}: {round_.question}")
            transcript_lines.append(
                f"A{round_.round_number}: {round_.user_response or '[pending]'}"
            )
            transcript_lines.append("")

        response_meta = {
            "session_id": state.interview_id,
            "status": state.status,
            "completed": state.is_complete,
            "is_brownfield": state.is_brownfield,
            "round_count": len(state.rounds),
            "answered_rounds": _count_answered_rounds(state),
            "ambiguity_score": state.ambiguity_score,
            "seed_ready": (
                state.ambiguity_score is not None and state.ambiguity_score <= AMBIGUITY_THRESHOLD
            ),
            "deferred_count": len(meta_payload.get("deferred_items", [])),
            "decide_later_count": len(meta_payload.get("decide_later_items", [])),
            "pending_reframe": meta_payload.get("pending_reframe"),
            "classifications": meta_payload.get("classifications", []),
        }
        if state.rounds and state.rounds[-1].user_response is None:
            response_meta.update(
                build_question_ui_meta(
                    state.rounds[-1].question,
                    title="PM Interview",
                )
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="\n".join(transcript_lines).rstrip(),
                    ),
                ),
                is_error=False,
                meta=response_meta,
            )
        )

    async def _action_record(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Record a PM question or answer in native mode."""
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required for action=record",
                    tool_name="ouroboros_pm_interview",
                )
            )

        record_type = arguments.get("type")
        if record_type not in {"question", "answer"}:
            return Result.err(
                MCPToolError(
                    "type is required for action=record and must be 'question' or 'answer'",
                    tool_name="ouroboros_pm_interview",
                )
            )

        store = self._get_interview_state_store()
        state, error = await self._load_state_or_error(store, session_id)
        if error is not None or state is None:
            return error

        engine = self._get_engine()
        pm_meta = self._restore_pm_session_meta(engine, session_id) or {}

        if record_type == "question":
            return await self._record_question_native(store, engine, state, pm_meta, arguments)
        return await self._record_answer_native(store, engine, state, pm_meta, arguments)

    async def _action_record_turn(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Record a complete PM question+answer turn in native mode."""

        session_id = arguments.get("session_id")
        question = arguments.get("question")
        answer = arguments.get("answer")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required for action=record_turn",
                    tool_name="ouroboros_pm_interview",
                )
            )
        if not question:
            return Result.err(
                MCPToolError(
                    "question is required for action=record_turn",
                    tool_name="ouroboros_pm_interview",
                )
            )
        if not answer:
            return Result.err(
                MCPToolError(
                    "answer is required for action=record_turn",
                    tool_name="ouroboros_pm_interview",
                )
            )

        store = self._get_interview_state_store()
        state, error = await self._load_state_or_error(store, session_id)
        if error is not None or state is None:
            return error

        if state.rounds and state.rounds[-1].user_response is None:
            return Result.err(
                MCPToolError(
                    "Last question is still pending. Finish the legacy pending question before "
                    "switching to action=record_turn.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        engine = self._get_engine()
        pm_meta = self._restore_pm_session_meta(engine, session_id) or {}

        ambiguity_score = arguments.get("ambiguity_score")
        if ambiguity_score is not None:
            try:
                state.store_ambiguity(score=float(ambiguity_score), breakdown={})
            except (TypeError, ValueError):
                log.warning("pm_handler.invalid_ambiguity_score", value=ambiguity_score)

        deferred_items = list(pm_meta.get("deferred_items", []))
        deferred_items.extend(arguments.get("deferred_this_round") or [])
        decide_later_items = list(pm_meta.get("decide_later_items", []))
        decide_later_items.extend(arguments.get("decide_later_this_round") or [])
        classifications = list(pm_meta.get("classifications", []))
        classification = arguments.get("classification")
        if classification:
            classifications.append(classification)

        original_question = arguments.get("original_question")
        pending_reframe = (
            {"reframed": question, "original": original_question}
            if original_question
            else None
        )
        merged_meta = {
            "initial_context": pm_meta.get("initial_context", state.initial_context),
            "brownfield_repos": pm_meta.get("brownfield_repos", []),
            "codebase_context": pm_meta.get("codebase_context", ""),
            "deferred_items": deferred_items,
            "decide_later_items": decide_later_items,
            "classifications": classifications,
            "pending_reframe": pending_reframe,
        }
        engine.restore_meta(merged_meta)

        record_result = await engine.record_response(state, answer, question)
        if record_result.is_err:
            return Result.err(
                MCPToolError(
                    str(record_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )
        state = record_result.value
        state.clear_stored_ambiguity()
        state.mark_updated()

        save_result = await store.save_state(state)
        if save_result.is_err:
            return Result.err(
                MCPToolError(
                    str(save_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        self._save_pm_session_meta(
            state.interview_id,
            engine=engine,
            cwd=(pm_meta.get("cwd") or ""),
            extra={
                "initial_context": merged_meta["initial_context"],
                "brownfield_repos": merged_meta["brownfield_repos"],
            },
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"PM turn recorded for session {state.interview_id}",
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": state.interview_id,
                    "round_number": len(state.rounds),
                    "answered_rounds": _count_answered_rounds(state),
                    "ambiguity_score": None,
                    "deferred_count": len(engine.deferred_items),
                    "decide_later_count": len(engine.decide_later_items),
                    "pending_reframe": engine.get_pending_reframe(),
                },
            )
        )

    async def _record_question_native(
        self,
        store: InterviewStateStore,
        engine: PMInterviewEngine,
        state: InterviewState,
        pm_meta: dict[str, Any],
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Record an agent-generated PM question into the state store."""
        question = arguments.get("question")
        if not question:
            return Result.err(
                MCPToolError(
                    "question is required for action=record, type=question",
                    tool_name="ouroboros_pm_interview",
                )
            )

        if state.rounds and state.rounds[-1].user_response is None:
            return Result.err(
                MCPToolError(
                    "Last question is still pending. Record an answer before adding a new question.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        ambiguity_score = arguments.get("ambiguity_score")
        if ambiguity_score is not None:
            try:
                state.store_ambiguity(score=float(ambiguity_score), breakdown={})
            except (TypeError, ValueError):
                log.warning("pm_handler.invalid_ambiguity_score", value=ambiguity_score)
        state.mark_updated()

        save_result = await store.save_state(state)
        if save_result.is_err:
            return Result.err(
                MCPToolError(
                    str(save_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        deferred_items = list(pm_meta.get("deferred_items", []))
        deferred_items.extend(arguments.get("deferred_this_round") or [])
        decide_later_items = list(pm_meta.get("decide_later_items", []))
        decide_later_items.extend(arguments.get("decide_later_this_round") or [])
        classifications = list(pm_meta.get("classifications", []))
        classification = arguments.get("classification")
        if classification:
            classifications.append(classification)

        original_question = arguments.get("original_question")
        pending_reframe = (
            {"reframed": question, "original": original_question}
            if original_question
            else None
        )

        self._save_pm_session_meta(
            state.interview_id,
            engine=engine,
            cwd=(pm_meta.get("cwd") or ""),
            extra={
                "initial_context": pm_meta.get("initial_context", state.initial_context),
                "brownfield_repos": pm_meta.get("brownfield_repos", []),
                "deferred_items": deferred_items,
                "decide_later_items": decide_later_items,
                "classifications": classifications,
                "pending_reframe": pending_reframe,
            },
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"PM question recorded for session {state.interview_id}",
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": state.interview_id,
                    "round_number": len(state.rounds),
                    "ambiguity_score": state.ambiguity_score,
                    "deferred_count": len(deferred_items),
                    "decide_later_count": len(decide_later_items),
                    "pending_reframe": pending_reframe,
                },
            )
        )

    async def _record_answer_native(
        self,
        store: InterviewStateStore,
        engine: PMInterviewEngine,
        state: InterviewState,
        pm_meta: dict[str, Any],
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Record a user's answer to the pending PM question."""
        answer = arguments.get("answer")
        if not answer:
            return Result.err(
                MCPToolError(
                    "answer is required for action=record, type=answer",
                    tool_name="ouroboros_pm_interview",
                )
            )

        if not state.rounds:
            return Result.err(
                MCPToolError(
                    "Cannot record answer - no questions have been asked yet",
                    tool_name="ouroboros_pm_interview",
                )
            )

        last_round = state.rounds[-1]
        if last_round.user_response is not None:
            return Result.err(
                MCPToolError(
                    "Last question already has an answer. Record a new question first.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        # Preserve the PM-facing question so PMInterviewEngine can bundle reframed context.
        state.rounds.pop()
        record_result = await engine.record_response(state, answer, last_round.question)
        if record_result.is_err:
            return Result.err(
                MCPToolError(
                    str(record_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )
        state = record_result.value
        state.clear_stored_ambiguity()
        state.mark_updated()

        save_result = await store.save_state(state)
        if save_result.is_err:
            return Result.err(
                MCPToolError(
                    str(save_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        self._save_pm_session_meta(
            state.interview_id,
            engine=engine,
            cwd=(pm_meta.get("cwd") or ""),
            extra={
                "initial_context": pm_meta.get("initial_context", state.initial_context),
                "brownfield_repos": pm_meta.get("brownfield_repos", []),
            },
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"PM answer recorded for session {state.interview_id}",
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": state.interview_id,
                    "round_number": len(state.rounds),
                    "answered_rounds": _count_answered_rounds(state),
                    "ambiguity_score": None,
                },
            )
        )

    async def _action_score(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Persist PM ambiguity score from native subagent component scores."""
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required for action=score",
                    tool_name="ouroboros_pm_interview",
                )
            )

        try:
            goal = float(arguments.get("goal_clarity", 0.0))
            constraint = float(arguments.get("constraint_clarity", 0.0))
            success = float(arguments.get("success_criteria_clarity", 0.0))
        except (TypeError, ValueError) as exc:
            return Result.err(
                MCPToolError(
                    f"Invalid component score: {exc}",
                    tool_name="ouroboros_pm_interview",
                )
            )

        is_brownfield = bool(arguments.get("is_brownfield", False))
        context_raw = arguments.get("context_clarity")
        if is_brownfield and context_raw is not None:
            try:
                context = float(context_raw)
            except (TypeError, ValueError):
                context = 0.0
            clarity = goal * 0.35 + constraint * 0.25 + success * 0.25 + context * 0.15
        else:
            clarity = goal * 0.40 + constraint * 0.30 + success * 0.30

        score = round(1.0 - clarity, 4)
        score = max(0.0, min(1.0, score))
        seed_ready = score <= AMBIGUITY_THRESHOLD

        store = self._get_interview_state_store()
        state, error = await self._load_state_or_error(store, session_id)
        if error is not None or state is None:
            return error

        state.store_ambiguity(score=score, breakdown={})
        state.mark_updated()

        save_result = await store.save_state(state)
        if save_result.is_err:
            log.warning("pm_handler.score_save_failed", error=str(save_result.error))

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"{score:.2f} seed_ready:{str(seed_ready).lower()}",
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "ambiguity_score": score,
                    "seed_ready": seed_ready,
                },
            )
        )

    async def _action_complete(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Complete the PM interview if the stored ambiguity gate passes."""
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required for action=complete",
                    tool_name="ouroboros_pm_interview",
                )
            )

        store = self._get_interview_state_store()
        state, error = await self._load_state_or_error(store, session_id)
        if error is not None or state is None:
            return error

        ambiguity_score_value = arguments.get("ambiguity_score")
        if ambiguity_score_value is not None:
            try:
                state.store_ambiguity(score=float(ambiguity_score_value), breakdown={})
            except (TypeError, ValueError) as exc:
                return Result.err(
                    MCPToolError(
                        f"Invalid ambiguity_score: {exc}",
                        tool_name="ouroboros_pm_interview",
                    )
                )

        if state.ambiguity_score is None:
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                "Cannot complete - no ambiguity score available. "
                                "Please persist a score with action=score first."
                            ),
                        ),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "ambiguity_score": None,
                        "seed_ready": False,
                    },
                )
            )

        if state.ambiguity_score > AMBIGUITY_THRESHOLD:
            return self._ambiguity_gate_response(session_id, state.ambiguity_score)

        complete_result = await store.complete_interview(state)
        if complete_result.is_err:
            return Result.err(
                MCPToolError(
                    str(complete_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )
        state = complete_result.value
        save_result = await store.save_state(state)
        if save_result.is_err:
            log.warning("pm_handler.complete_save_failed", error=str(save_result.error))

        pm_meta = self._load_pm_session_meta(session_id) or {}
        self._save_pm_session_meta(
            session_id,
            cwd=(pm_meta.get("cwd") or ""),
            status="completed",
            extra={
                "initial_context": pm_meta.get("initial_context", state.initial_context),
                "brownfield_repos": pm_meta.get("brownfield_repos", []),
                "deferred_items": pm_meta.get("deferred_items", []),
                "decide_later_items": pm_meta.get("decide_later_items", []),
                "classifications": pm_meta.get("classifications", []),
                "pending_reframe": None,
            },
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview completed. Session ID: {session_id}\n\n"
                            f"Ambiguity: {state.ambiguity_score:.2f}\n"
                            f"Answered rounds: {_count_answered_rounds(state)}\n"
                            f'Generate a PM with: action="generate", session_id="{session_id}"'
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "completed": True,
                    "seed_ready": True,
                    "ambiguity_score": state.ambiguity_score,
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Start
    # ──────────────────────────────────────────────────────────────

    async def _handle_start(
        self,
        engine: PMInterviewEngine,
        initial_context: str,
        cwd: str,
        *,
        selected_repos: list[str] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Start a new PM interview session.

        Automatically loads is_default=true repos from DB as brownfield
        context. No user selection needed — repo defaults are managed
        via ``ooo setup``.

        If ``selected_repos`` is provided, uses those instead (backward compat).
        """
        bf_result = await self._load_brownfield_repos(selected_repos)
        if bf_result.is_err:
            return bf_result
        brownfield_repos = bf_result.value or None

        result = await engine.ask_opening_and_start(
            user_response=initial_context,
            brownfield_repos=brownfield_repos,
        )
        if result.is_err:
            return Result.err(MCPToolError(str(result.error), tool_name="ouroboros_pm_interview"))

        state = result.value

        # Snapshot before asking first question
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

        question_result = await engine.ask_next_question(state)
        if question_result.is_err:
            return Result.err(
                MCPToolError(
                    str(question_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        question = question_result.value

        # Compute diff
        diff = engine.compute_deferred_diff(deferred_before, decide_later_before)

        # Record unanswered round
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        # Persist — check save result to avoid handing back a session that wasn't written
        save_result = await engine.save_state(state)
        if isinstance(save_result, Result) and save_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Failed to persist interview state: {save_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )
        self._save_pm_session_meta(
            state.interview_id,
            engine=engine,
            cwd=cwd,
            status="interview_started",
        )

        # Include pending_reframe in response meta if a reframe occurred
        pending_reframe = engine.get_pending_reframe()

        # Check classification to signal skip eligibility
        classification = _last_classification(engine)
        is_decide_later = classification == "decide_later"
        is_deferred = classification == "deferred"
        skip_eligible = is_decide_later or is_deferred

        meta = {
            "session_id": state.interview_id,
            "status": "interview_started",
            "is_brownfield": state.is_brownfield,
            "classification": classification,
            "skip_eligible": skip_eligible,
            "pending_reframe": pending_reframe,
            **diff,
            **build_question_ui_meta(
                question,
                title="PM Interview",
            ),
        }

        log.info(
            "pm_handler.started",
            session_id=state.interview_id,
            is_brownfield=state.is_brownfield,
            classification=classification,
            skip_eligible=skip_eligible,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        # Build response text — include skip hint when applicable
        start_text = f"PM interview started. Session ID: {state.interview_id}\n\n{question}"
        if is_decide_later:
            start_text += (
                "\n\n💡 This question can be deferred. "
                'The user may answer now, or choose "decide later" to skip it. '
                "If they choose to decide later, pass "
                f'answer="[decide_later]" with session_id="{state.interview_id}".'
            )
        elif is_deferred:
            start_text += (
                "\n\n💡 This is a technical question that can be deferred to the dev phase. "
                "The user may answer now, or choose to defer it. "
                "If they choose to defer, pass "
                f'answer="[deferred]" with session_id="{state.interview_id}".'
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=start_text,
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Brownfield repo helpers
    # ──────────────────────────────────────────────────────────────

    async def _load_brownfield_repos(
        self,
        selected_repos: list[str] | None,
    ) -> Result[list[dict], MCPServerError]:
        """Resolve selected_repos to a list of repo dicts for session initialization.

        - Non-empty list → resolve from DB; error if none found.
        - None → auto-load defaults (empty defaults → greenfield is OK).
        - Empty list → greenfield, return empty list.
        """
        if selected_repos is not None and len(selected_repos) > 0:
            resolved = await self._resolve_repos_from_db(selected_repos)
            if not resolved:
                return Result.err(
                    MCPToolError(
                        f"None of the selected repos could be resolved: {selected_repos}. "
                        "Register them first via 'ouroboros setup scan' or the brownfield tool.",
                        tool_name="ouroboros_pm_interview",
                    )
                )
        elif selected_repos is None:
            resolved = await self._query_default_repos()
        else:
            resolved = []

        if not resolved:
            return Result.ok([])

        repo_dicts = [
            {
                "path": r.path,
                "name": r.name,
                "role": "main",
                **({"desc": r.desc} if r.desc else {}),
            }
            for r in resolved
        ]
        log.info(
            "pm_handler.brownfield_repos_loaded",
            count=len(resolved),
            paths=[r.path for r in resolved],
        )
        return Result.ok(repo_dicts)

    async def _query_default_repos(self) -> list[BrownfieldRepo]:
        """Query DB for is_default=true repos."""
        try:
            store = BrownfieldStore()
            await store.initialize()
            try:
                return list(await store.get_defaults())
            finally:
                await store.close()
        except Exception as exc:
            log.warning("pm_handler.query_defaults_failed", error=str(exc))
            return []

    async def _query_all_repos(self) -> list[BrownfieldRepo]:
        """Query DB for all registered brownfield repos."""
        try:
            store = BrownfieldStore()
            await store.initialize()
            try:
                return await store.list()
            finally:
                await store.close()
        except Exception as exc:
            log.warning("pm_handler.query_repos_failed", error=str(exc))
            return []

    async def _resolve_repos_from_db(
        self,
        paths: list[str],
    ) -> list[BrownfieldRepo]:
        """Look up selected paths in the DB, returning only those that exist.

        Paths that are not registered in the brownfield_repos table are
        silently ignored.  If *all* paths are missing the caller should
        treat the session as greenfield.

        Args:
            paths: List of absolute filesystem paths chosen by the user.

        Returns:
            List of :class:`BrownfieldRepo` instances for paths found in DB,
            preserving the order of *paths*.
        """
        all_repos = await self._query_all_repos()
        repo_by_path: dict[str, BrownfieldRepo] = {r.path: r for r in all_repos}

        resolved: list[BrownfieldRepo] = []
        for p in paths:
            repo = repo_by_path.get(p)
            if repo is not None:
                resolved.append(repo)
            else:
                log.warning(
                    "pm_handler.resolve_repos.path_not_in_db",
                    path=p,
                )
        return resolved

    # ──────────────────────────────────────────────────────────────
    # Step 2: select_repos (AC 4)
    # ──────────────────────────────────────────────────────────────

    async def _handle_select_repos(
        self,
        engine: PMInterviewEngine,
        selected_repos: list[str],
        session_id: str | None,
        initial_context: str | None,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle step 2 of the 2-step start: user has selected repos.

        Backward compat: if ``initial_context`` is provided alongside
        ``selected_repos``, behave identically to the old 1-step flow
        (no pm_meta lookup needed).

        Otherwise, ``session_id`` is required to recover the saved
        ``initial_context`` from pm_meta written during step 1.
        """
        # ── Backward-compat 1-step: both selected_repos + initial_context ──
        if initial_context:
            return await self._handle_start(
                engine,
                initial_context,
                cwd,
                selected_repos=selected_repos,
            )

        # ── 2-step: recover initial_context from pm_meta ──────────────
        if not session_id:
            return Result.err(
                MCPToolError(
                    "select_repos requires session_id (from step 1) "
                    "or initial_context for 1-step start",
                    tool_name="ouroboros_pm_interview",
                )
            )

        meta = self._load_pm_session_meta(session_id)
        if meta is None:
            return Result.err(
                MCPToolError(
                    f"No pm_meta found for session {session_id}. "
                    "The session may have expired or never been created.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        # ── Idempotency (AC 9): session already started ──────────
        # If select_repos is called again on an already-started session,
        # return the first question from InterviewState instead of
        # re-starting the interview.
        if meta.get("status") == "interview_started":
            return await self._idempotent_select_repos(engine, session_id, meta)

        saved_context = meta.get("initial_context", "")
        if not saved_context:
            return Result.err(
                MCPToolError(
                    f"pm_meta for {session_id} has no initial_context. "
                    "Cannot proceed with repo selection.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        log.info(
            "pm_handler.select_repos.step2",
            session_id=session_id,
            repo_count=len(selected_repos),
        )

        # Do NOT update global DB defaults — PM interview selection is session-scoped
        return await self._handle_start(
            engine,
            saved_context,
            cwd,
            selected_repos=selected_repos,
        )

    # ──────────────────────────────────────────────────────────────
    # Idempotency guard (AC 9)
    # ──────────────────────────────────────────────────────────────

    async def _idempotent_select_repos(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        meta: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Return the first question when select_repos is called on an already-started session.

        This handles the case where the caller sends ``select_repos`` more
        than once for the same session.  Instead of re-starting the
        interview (which would create duplicate state), we load the existing
        ``InterviewState`` and replay the first question from its rounds.
        """
        log.info(
            "pm_handler.select_repos.idempotent",
            session_id=session_id,
        )

        state, error = await self._load_state_or_error(engine, session_id)
        if error is not None or state is None:
            return Result.err(
                MCPToolError(
                    f"Session {session_id} is marked as started but state could not be loaded",
                    tool_name="ouroboros_pm_interview",
                )
            )
        # Return the last unanswered round's question (the pending PM-facing prompt),
        # not rounds[0] which may be a hidden auto-deferred/auto-decided question.
        pending = next(
            (r for r in reversed(state.rounds) if r.user_response is None),
            None,
        )
        first_question = (
            pending.question
            if pending
            else (state.rounds[-1].question if state.rounds else "No question available.")
        )

        engine.restore_meta(meta)
        classification = _last_classification(engine)
        is_decide_later = classification == "decide_later"
        is_deferred = classification == "deferred"
        skip_eligible = is_decide_later or is_deferred

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview started. Session ID: {session_id}\n\n{first_question}"
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "status": "interview_started",
                    "is_brownfield": state.is_brownfield,
                    "idempotent": True,
                    "classification": classification,
                    "skip_eligible": skip_eligible,
                    **build_question_ui_meta(
                        first_question,
                        title="PM Interview",
                    ),
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Answer — native mode (state-only, no LLM question generation)
    # ──────────────────────────────────────────────────────────────

    async def _handle_answer_native(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        answer: str | None,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Record answer and return state — no LLM call.

        The platform-native agent handles question generation.
        """
        state, error = await self._load_state_and_restore_meta_or_error(engine, session_id)
        if error is not None or state is None:
            return error

        # Record answer if provided
        if answer and state.rounds:
            last_question = state.rounds[-1].question
            if state.rounds[-1].user_response is None:
                state.rounds.pop()
            record_result = await engine.record_response(state, answer, last_question)
            if record_result.is_err:
                return Result.err(
                    MCPToolError(str(record_result.error), tool_name="ouroboros_pm_interview")
                )
            state = record_result.value
            state.clear_stored_ambiguity()

        # Save state
        persist_error = await self._persist_state_and_meta_or_error(
            engine,
            state,
            session_id=session_id,
            cwd=cwd,
            failure_message="Failed to save",
        )
        if persist_error is not None:
            return persist_error

        # Completion check
        completion = await engine.check_completion(state)

        # Return state for agent to generate next question
        rounds_summary = [
            {"round": r.round_number, "question": r.question, "answer": r.user_response}
            for r in state.rounds
        ]

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=json.dumps({
                            "session_id": session_id,
                            "rounds": rounds_summary,
                            "is_complete": completion is not None,
                            "completion": completion,
                            "deferred_count": len(engine.deferred_items),
                            "decide_later_count": len(engine.decide_later_items),
                        }, ensure_ascii=False, indent=2),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "agent_mode": "native",
                    "is_complete": completion is not None,
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Answer (resume + record) — internal mode with LLM
    # ──────────────────────────────────────────────────────────────

    async def _handle_answer(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        answer: str | None,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Resume session, record an answer, check completion, then ask next question.

        Completion is determined by the engine's ambiguity score dropping
        below the threshold (requirements are clear).  User controls when
        to stop.
        """
        state, error = await self._load_state_and_restore_meta_or_error(engine, session_id)
        if error is not None or state is None:
            return error

        # If no answer provided, re-display the pending question (retry/reconnect)
        if not answer and state.rounds and state.rounds[-1].user_response is None:
            pending_question = state.rounds[-1].question
            classification = _last_classification(engine)
            is_decide_later = classification == "decide_later"
            is_deferred = classification == "deferred"
            skip_eligible = is_decide_later or is_deferred

            pending_reframe = engine.get_pending_reframe()

            # Include skip hint in re-displayed question
            pending_text = f"Session {session_id}\n\n{pending_question}"
            if is_decide_later:
                pending_text += (
                    "\n\n💡 This question can be deferred. "
                    'The user may answer now, or choose "decide later" to skip it. '
                    "If they choose to decide later, pass "
                    f'answer="[decide_later]" with session_id="{session_id}".'
                )
            elif is_deferred:
                pending_text += (
                    "\n\n💡 This is a technical question that can be deferred to the dev phase. "
                    "The user may answer now, or choose to defer it. "
                    "If they choose to defer, pass "
                    f'answer="[deferred]" with session_id="{session_id}".'
                )

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=pending_text,
                        ),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "is_complete": False,
                        "classification": classification,
                        "skip_eligible": skip_eligible,
                        "deferred_this_round": [],
                        "decide_later_this_round": [],
                        "interview_complete": False,
                        "pending_reframe": pending_reframe,
                        "new_deferred": [],
                        "new_decide_later": [],
                        "deferred_count": 0,
                        "decide_later_count": len(engine.deferred_items)
                        + len(engine.decide_later_items),
                        **build_question_ui_meta(
                            pending_question,
                            title="PM Interview",
                        ),
                    },
                )
            )

        # ── Per-round diff snapshot — must be BEFORE any skip/record call ──
        # Snapshot list lengths here so that items appended inside
        # skip_as_decide_later() / skip_as_deferred() are captured in the
        # per-round diff returned at the end of this call.
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

        # Record answer if provided
        if answer and not state.rounds:
            return Result.err(
                MCPToolError(
                    "Cannot record answer: no questions have been asked yet.",
                    tool_name="ouroboros_pm_interview",
                )
            )
        if answer and state.rounds:
            last_question = state.rounds[-1].question
            if state.rounds[-1].user_response is None:
                state.rounds.pop()

            # ── User chose to skip (decide later / defer to dev) ───
            # The main session detects classification via response_meta
            # and offers skip options.  The user's choice arrives as:
            #   answer="[decide_later]" → skip_as_decide_later()
            #   answer="[deferred]"     → skip_as_deferred()
            # Guard: only honour the sentinel when the last question was
            # actually classified as that type.  If a client sends
            # "[decide_later]" for a passthrough/reframed question, treat
            # it as a normal answer so no data is silently discarded.
            stripped = answer.strip()
            last_classification = _last_classification(engine)
            if stripped == "[decide_later]" and last_classification == "decide_later":
                skip_result = await engine.skip_as_decide_later(state, last_question)
                if skip_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(skip_result.error),
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                state = skip_result.value
                state.clear_stored_ambiguity()
            elif stripped == "[deferred]" and last_classification == "deferred":
                skip_result = await engine.skip_as_deferred(state, last_question)
                if skip_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(skip_result.error),
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                state = skip_result.value
                state.clear_stored_ambiguity()
            else:
                record_result = await engine.record_response(state, answer, last_question)
                if record_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(record_result.error),
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                state = record_result.value
                state.clear_stored_ambiguity()

        # ── Completion check (AC 12) ─────────────────────────────
        # Completion is determined by engine ambiguity scoring.
        # When complete, auto-generate the PM document immediately
        # (no separate "generate" call needed from the skill).
        completion_result = await maybe_complete_pm_interview(state, engine)
        if completion_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Failed to complete interview: {completion_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )

        state, completion = completion_result.value
        if completion is not None:
            persist_error = await self._persist_state_and_meta_or_error(
                engine,
                state,
                session_id=session_id,
                cwd=cwd,
                failure_message="Failed to persist completed state",
            )
            if persist_error is not None:
                return persist_error

            log.info(
                "pm_handler.interview_complete",
                session_id=session_id,
                **completion,
            )

            # Auto-generate PM document on completion
            seed_result = await engine.generate_pm_seed(state)
            if seed_result.is_err:
                # Generation failed — still report completion but without document
                summary_text = (
                    f"Interview complete but PM generation failed: {seed_result.error}\n"
                    f"Session ID: {session_id}\n"
                    f'Retry with: action="generate", session_id="{session_id}"'
                )
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text=summary_text),),
                        is_error=False,
                        meta={
                            "session_id": session_id,
                            "is_complete": True,
                            "generation_failed": True,
                            **completion,
                        },
                    )
                )

            seed = seed_result.value
            try:
                seed_path = engine.save_pm_seed(seed)
                pm_output_dir = Path(cwd) / ".ouroboros"
                pm_path = save_pm_document(seed, output_dir=pm_output_dir)
            except Exception as e:
                log.error("pm_handler.save_failed", error=str(e), session_id=session_id)
                summary_text = (
                    f"Interview complete but saving PM artifacts failed: {e}\n"
                    f"Session ID: {session_id}\n"
                    f'Retry with: action="generate", session_id="{session_id}"'
                )
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text=summary_text),),
                        is_error=False,
                        meta={
                            "session_id": session_id,
                            "is_complete": True,
                            "generation_failed": True,
                            **completion,
                        },
                    )
                )

            decide_later_summary = engine.format_decide_later_summary()
            summary_text = build_pm_completion_summary(
                session_id=session_id,
                completion=completion,
                stored_ambiguity_score=getattr(state, "ambiguity_score", None),
                deferred_count=0,
                decide_later_count=len(engine.deferred_items) + len(engine.decide_later_items),
                decide_later_summary=decide_later_summary,
            )
            summary_text += f"\n\nPM document: {pm_path}\nSeed: {seed_path}"

            response_meta = {
                "session_id": session_id,
                "question": None,
                "is_complete": True,
                "classification": engine.get_last_classification(),
                "deferred_this_round": [],
                "decide_later_this_round": [],
                **completion,
                "deferred_count": 0,
                "decide_later_count": len(engine.deferred_items) + len(engine.decide_later_items),
                "seed_path": str(seed_path),
                "pm_path": str(pm_path),
            }

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=summary_text,
                        ),
                    ),
                    is_error=False,
                    meta=response_meta,
                )
            )

        question_result = await engine.ask_next_question(state)
        if question_result.is_err:
            error_msg = str(question_result.error)
            if "empty response" in error_msg.lower():
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=(
                                    f"Question generation failed. "
                                    f"Session ID: {session_id}\n\n"
                                    f'Resume with: session_id="{session_id}"'
                                ),
                            ),
                        ),
                        is_error=True,
                        meta={"session_id": session_id, "recoverable": True},
                    )
                )
            return Result.err(MCPToolError(error_msg, tool_name="ouroboros_pm_interview"))

        question = question_result.value

        # Compute diff AFTER ask_next_question — new items are the
        # slice from the pre-snapshot length to current length
        diff = engine.compute_deferred_diff(deferred_before, decide_later_before)

        # Save unanswered round
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        persist_error = await self._persist_state_and_meta_or_error(
            engine,
            state,
            session_id=session_id,
            cwd=cwd,
            failure_message="Failed to persist resume state",
        )
        if persist_error is not None:
            return persist_error

        # Include pending_reframe in response meta if a new reframe occurred
        pending_reframe = engine.get_pending_reframe()

        # Extract classification from the last classify call
        classification = engine.get_last_classification()

        # Signal to the caller that the user can skip this question
        is_decide_later = classification == "decide_later"
        is_deferred = classification == "deferred"
        skip_eligible = is_decide_later or is_deferred

        response_meta = {
            "session_id": session_id,
            "is_complete": False,
            "classification": classification,
            "skip_eligible": skip_eligible,
            "deferred_this_round": diff["new_deferred"],
            "decide_later_this_round": diff["new_decide_later"],
            # Keep backward-compat fields from AC 8
            "interview_complete": False,
            "pending_reframe": pending_reframe,
            **diff,
            **build_question_ui_meta(
                question,
                title="PM Interview",
            ),
        }

        log.info(
            "pm_handler.question_asked",
            session_id=session_id,
            classification=classification,
            skip_eligible=skip_eligible,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        # Build response text — include skip hint when applicable
        response_text = f"Session {session_id}\n\n{question}"
        if is_decide_later:
            response_text += (
                "\n\n💡 This question can be deferred. "
                'The user may answer now, or choose "decide later" to skip it. '
                "If they choose to decide later, pass "
                f'answer="[decide_later]" with session_id="{session_id}".'
            )
        elif is_deferred:
            response_text += (
                "\n\n💡 This is a technical question that can be deferred to the dev phase. "
                "The user may answer now, or choose to defer it. "
                "If they choose to defer, pass "
                f'answer="[deferred]" with session_id="{session_id}".'
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=response_text,
                    ),
                ),
                is_error=False,
                meta=response_meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Generate PM seed
    # ──────────────────────────────────────────────────────────────

    async def _handle_generate(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Generate PM seed from completed interview (path-idempotent).

        Loads InterviewState and pm_meta, restores engine via restore_meta(),
        runs generate_pm_seed, saves PM seed to ~/.ouroboros/seeds/ and
        pm.md to {cwd}/.ouroboros/.

        Path-idempotent: file paths are deterministic for a given session_id
        (seed → ``pm_seed_{interview_id}.json``, document → ``pm.md``).
        Content timestamps (created_at, Generated header) may differ on retry.

        Rejects incomplete interviews with an error to prevent partial-spec
        artifacts from being generated.
        """
        state, error = await self._load_state_and_restore_meta_or_error(engine, session_id)
        if error is not None or state is None:
            return error

        # Guard: reject incomplete interviews
        if not state.is_complete:
            return Result.err(
                MCPToolError(
                    f"Interview '{session_id}' is not complete. "
                    "Finish the interview before generating a PM document.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        seed_result = await engine.generate_pm_seed(state)
        if seed_result.is_err:
            return Result.err(
                MCPToolError(
                    str(seed_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        seed = seed_result.value

        # Save seed to ~/.ouroboros/seeds/ (idempotent — overwrites on retry)
        # Save seed and PM document with recovery contract
        try:
            seed_path = engine.save_pm_seed(seed)
            pm_output_dir = Path(cwd) / ".ouroboros"
            pm_path = save_pm_document(seed, output_dir=pm_output_dir)
        except Exception as e:
            log.error("pm_handler.generate_save_failed", error=str(e), session_id=session_id)
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                f"PM generation succeeded but saving artifacts failed: {e}\n"
                                f"Session ID: {session_id}\n"
                                f'Retry with: action="generate", session_id="{session_id}"'
                            ),
                        ),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "is_complete": True,
                        "generation_failed": True,
                    },
                )
            )

        next_step = build_pm_dev_handoff_next_step(seed_path)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM seed generated: {seed.product_name}\n"
                            f"PM seed: {seed_path}\n"
                            f"PM document: {pm_path}\n\n"
                            "This PM seed is a handoff artifact for the dev interview, "
                            "not the runnable Seed.\n"
                            f"Decide-later items: {len(seed.decide_later_items)}\n"
                            f"Next: {next_step}"
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "seed_path": str(seed_path),
                    "pm_seed_path": str(seed_path),
                    "pm_path": str(pm_path),
                    "artifact_kind": "pm_seed",
                    "runnable": False,
                    "next_step": next_step,
                },
            )
        )
