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
reaching the maximum round limit.  There is no user "done" signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewRound,
    InterviewState,
)
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore

log = structlog.get_logger()

# Hard cap on interview rounds in MCP mode.  The engine's ambiguity scorer
# should trigger completion well before this, but this prevents runaway loops.
MAX_PM_INTERVIEW_ROUNDS = 20

_DATA_DIR = Path.home() / ".ouroboros" / "data"


def _meta_path(session_id: str, data_dir: Path | None = None) -> Path:
    """Return the path to the pm_meta JSON file for a session."""
    base = data_dir or _DATA_DIR
    return base / f"pm_meta_{session_id}.json"


def _save_pm_meta(
    session_id: str,
    engine: PMInterviewEngine | None = None,
    cwd: str = "",
    data_dir: Path | None = None,
    *,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Persist PM-specific metadata that isn't in InterviewState.

    Fields:
        deferred_items: list[str]
        decide_later_items: list[str]
        codebase_context: str
        pending_reframe: dict | None
        cwd: str
        status: str | None  — e.g. "interview_started"
    """
    # Engine may be None when saving before interview start
    if engine is not None:
        reframe_map = engine._reframe_map
        # pending_reframe: single {reframed, original} object or None
        pending_reframe: dict[str, str] | None = None
        if reframe_map:
            # Take the most recent entry (last inserted)
            reframed = next(reversed(reframe_map))
            pending_reframe = {
                "reframed": reframed,
                "original": reframe_map[reframed],
            }

        meta: dict[str, Any] = {
            "deferred_items": list(engine.deferred_items),
            "decide_later_items": list(engine.decide_later_items),
            "codebase_context": engine.codebase_context,
            "pending_reframe": pending_reframe,
            "cwd": cwd,
            "brownfield_repos": list(getattr(engine, "_selected_brownfield_repos", [])),
            "classifications": [
                c.output_type.value for c in getattr(engine, "classifications", [])
            ],
        }
    else:
        meta = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
            "cwd": cwd,
            "brownfield_repos": [],
            "classifications": [],
        }

    if status is not None:
        meta["status"] = status

    if extra:
        meta.update(extra)

    path = _meta_path(session_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debug("pm_handler.meta_saved", session_id=session_id, path=str(path))


def _load_pm_meta(
    session_id: str,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Load PM-specific metadata from disk.  Returns None if not found."""
    path = _meta_path(session_id, data_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("pm_handler.meta_load_failed", error=str(exc))
        return None


def _restore_engine_meta(engine: PMInterviewEngine, meta: dict[str, Any]) -> None:
    """Restore PM-specific state into an engine from loaded meta."""
    engine.deferred_items = list(meta.get("deferred_items", []))
    engine.decide_later_items = list(meta.get("decide_later_items", []))
    engine.codebase_context = meta.get("codebase_context", "")
    engine._selected_brownfield_repos = list(meta.get("brownfield_repos", []))
    # Restore the reframe map from pending_reframe if present
    pending = meta.get("pending_reframe")
    if pending and isinstance(pending, dict):
        engine._reframe_map[pending["reframed"]] = pending["original"]


def _last_classification(engine: PMInterviewEngine) -> str | None:
    """Return the output_type string of the engine's last classification, or None."""
    if engine.classifications:
        return engine.classifications[-1].output_type.value
    return None


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


def _compute_deferred_diff(
    engine: PMInterviewEngine,
    deferred_len_before: int,
    decide_later_len_before: int,
) -> dict[str, Any]:
    """Compute the diff of deferred/decide-later items after ask_next_question.

    Compares list lengths before and after the call to determine which
    new items were added during classification.  Returns a dict with:
        new_deferred: list of newly deferred question texts
        new_decide_later: list of newly decide-later question texts
        deferred_count: total deferred items
        decide_later_count: total decide-later items

    This is the core diff computation for AC 8.
    """
    new_deferred = engine.deferred_items[deferred_len_before:]
    new_decide_later = engine.decide_later_items[decide_later_len_before:]

    return {
        "new_deferred": list(new_deferred),
        "new_decide_later": list(new_decide_later),
        "deferred_count": len(engine.deferred_items),
        "decide_later_count": len(engine.decide_later_items),
    }


async def _check_completion(
    state: InterviewState,
    engine: PMInterviewEngine,
) -> dict[str, Any] | None:
    """Check whether the interview should complete based on ambiguity or rounds.

    Completion is determined by two signals (no user "done" signal):

    1. **Ambiguity score** — after at least ``MIN_ROUNDS_BEFORE_EARLY_EXIT``
       answered rounds, the scorer evaluates requirement clarity.  If the score
       is ≤ ``AMBIGUITY_THRESHOLD`` (0.2) the interview is ready for PM
       generation.

    2. **Max-rounds safety cap** — after ``MAX_PM_INTERVIEW_ROUNDS`` rounds
       the interview is force-completed to prevent runaway loops.

    Returns a dict with completion metadata if the interview should end,
    or ``None`` if the interview should continue.
    """
    # Count only answered rounds (exclude the pending unanswered round)
    answered_rounds = sum(1 for r in state.rounds if r.user_response is not None)

    # ── Max-rounds hard cap ────────────────────────────────────────
    if answered_rounds >= MAX_PM_INTERVIEW_ROUNDS:
        log.info(
            "pm_handler.completion.max_rounds",
            session_id=state.interview_id,
            rounds=answered_rounds,
        )
        return {
            "interview_complete": True,
            "completion_reason": "max_rounds",
            "rounds_completed": answered_rounds,
            "ambiguity_score": None,
        }

    # ── Ambiguity check (only after minimum rounds) ────────────────
    if answered_rounds < MIN_ROUNDS_BEFORE_EARLY_EXIT:
        return None

    try:
        # Build additional context for scorer: decide-later items are
        # intentional deferrals that should not penalise clarity.
        additional_context = ""
        if engine.decide_later_items:
            additional_context = "Decide-later items (intentional deferrals):\n"
            additional_context += "\n".join(f"- {item}" for item in engine.decide_later_items)

        scorer = AmbiguityScorer(
            llm_adapter=engine.llm_adapter,
            model=engine.model,
        )
        score_result = await scorer.score(
            state,
            is_brownfield=state.is_brownfield,
            additional_context=additional_context,
        )

        if score_result.is_err:
            log.warning(
                "pm_handler.completion.scoring_failed",
                session_id=state.interview_id,
                error=str(score_result.error),
            )
            # Scoring failed — continue the interview rather than blocking
            return None

        ambiguity = score_result.value

        # Persist score on state for downstream use
        state.store_ambiguity(
            score=ambiguity.overall_score,
            breakdown=ambiguity.breakdown.model_dump(mode="json"),
        )

        if ambiguity.is_ready_for_seed:
            log.info(
                "pm_handler.completion.ambiguity_resolved",
                session_id=state.interview_id,
                ambiguity_score=ambiguity.overall_score,
                rounds=answered_rounds,
            )
            return {
                "interview_complete": True,
                "completion_reason": "ambiguity_resolved",
                "rounds_completed": answered_rounds,
                "ambiguity_score": ambiguity.overall_score,
            }

        log.debug(
            "pm_handler.completion.continuing",
            session_id=state.interview_id,
            ambiguity_score=ambiguity.overall_score,
            rounds=answered_rounds,
        )

    except Exception as e:
        log.warning(
            "pm_handler.completion.check_error",
            session_id=state.interview_id,
            error=str(e),
        )

    return None


@dataclass
class PMInterviewHandler:
    """Handler for the ouroboros_pm_interview MCP tool.

    Manages PM-focused interviews with question classification,
    deferred item tracking, and per-call diff computation.

    Interview completion is determined solely by the engine's ambiguity
    scorer (score ≤ 0.2) or max-rounds cap — there is no user "done"
    signal.

    The handler wraps PMInterviewEngine and adds:
    - Flat MCP parameter interface (session_id, action, answer, cwd, initial_context)
    - pm_meta_{session_id}.json persistence for PM-specific state
    - Deferred/decide-later diff computation per ask_next_question call
    - Automatic completion detection via ambiguity scoring and max-rounds
    """

    pm_engine: PMInterviewEngine | None = field(default=None, repr=False)
    data_dir: Path | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition with flat optional parameters."""
        return MCPToolDefinition(
            name="ouroboros_pm_interview",
            description=(
                "PM interview for product requirements gathering. "
                "Start with initial_context, continue with session_id + answer, "
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
                    description="PM's response to the current interview question",
                    required=False,
                ),
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description=(
                        "Action to perform. Auto-detected from parameter presence when omitted: "
                        "initial_context → 'start', session_id + answer → 'resume'. "
                        "Use 'generate' explicitly to produce PM seed from completed interview."
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
        """Return the injected engine or create a new one with default adapter."""
        if self.pm_engine is not None:
            return self.pm_engine
        adapter = ClaudeAgentAdapter(permission_mode="bypassPermissions")
        return PMInterviewEngine.create(
            llm_adapter=adapter,
            state_dir=self.data_dir or _DATA_DIR,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a PM interview request.

        Action is auto-detected from parameter presence when ``action`` is
        omitted:

        - ``initial_context`` present → ``start``
        - ``session_id`` (+ optional ``answer``) present → ``resume``
        - ``action="generate"`` + ``session_id`` → ``generate``
        """
        initial_context = arguments.get("initial_context")
        session_id = arguments.get("session_id")
        answer = arguments.get("answer")
        cwd = arguments.get("cwd") or os.getcwd()
        selected_repos: list[str] | None = arguments.get("selected_repos")

        # Auto-detect action from parameter presence (AC 13)
        action = _detect_action(arguments)

        engine = self._get_engine()

        try:
            # ── Generate PM seed ──────────────────────────────────
            if action == "generate" and session_id:
                return await self._handle_generate(engine, session_id, cwd)

            # ── Step 2: repo selection (AC 4) ─────────────────────
            if action == "select_repos" and selected_repos is not None:
                return await self._handle_select_repos(
                    engine,
                    selected_repos,
                    session_id,
                    initial_context,
                    cwd,
                )

            # ── Start new interview ────────────────────────────────
            if action == "start" and initial_context:
                return await self._handle_start(
                    engine,
                    initial_context,
                    cwd,
                    selected_repos=selected_repos,
                )

            # ── Resume with answer ─────────────────────────────────
            if action == "resume" and session_id:
                return await self._handle_answer(engine, session_id, answer, cwd)

            return Result.err(
                MCPToolError(
                    "Must provide initial_context to start, or session_id to resume/generate",
                    tool_name="ouroboros_pm_interview",
                )
            )

        except Exception as e:
            log.error("pm_handler.unexpected_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"PM interview failed: {e}",
                    tool_name="ouroboros_pm_interview",
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
        # ── Load brownfield from DB defaults ────────────────────
        brownfield_repos = None
        if selected_repos is not None:
            # Backward compat: explicit selected_repos
            resolved = await self._resolve_repos_from_db(selected_repos)
        else:
            # Auto-load defaults from DB
            resolved = await self._query_default_repos()

        if resolved:
            brownfield_repos = [
                {
                    "path": r.path,
                    "name": r.name,
                    "role": "main",
                    **({"desc": r.desc} if r.desc else {}),
                }
                for r in resolved
            ]
            log.info(
                "pm_handler.start.brownfield_repos",
                count=len(resolved),
                paths=[r.path for r in resolved],
            )

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
        diff = _compute_deferred_diff(engine, deferred_before, decide_later_before)

        # Record unanswered round
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        # Persist
        await engine.save_state(state)
        _save_pm_meta(
            state.interview_id,
            engine,
            cwd=cwd,
            data_dir=self.data_dir,
            status="interview_started",
        )

        # Include pending_reframe in response meta if a reframe occurred
        pending_reframe = None
        if engine._reframe_map:
            reframed = next(reversed(engine._reframe_map))
            pending_reframe = {
                "reframed": reframed,
                "original": engine._reframe_map[reframed],
            }

        meta = {
            "session_id": state.interview_id,
            "status": "interview_started",
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_brownfield": state.is_brownfield,
            "pending_reframe": pending_reframe,
            **diff,
        }

        log.info(
            "pm_handler.started",
            session_id=state.interview_id,
            is_brownfield=state.is_brownfield,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview started. Session ID: {state.interview_id}\n\n{question}"
                        ),
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Brownfield repo helpers
    # ──────────────────────────────────────────────────────────────

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

        meta = _load_pm_meta(session_id, data_dir=self.data_dir)
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

        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Session {session_id} is marked as started but state "
                    f"could not be loaded: {load_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )

        state = load_result.value
        first_question = state.rounds[0].question if state.rounds else "No question available."

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
                    "question": first_question,
                    "is_brownfield": state.is_brownfield,
                    "idempotent": True,
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Answer (resume + record)
    # ──────────────────────────────────────────────────────────────

    async def _handle_answer(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        answer: str | None,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Resume session, record an answer, check completion, then ask next question.

        Completion is determined solely by the engine — either the ambiguity
        score drops below the threshold (requirements are clear) or the
        max-rounds cap is reached.  There is no user "done" signal.
        """
        # Load interview state
        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
            )
        state = load_result.value

        # Restore PM meta into engine
        meta = _load_pm_meta(session_id, self.data_dir)
        if meta:
            engine.restore_meta(meta)

        # If no answer provided, re-display the pending question (retry/reconnect)
        if not answer and state.rounds and state.rounds[-1].user_response is None:
            pending_question = state.rounds[-1].question
            classification = _last_classification(engine)

            pending_reframe = None
            if engine._reframe_map:
                reframed = next(reversed(engine._reframe_map))
                pending_reframe = {
                    "reframed": reframed,
                    "original": engine._reframe_map[reframed],
                }

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=f"Session {session_id}\n\n{pending_question}",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "input_type": "freeText",
                        "response_param": "answer",
                        "question": pending_question,
                        "is_complete": False,
                        "classification": classification,
                        "deferred_this_round": [],
                        "decide_later_this_round": [],
                        "interview_complete": False,
                        "pending_reframe": pending_reframe,
                        "new_deferred": [],
                        "new_decide_later": [],
                        "deferred_count": len(engine.deferred_items),
                        "decide_later_count": len(engine.decide_later_items),
                    },
                )
            )

        # Record answer if provided
        if answer and state.rounds:
            last_question = state.rounds[-1].question
            if state.rounds[-1].user_response is None:
                state.rounds.pop()

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
        # No user "done" signal — completion is determined solely by
        # engine ambiguity scoring and max-rounds cap.
        completion = await _check_completion(state, engine)
        if completion is not None:
            # Mark interview as complete
            await engine.complete_interview(state)
            await engine.save_state(state)
            _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

            decide_later_summary = engine.format_decide_later_summary()
            summary_text = (
                f"Interview complete. Session ID: {session_id}\n"
                f"Rounds completed: {completion['rounds_completed']}\n"
                f"Completion reason: {completion['completion_reason']}\n"
            )
            if completion.get("ambiguity_score") is not None:
                summary_text += f"Ambiguity score: {completion['ambiguity_score']:.2f}\n"
            summary_text += (
                f"\nDeferred items: {len(engine.deferred_items)}\n"
                f"Decide-later items: {len(engine.decide_later_items)}\n"
            )
            if decide_later_summary:
                summary_text += f"\n{decide_later_summary}\n"
            summary_text += f'\nGenerate PM with: action="generate", session_id="{session_id}"'

            response_meta = {
                "session_id": session_id,
                "question": None,
                "is_complete": True,
                "classification": _last_classification(engine),
                "deferred_this_round": [],
                "decide_later_this_round": [],
                **completion,
                "deferred_count": len(engine.deferred_items),
                "decide_later_count": len(engine.decide_later_items),
            }

            log.info(
                "pm_handler.interview_complete",
                session_id=session_id,
                **completion,
            )

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

        # ── Core diff computation (AC 8) ──────────────────────────
        # Snapshot list lengths BEFORE ask_next_question
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

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
        diff = _compute_deferred_diff(engine, deferred_before, decide_later_before)

        # Save unanswered round
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        await engine.save_state(state)
        _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

        # Include pending_reframe in response meta if a new reframe occurred
        pending_reframe = None
        if engine._reframe_map:
            reframed = next(reversed(engine._reframe_map))
            pending_reframe = {
                "reframed": reframed,
                "original": engine._reframe_map[reframed],
            }

        # Extract classification from the last classify call
        classification = _last_classification(engine)

        response_meta = {
            "session_id": session_id,
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_complete": False,
            "classification": classification,
            "deferred_this_round": diff["new_deferred"],
            "decide_later_this_round": diff["new_decide_later"],
            # Keep backward-compat fields from AC 8
            "interview_complete": False,
            "pending_reframe": pending_reframe,
            **diff,
        }

        log.info(
            "pm_handler.question_asked",
            session_id=session_id,
            classification=classification,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"Session {session_id}\n\n{question}",
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
        """Generate PM seed from completed interview (idempotent).

        Loads InterviewState and pm_meta, restores engine via restore_meta(),
        runs generate_pm_seed, saves PM seed to ~/.ouroboros/seeds/ and
        pm.md to {cwd}/.ouroboros/.  Idempotent — overwrites on retry with
        the same session_id.
        """
        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
            )
        state = load_result.value

        # Restore PM meta into engine via engine.restore_meta()
        meta = _load_pm_meta(session_id, self.data_dir)
        if meta:
            engine.restore_meta(meta)

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
        seed_path = engine.save_pm_seed(seed)

        # Save pm.md to {cwd}/.ouroboros/
        pm_output_dir = Path(cwd) / ".ouroboros"
        pm_path = engine.save_pm_document(seed, output_dir=pm_output_dir)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM seed generated: {seed.product_name}\n"
                            f"Seed: {seed_path}\n"
                            f"Document: {pm_path}\n\n"
                            f"Deferred items: {len(seed.deferred_items)}\n"
                            f"Decide-later items: {len(seed.decide_later_items)}"
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "pm_path": str(pm_path),
                    "seed_path": str(seed_path),
                },
            )
        )
