"""Evaluation-phase tool handlers for Ouroboros MCP server.

Contains handlers for drift measurement, evaluation, and lateral thinking tools:
- MeasureDriftHandler: Measures goal deviation from seed specification.
- EvaluateHandler: Three-stage evaluation pipeline (mechanical, semantic, consensus).
- LateralThinkHandler: Generates alternative thinking approaches via personas.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.config import get_semantic_model
from ouroboros.core.errors import ValidationError
from ouroboros.core.seed import Seed
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
from ouroboros.observability.drift import (
    DRIFT_THRESHOLD,
    DriftMeasurement,
)
from ouroboros.orchestrator.session import SessionRepository, tracker_runtime_cwd
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _EvaluationSessionContext:
    """Resolved context for agent-driven evaluation."""

    seed_id: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    cwd: str
    artifact_path: str


@dataclass
class MeasureDriftHandler:
    """Handler for the measure_drift tool.

    Measures goal deviation from the original seed specification
    using DriftMeasurement with weighted components:
    goal (50%), constraint (30%), ontology (20%).
    """

    event_store: EventStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_measure_drift",
            description=(
                "Measure drift from the original seed goal. "
                "Calculates goal deviation score using weighted components: "
                "goal drift (50%), constraint drift (30%), ontology drift (20%). "
                "Returns drift metrics, analysis, and suggestions if drift exceeds threshold."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to measure drift for",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_output",
                    type=ToolInputType.STRING,
                    description="Current execution output to measure drift against the seed goal",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML content for drift calculation",
                    required=True,
                ),
                MCPToolParameter(
                    name="constraint_violations",
                    type=ToolInputType.ARRAY,
                    description="Known constraint violations (e.g., ['Missing tests', 'Wrong language'])",
                    required=False,
                ),
                MCPToolParameter(
                    name="current_concepts",
                    type=ToolInputType.ARRAY,
                    description="Concepts present in the current output (for ontology drift)",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a drift measurement request.

        Args:
            arguments: Tool arguments including session_id, current_output, and seed_content.

        Returns:
            Result containing drift metrics or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        current_output = arguments.get("current_output")
        if not current_output:
            return Result.err(
                MCPToolError(
                    "current_output is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        constraint_violations_raw = arguments.get("constraint_violations", [])
        current_concepts_raw = arguments.get("current_concepts", [])

        log.info(
            "mcp.tool.measure_drift",
            session_id=session_id,
            output_length=len(current_output),
            violations_count=len(constraint_violations_raw),
        )

        try:
            # Parse seed YAML
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as e:
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )
        except (ValidationError, PydanticValidationError) as e:
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )

        try:
            # Calculate drift using real DriftMeasurement
            measurement = DriftMeasurement()
            metrics = measurement.measure(
                current_output=current_output,
                constraint_violations=[str(v) for v in constraint_violations_raw],
                current_concepts=[str(c) for c in current_concepts_raw],
                seed=seed,
            )

            drift_text = (
                f"Drift Measurement Report\n"
                f"=======================\n"
                f"Session: {session_id}\n"
                f"Seed ID: {seed.metadata.seed_id}\n"
                f"Goal: {seed.goal}\n\n"
                f"Combined Drift: {metrics.combined_drift:.2f}\n"
                f"Acceptable Threshold: {DRIFT_THRESHOLD}\n"
                f"Status: {'ACCEPTABLE' if metrics.is_acceptable else 'EXCEEDED'}\n\n"
                f"Component Breakdown:\n"
                f"  Goal Drift: {metrics.goal_drift:.2f} (50% weight)\n"
                f"  Constraint Drift: {metrics.constraint_drift:.2f} (30% weight)\n"
                f"  Ontology Drift: {metrics.ontology_drift:.2f} (20% weight)\n"
            )

            suggestions: list[str] = []
            if not metrics.is_acceptable:
                suggestions.append("Drift exceeds threshold - consider consensus review")
                suggestions.append("Review execution path against original goal")
                if metrics.constraint_drift > 0:
                    suggestions.append(
                        f"Constraint violations detected: {constraint_violations_raw}"
                    )

            if suggestions:
                drift_text += "\nSuggestions:\n"
                for s in suggestions:
                    drift_text += f"  - {s}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=drift_text),),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "seed_id": seed.metadata.seed_id,
                        "goal_drift": metrics.goal_drift,
                        "constraint_drift": metrics.constraint_drift,
                        "ontology_drift": metrics.ontology_drift,
                        "combined_drift": metrics.combined_drift,
                        "is_acceptable": metrics.is_acceptable,
                        "threshold": DRIFT_THRESHOLD,
                        "suggestions": suggestions,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.measure_drift.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to measure drift: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )


@dataclass
class EvaluateHandler:
    """Handler for the ouroboros_evaluate tool.

    Evaluates an execution session using the three-stage evaluation pipeline:
    Stage 1: Mechanical Verification ($0)
    Stage 2: Semantic Evaluation (Standard tier)
    Stage 3: Multi-Model Consensus (Frontier tier, if triggered)
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_mode: AgentMode | None = field(default=None, repr=False)
    TIMEOUT_SECONDS: int = 0  # No server-side timeout; client/runtime decides.

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evaluate",
            description=(
                "Evaluate an Ouroboros execution session using the three-stage evaluation pipeline. "
                "Stage 1 performs mechanical verification (lint, build, test). "
                "Stage 2 performs semantic evaluation of AC compliance and goal alignment. "
                "Stage 3 runs multi-model consensus if triggered by uncertainty or manual request."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description=(
                        "Artifact content to evaluate directly. Required for action='evaluate' "
                        "and internal compatibility mode. Native agent mode should prefer "
                        "artifact_path so subagents can read files in their own context."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="artifact_path",
                    type=ToolInputType.STRING,
                    description=(
                        "Filesystem path to the artifact or project directory to evaluate. "
                        "Used by native subagents with action='state'. Defaults to cwd when omitted."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description=(
                        "Action to perform: evaluate, state, record. "
                        "Use 'state' to get session context for agent evaluation, "
                        "'record' with agent_verdict to save evaluation result. "
                        "Omit for auto-routing: returns state in native mode (default), "
                        "or runs internal pipeline when OUROBOROS_AGENT_MODE=internal."
                    ),
                    required=False,
                    enum=("evaluate", "state", "record"),
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML for goal/constraints extraction",
                    required=False,
                ),
                MCPToolParameter(
                    name="acceptance_criterion",
                    type=ToolInputType.STRING,
                    description="Specific acceptance criterion to evaluate against",
                    required=False,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description="Type of artifact: code, docs, config. Default: code",
                    required=False,
                    default="code",
                    enum=("code", "docs", "config"),
                ),
                MCPToolParameter(
                    name="trigger_consensus",
                    type=ToolInputType.BOOLEAN,
                    description="Force Stage 3 consensus evaluation. Default: False",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="working_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project working directory for language auto-detection of Stage 1 "
                        "mechanical verification commands. Auto-detects language from marker "
                        "files (build.zig, Cargo.toml, go.mod, package.json, etc.). "
                        "Supports .ouroboros/mechanical.toml for custom overrides."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="agent_verdict",
                    type=ToolInputType.STRING,
                    description=(
                        "Pre-computed semantic evaluation from an agent. When provided "
                        "with action='record', saves the evaluation result (no LLM call). "
                        "Expected format: JSON with score, ac_compliance, goal_alignment, "
                        "drift_score, reasoning fields. Used in native agent mode."
                    ),
                    required=False,
                ),
            ),
        )

    @staticmethod
    def _parse_seed(seed_content: str | None) -> Seed | None:
        """Parse optional seed YAML into a Seed model."""
        if not seed_content:
            return None
        try:
            seed_dict = yaml.safe_load(seed_content)
            return Seed.from_dict(seed_dict)
        except (yaml.YAMLError, ValidationError, PydanticValidationError) as exc:
            log.warning("mcp.tool.evaluate.seed_parse_warning", error=str(exc))
            return None

    async def _load_seed_from_session(self, session_id: str) -> tuple[str, Seed] | None:
        """Best-effort load of Seed context from an execution session ID."""
        store = self.event_store or EventStore()
        owns_store = self.event_store is None
        try:
            await store.initialize()
            repo = SessionRepository(store)
            session_result = await repo.reconstruct_session(session_id)
            if session_result.is_err:
                return None
            tracker = session_result.value
            seed_file = Path.home() / ".ouroboros" / "seeds" / f"{tracker.seed_id}.yaml"
            seed_content = await asyncio.to_thread(seed_file.read_text, encoding="utf-8")
            seed = Seed.from_dict(yaml.safe_load(seed_content))
            return tracker_runtime_cwd(tracker) or "", seed
        except Exception as exc:
            log.warning(
                "mcp.tool.evaluate.session_context_unavailable",
                session_id=session_id,
                error=str(exc),
            )
            return None
        finally:
            if owns_store:
                try:
                    await store.close()
                except Exception:
                    pass

    async def _resolve_session_context(
        self,
        session_id: str,
        arguments: dict[str, Any],
        *,
        require_context: bool,
    ) -> Result[_EvaluationSessionContext, MCPServerError]:
        """Resolve goal/AC/cwd context for native agent evaluation."""
        seed = self._parse_seed(arguments.get("seed_content"))
        cwd = arguments.get("working_dir")
        if isinstance(cwd, str) and cwd.strip():
            resolved_cwd = str(Path(cwd).expanduser().resolve())
        else:
            resolved_cwd = ""

        if seed is None:
            loaded = await self._load_seed_from_session(session_id)
            if loaded is not None:
                loaded_cwd, seed = loaded
                if not resolved_cwd:
                    resolved_cwd = loaded_cwd

        if seed is None:
            if require_context:
                return Result.err(
                    MCPToolError(
                        "Could not resolve evaluation context from session_id or seed_content",
                        tool_name="ouroboros_evaluate",
                    )
                )
            seed_id = session_id
            goal = ""
            acceptance_criteria: tuple[str, ...] = ()
            constraints: tuple[str, ...] = ()
        else:
            seed_id = seed.metadata.seed_id
            goal = seed.goal
            acceptance_criteria = tuple(seed.acceptance_criteria)
            constraints = tuple(seed.constraints or [])

        if not resolved_cwd:
            resolved_cwd = str(Path.cwd())

        artifact_path_arg = arguments.get("artifact_path")
        if isinstance(artifact_path_arg, str) and artifact_path_arg.strip():
            artifact_path = str(Path(artifact_path_arg).expanduser().resolve())
        else:
            artifact_path = resolved_cwd

        return Result.ok(
            _EvaluationSessionContext(
                seed_id=seed_id,
                goal=goal,
                acceptance_criteria=acceptance_criteria,
                constraints=constraints,
                cwd=resolved_cwd,
                artifact_path=artifact_path,
            )
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evaluation request.

        Action dispatch:
            1. action="state" → return session state for evaluation (no LLM)
            2. action="record" with agent_verdict → save evaluation result (no LLM)
            3. No action + NATIVE (default) → returns state for platform agent
            4. action="evaluate" or OUROBOROS_AGENT_MODE=internal → internal compatibility pipeline
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError("session_id is required", tool_name="ouroboros_evaluate")
            )

        action = arguments.get("action")
        acceptance_criterion = arguments.get("acceptance_criterion")
        artifact_type = arguments.get("artifact_type", "code")
        trigger_consensus = arguments.get("trigger_consensus", False)
        agent_verdict = arguments.get("agent_verdict")
        effective_mode = get_agent_mode(self.agent_mode)
        needs_native_context = (
            action in {"state", "record"}
            or agent_verdict is not None
            or (action is None and effective_mode == AgentMode.NATIVE)
        )

        log.info(
            "mcp.tool.evaluate",
            session_id=session_id,
            action=action,
            has_seed=arguments.get("seed_content") is not None,
            trigger_consensus=trigger_consensus,
            agent_mode=effective_mode.value,
            has_agent_verdict=agent_verdict is not None,
        )

        try:
            context_result = await self._resolve_session_context(
                session_id,
                arguments,
                require_context=needs_native_context,
            )
            if context_result.is_err:
                if needs_native_context:
                    return Result.err(context_result.error)
                context = _EvaluationSessionContext(
                    seed_id=session_id,
                    goal="",
                    acceptance_criteria=(),
                    constraints=(),
                    cwd=str(Path.cwd()),
                    artifact_path=str(Path.cwd()),
                )
            else:
                context = context_result.value

            current_ac = acceptance_criterion or "Verify execution output meets requirements"

            # --- Action dispatch ---
            if action == "state":
                return self._action_state(
                    session_id=session_id,
                    current_ac=current_ac,
                    artifact_type=artifact_type,
                    context=context,
                )

            if action == "record":
                if agent_verdict is None:
                    return Result.err(
                        MCPToolError(
                            "agent_verdict is required for action=record",
                            tool_name="ouroboros_evaluate",
                        )
                    )
                return self._handle_agent_verdict(
                    agent_verdict=agent_verdict,
                    session_id=session_id,
                    seed_id=context.seed_id,
                )

            # Compatibility: agent_verdict without explicit action
            if agent_verdict is not None:
                return self._handle_agent_verdict(
                    agent_verdict=agent_verdict,
                    session_id=session_id,
                    seed_id=context.seed_id,
                )

            # Environment-driven: native mode returns state for agent
            if effective_mode == AgentMode.NATIVE:
                return self._action_state(
                    session_id=session_id,
                    current_ac=current_ac,
                    artifact_type=artifact_type,
                    context=context,
                )

            # Internal compatibility mode
            artifact = arguments.get("artifact")
            if not artifact:
                return Result.err(
                    MCPToolError("artifact is required", tool_name="ouroboros_evaluate")
                )
            return await self._handle_internal_pipeline(
                session_id,
                context.seed_id,
                current_ac,
                artifact,
                artifact_type,
                context.goal,
                context.constraints,
                trigger_consensus,
                {
                    **arguments,
                    "working_dir": arguments.get("working_dir") or context.cwd,
                },
            )

        except Exception as e:
            log.error("mcp.tool.evaluate.error", error=str(e))
            return Result.err(
                MCPToolError(f"Evaluation failed: {e}", tool_name="ouroboros_evaluate")
            )

    def _action_state(
        self,
        *,
        session_id: str,
        current_ac: str,
        artifact_type: str,
        context: _EvaluationSessionContext,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Return session state for agent-driven evaluation (no LLM)."""
        constraints_text = (
            "\n".join(f"  - {constraint}" for constraint in context.constraints)
            if context.constraints
            else "None"
        )
        ac_text = (
            "\n".join(f"  - {criterion}" for criterion in context.acceptance_criteria)
            if context.acceptance_criteria
            else "  (none)"
        )
        result_text = (
            f"Evaluation State\n"
            f"{'=' * 60}\n"
            f"Session ID: {session_id}\n"
            f"Seed ID: {context.seed_id}\n"
            f"Goal: {context.goal or 'Not specified'}\n\n"
            f"Acceptance Criteria:\n{ac_text}\n\n"
            f"Focused Criterion: {current_ac}\n"
            f"Constraints:\n{constraints_text}\n\n"
            f"Working Directory: {context.cwd}\n"
            f"Artifact Path: {context.artifact_path}\n"
            f"Artifact Type: {artifact_type}\n"
        )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "seed_id": context.seed_id,
                    "goal": context.goal,
                    "acceptance_criteria": list(context.acceptance_criteria),
                    "acceptance_criterion": current_ac,
                    "constraints": list(context.constraints),
                    "cwd": context.cwd,
                    "artifact_path": context.artifact_path,
                    "artifact_type": artifact_type,
                },
            )
        )

    def _handle_agent_verdict(
        self,
        *,
        agent_verdict: str,
        session_id: str,
        seed_id: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Parse pre-computed semantic evaluation from agent (State Layer)."""
        import json

        from ouroboros.evaluation.json_utils import extract_json_payload

        # Normalize: agent may pass a dict instead of a JSON string
        if isinstance(agent_verdict, dict):
            agent_verdict = json.dumps(agent_verdict)

        json_str = extract_json_payload(agent_verdict)
        if not json_str:
            return Result.err(
                MCPToolError(
                    "Could not find JSON in agent_verdict",
                    tool_name="ouroboros_evaluate",
                )
            )

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return Result.err(
                MCPToolError(
                    f"Invalid JSON in agent_verdict: {e}",
                    tool_name="ouroboros_evaluate",
                )
            )

        # Normalize verdict format — four shapes are accepted:
        # A) stage3 bundle:   {stage2, stage3: {approved, majority_ratio, votes}}
        # B) judge verdict:   {verdict, confidence, reasoning, conditions}
        # C) evaluator output: {stage1, stage2: {score, ac_results}, needs_consensus}
        # D) legacy direct:   {score, ac_compliance, goal_alignment, drift_score, reasoning}
        highest_stage = 2
        stage1_passed: bool | None = None
        stage2_ac_compliance: bool | None = None
        stage2_score: float | None = None
        stage3_approved: bool | None = None
        stage3_majority_ratio: float | None = None
        stage3_total_votes: int | None = None
        stage3_approving_votes: int | None = None
        consensus_vote_lines: list[str] = []
        final_approved_override: bool | None = None

        if "stage3" in data or ("votes" in data and "total_votes" in data):
            # Format A: simple multi-model consensus bundle
            stage3 = data.get("stage3") if isinstance(data.get("stage3"), dict) else data
            stage2 = data.get("stage2") if isinstance(data.get("stage2"), dict) else {}
            votes_raw = stage3.get("votes", [])
            stage3_total_votes = int(stage3.get("total_votes", len(votes_raw)))
            stage3_approving_votes = int(
                stage3.get(
                    "approving_votes",
                    sum(
                        1 for vote in votes_raw if isinstance(vote, dict) and vote.get("approved", False)
                    ),
                )
            )
            derived_ratio = (
                stage3_approving_votes / stage3_total_votes if stage3_total_votes else 0.0
            )
            stage3_majority_ratio = float(stage3.get("majority_ratio", derived_ratio))
            stage3_approved = bool(
                stage3.get(
                    "approved",
                    stage3_total_votes >= 3 and stage3_majority_ratio >= 0.66,
                )
            )
            highest_stage = 3
            ac_compliance = stage3_approved
            final_approved_override = stage3_approved

            if stage2:
                score = float(stage2.get("score", stage3_majority_ratio))
                ac_results = stage2.get("ac_results", [])
                stage2_ac_compliance = (
                    all(result.get("passed", False) for result in ac_results)
                    if ac_results
                    else score >= 0.8
                )
                stage2_score = score
                goal_alignment = score
                drift_score = float(stage2.get("drift_score", 0.0))
            else:
                score = stage3_majority_ratio
                goal_alignment = stage3_majority_ratio
                drift_score = 0.0

            reasoning_parts: list[str] = []
            for index, vote in enumerate(votes_raw, start=1):
                if not isinstance(vote, dict):
                    continue
                reviewer = str(vote.get("reviewer") or vote.get("model") or f"reviewer_{index}")
                vote_approved = bool(vote.get("approved", False))
                confidence = float(vote.get("confidence", 0.5))
                consensus_vote_lines.append(
                    f"  [{'APPROVE' if vote_approved else 'REJECT'}] "
                    f"{reviewer} (confidence: {confidence:.2f})"
                )
                vote_reasoning = vote.get("reasoning")
                if vote_reasoning:
                    reasoning_parts.append(f"{reviewer}: {vote_reasoning}")

            reasoning = str(
                stage3.get("summary_reasoning")
                or "; ".join(reasoning_parts)
                or f"Stage 3 majority {stage3_approving_votes}/{stage3_total_votes}"
            )
        elif "verdict" in data:
            # Format A: judge verdict
            verdict_str = data["verdict"]  # "approved" | "rejected" | "conditional"
            confidence = float(data.get("confidence", 0.7))
            reasoning = str(data.get("reasoning", ""))
            ac_compliance = verdict_str == "approved"
            score = confidence if ac_compliance else confidence * 0.5
            goal_alignment = confidence
            drift_score = 0.0
            highest_stage = 3
            stage3_approved = ac_compliance
            final_approved_override = ac_compliance
        elif "stage1" in data:
            # Format C: evaluator output (stage 1+2, or stage-1 early reject)
            stage1 = data.get("stage1") or {}
            stage1_passed = bool(stage1.get("passed", False))
            stage2 = data.get("stage2") or {}
            tests_passed = stage1.get("tests_passed")
            tests_total = stage1.get("tests_total")
            build_ok = stage1.get("build")

            if not stage1_passed:
                score = 0.0
                ac_compliance = False
                goal_alignment = 0.0
                drift_score = 1.0
                reasoning = (
                    f"Stage 1 failed: build={build_ok}, tests={tests_passed}/{tests_total}"
                )
                highest_stage = 1
            else:
                score = float(stage2.get("score", 0.0))
                ac_results = stage2.get("ac_results", [])
                ac_compliance = (
                    all(result.get("passed", False) for result in ac_results)
                    if ac_results
                    else score >= 0.8
                )
                goal_alignment = score
                drift_score = float(stage2.get("drift_score", 0.0))
                notes = [result.get("note", "") for result in ac_results if result.get("note")]
                reasoning = "; ".join(notes) if notes else f"Stage 2 score: {score:.2f}"
                highest_stage = 2
                stage2_ac_compliance = ac_compliance
                stage2_score = score
        elif "stage2" in data:
            # Format D: evaluator stage output (early-exit path, no consensus)
            stage2 = data.get("stage2", {})
            score = float(stage2.get("score", 0.0))
            ac_results = stage2.get("ac_results", [])
            ac_compliance = (
                all(r.get("passed", False) for r in ac_results)
                if ac_results
                else score >= 0.8
            )
            goal_alignment = score
            drift_score = float(stage2.get("drift_score", 0.0))
            notes = [r.get("note", "") for r in ac_results if r.get("note")]
            reasoning = "; ".join(notes) if notes else f"Stage 2 score: {score:.2f}"
            stage2_ac_compliance = ac_compliance
            stage2_score = score
        else:
            # Format E: legacy direct format
            score = float(data.get("score", 0.0))
            ac_compliance = bool(data.get("ac_compliance", False))
            goal_alignment = float(data.get("goal_alignment", 0.0))
            drift_score = float(data.get("drift_score", 0.0))
            reasoning = str(data.get("reasoning", ""))
            stage2_ac_compliance = ac_compliance
            stage2_score = score

        approved = (
            final_approved_override
            if final_approved_override is not None
            else ac_compliance and score >= 0.7
        )

        if highest_stage == 3 and stage3_majority_ratio is not None:
            result_lines = [
                "Evaluation Results",
                "=" * 60,
                f"Execution ID: {session_id}",
                f"Seed ID: {seed_id}",
                f"Final Approval: {'APPROVED' if approved else 'REJECTED'}",
                "",
                f"Highest Stage Completed: {highest_stage}",
            ]
            if stage2_score is not None:
                result_lines.extend(
                    [
                        "Stage 2: Semantic Evaluation",
                        "-" * 40,
                        f"Score: {stage2_score:.2f}",
                        f"AC Compliance: {'YES' if stage2_ac_compliance else 'NO'}"
                        if stage2_ac_compliance is not None
                        else "AC Compliance: unknown",
                        f"Goal Alignment: {goal_alignment:.2f}",
                        f"Drift Score: {drift_score:.2f}",
                        "",
                    ]
                )
            result_lines.extend(
                [
                    "Stage 3: Multi-Model Consensus",
                    "-" * 40,
                    f"Status: {'APPROVED' if stage3_approved else 'REJECTED'}",
                    f"Majority Ratio: {stage3_majority_ratio:.1%}",
                    f"Total Votes: {stage3_total_votes}",
                    f"Approving: {stage3_approving_votes}",
                    *consensus_vote_lines,
                ]
            )
            if reasoning:
                result_lines.extend(
                    [
                        "",
                        f"Reasoning: {reasoning[:200]}{'...' if len(reasoning) > 200 else ''}",
                    ]
                )
            result_text = "\n".join(result_lines) + "\n"
        else:
            result_text = (
                f"Evaluation Results\n"
                f"{'=' * 60}\n"
                f"Execution ID: {session_id}\n"
                f"Seed ID: {seed_id}\n"
                f"Final Approval: {'APPROVED' if approved else 'REJECTED'}\n\n"
                f"Highest Stage Completed: {highest_stage}\n"
                f"Subagent Verdict\n"
                f"{'-' * 40}\n"
                f"Score: {score:.2f}\n"
                f"AC Compliance: {'YES' if ac_compliance else 'NO'}\n"
                f"Goal Alignment: {goal_alignment:.2f}\n"
                f"Drift Score: {drift_score:.2f}\n"
                f"Reasoning: {reasoning[:200]}{'...' if len(reasoning) > 200 else ''}\n"
            )

        meta = {
            "session_id": session_id,
            "seed_id": seed_id,
            "final_approved": approved,
            "highest_stage": highest_stage,
            "stage1_passed": stage1_passed,
            "stage2_ac_compliance": stage2_ac_compliance,
            "stage2_score": stage2_score,
            "stage3_approved": stage3_approved,
            "stage3_majority_ratio": stage3_majority_ratio,
            "stage3_total_votes": stage3_total_votes,
            "stage3_approving_votes": stage3_approving_votes,
            "code_changes_detected": None,
        }

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                is_error=False,
                meta=meta,
            )
        )

    async def _handle_internal_pipeline(
        self,
        session_id: str,
        seed_id: str,
        current_ac: str,
        artifact: str,
        artifact_type: str,
        goal: str,
        constraints: tuple[str, ...],
        trigger_consensus: bool,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Run full evaluation pipeline in internal compatibility mode."""
        from ouroboros.evaluation import (
            EvaluationContext,
            EvaluationPipeline,
            PipelineConfig,
            SemanticConfig,
            TriggerContext,
            build_mechanical_config,
        )

        context = EvaluationContext(
            execution_id=session_id,
            seed_id=seed_id,
            current_ac=current_ac,
            artifact=artifact,
            artifact_type=artifact_type,
            goal=goal,
            constraints=constraints,
        )

        llm_adapter = self.llm_adapter or create_llm_adapter(
            backend=self.llm_backend,
            max_turns=1,
        )
        working_dir_str = arguments.get("working_dir")
        working_dir = Path(working_dir_str).resolve() if working_dir_str else Path.cwd()
        mechanical_config = build_mechanical_config(working_dir)
        config = PipelineConfig(
            mechanical=mechanical_config,
            semantic=SemanticConfig(model=get_semantic_model(self.llm_backend)),
        )
        pipeline = EvaluationPipeline(llm_adapter, config)

        trigger_ctx: TriggerContext | None = None
        if trigger_consensus:
            trigger_ctx = TriggerContext(
                execution_id=session_id,
                seed_modified=True,
            )

        result = await pipeline.evaluate(context, trigger_context=trigger_ctx)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    f"Evaluation failed: {result.error}",
                    tool_name="ouroboros_evaluate",
                )
            )

        eval_result = result.value

        # Detect code changes when Stage 1 fails (presentation concern)
        code_changes: bool | None = None
        if eval_result.stage1_result and not eval_result.stage1_result.passed:
            code_changes = await self._has_code_changes(working_dir)

        result_text = self._format_evaluation_result(eval_result, code_changes=code_changes)

        meta = {
            "session_id": session_id,
            "final_approved": eval_result.final_approved,
            "highest_stage": eval_result.highest_stage_completed,
            "stage1_passed": eval_result.stage1_result.passed
            if eval_result.stage1_result
            else None,
            "stage2_ac_compliance": eval_result.stage2_result.ac_compliance
            if eval_result.stage2_result
            else None,
            "stage2_score": eval_result.stage2_result.score
            if eval_result.stage2_result
            else None,
            "stage3_approved": eval_result.stage3_result.approved
            if eval_result.stage3_result
            else None,
            "code_changes_detected": code_changes,
        }

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                is_error=False,
                meta=meta,
            )
        )

    async def _has_code_changes(self, working_dir: Path) -> bool | None:
        """Detect whether the working tree has code changes.

        Runs ``git status --porcelain`` to check for modifications.

        Returns:
            True if changes detected, False if clean, None if not a git repo
            or git is unavailable.
        """
        from ouroboros.evaluation.mechanical import run_command

        try:
            cmd_result = await run_command(
                ("git", "status", "--porcelain"),
                timeout=10,
                working_dir=working_dir,
            )
            if cmd_result.return_code != 0:
                return None
            return bool(cmd_result.stdout.strip())
        except Exception:
            return None

    def _format_evaluation_result(self, result, *, code_changes: bool | None = None) -> str:
        """Format evaluation result as human-readable text.

        Args:
            result: EvaluationResult from pipeline.
            code_changes: Whether working tree has code changes (Stage 1 context).

        Returns:
            Formatted text representation.
        """
        lines = [
            "Evaluation Results",
            "=" * 60,
            f"Execution ID: {result.execution_id}",
            f"Final Approval: {'APPROVED' if result.final_approved else 'REJECTED'}",
            f"Highest Stage Completed: {result.highest_stage_completed}",
            "",
        ]

        # Stage 1 results
        if result.stage1_result:
            s1 = result.stage1_result
            lines.extend(
                [
                    "Stage 1: Mechanical Verification",
                    "-" * 40,
                    f"Status: {'PASSED' if s1.passed else 'FAILED'}",
                    f"Coverage: {s1.coverage_score:.1%}" if s1.coverage_score else "Coverage: N/A",
                ]
            )
            for check in s1.checks:
                status = "PASS" if check.passed else "FAIL"
                lines.append(f"  [{status}] {check.check_type}: {check.message}")
            lines.append("")

        # Stage 2 results
        if result.stage2_result:
            s2 = result.stage2_result
            lines.extend(
                [
                    "Stage 2: Semantic Evaluation",
                    "-" * 40,
                    f"Score: {s2.score:.2f}",
                    f"AC Compliance: {'YES' if s2.ac_compliance else 'NO'}",
                    f"Goal Alignment: {s2.goal_alignment:.2f}",
                    f"Drift Score: {s2.drift_score:.2f}",
                    f"Uncertainty: {s2.uncertainty:.2f}",
                    f"Reasoning: {s2.reasoning[:200]}..."
                    if len(s2.reasoning) > 200
                    else f"Reasoning: {s2.reasoning}",
                    "",
                ]
            )

        # Stage 3 results
        if result.stage3_result:
            s3 = result.stage3_result
            lines.extend(
                [
                    "Stage 3: Multi-Model Consensus",
                    "-" * 40,
                    f"Status: {'APPROVED' if s3.approved else 'REJECTED'}",
                    f"Majority Ratio: {s3.majority_ratio:.1%}",
                    f"Total Votes: {s3.total_votes}",
                    f"Approving: {s3.approving_votes}",
                ]
            )
            for vote in s3.votes:
                decision = "APPROVE" if vote.approved else "REJECT"
                lines.append(f"  [{decision}] {vote.model} (confidence: {vote.confidence:.2f})")
            if s3.disagreements:
                lines.append("Disagreements:")
                for d in s3.disagreements:
                    lines.append(f"  - {d[:100]}...")
            lines.append("")

        # Failure reason
        if not result.final_approved:
            lines.extend(
                [
                    "Failure Reason",
                    "-" * 40,
                    result.failure_reason or "Unknown",
                ]
            )
            # Contextual annotation for Stage 1 failures
            stage1_failed = result.stage1_result and not result.stage1_result.passed
            if stage1_failed and code_changes is True:
                lines.extend(
                    [
                        "",
                        "⚠ Code changes detected — these are real build/test failures "
                        "that need to be fixed before re-evaluating.",
                    ]
                )
            elif stage1_failed and code_changes is False:
                lines.extend(
                    [
                        "",
                        "ℹ No code changes detected in the working tree. These failures "
                        "are expected if you haven't run `ooo run` yet to produce code.",
                    ]
                )

        return "\n".join(lines)


@dataclass
class LateralThinkHandler:
    """Handler for the lateral_think tool.

    Generates alternative thinking approaches using lateral thinking personas
    to break through stagnation in problem-solving.
    """

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lateral_think",
            description=(
                "Generate alternative thinking approaches using lateral thinking personas. "
                "Use this tool when stuck on a problem to get fresh perspectives from "
                "different thinking modes: hacker (unconventional workarounds), "
                "researcher (seeks information), simplifier (reduces complexity), "
                "architect (restructures approach), or contrarian (challenges assumptions)."
            ),
            parameters=(
                MCPToolParameter(
                    name="problem_context",
                    type=ToolInputType.STRING,
                    description="Description of the stuck situation or problem",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_approach",
                    type=ToolInputType.STRING,
                    description="What has been tried so far that isn't working",
                    required=True,
                ),
                MCPToolParameter(
                    name="persona",
                    type=ToolInputType.STRING,
                    description="Specific persona to use: hacker, researcher, simplifier, architect, or contrarian",
                    required=False,
                    enum=("hacker", "researcher", "simplifier", "architect", "contrarian"),
                ),
                MCPToolParameter(
                    name="failed_attempts",
                    type=ToolInputType.ARRAY,
                    description="Previous failed approaches to avoid repeating",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lateral thinking request.

        Args:
            arguments: Tool arguments including problem_context and current_approach.

        Returns:
            Result containing lateral thinking prompt and questions or error.
        """
        from ouroboros.resilience.lateral import LateralThinker, ThinkingPersona

        problem_context = arguments.get("problem_context")
        if not problem_context:
            return Result.err(
                MCPToolError(
                    "problem_context is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        current_approach = arguments.get("current_approach")
        if not current_approach:
            return Result.err(
                MCPToolError(
                    "current_approach is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        persona_str = arguments.get("persona", "contrarian")
        failed_attempts_raw = arguments.get("failed_attempts") or []

        # Convert string to ThinkingPersona enum
        try:
            persona = ThinkingPersona(persona_str)
        except ValueError:
            return Result.err(
                MCPToolError(
                    f"Invalid persona: {persona_str}. Must be one of: "
                    f"hacker, researcher, simplifier, architect, contrarian",
                    tool_name="ouroboros_lateral_think",
                )
            )

        # Convert failed_attempts to tuple of strings
        failed_attempts = tuple(str(a) for a in failed_attempts_raw if a)

        log.info(
            "mcp.tool.lateral_think",
            persona=persona.value,
            context_length=len(problem_context),
            failed_count=len(failed_attempts),
        )

        try:
            thinker = LateralThinker()
            result = thinker.generate_alternative(
                persona=persona,
                problem_context=problem_context,
                current_approach=current_approach,
                failed_attempts=failed_attempts,
            )

            if result.is_err:
                return Result.err(
                    MCPToolError(
                        result.error,
                        tool_name="ouroboros_lateral_think",
                    )
                )

            lateral_result = result.unwrap()

            # Build the response
            response_text = (
                f"# Lateral Thinking: {lateral_result.approach_summary}\n\n"
                f"{lateral_result.prompt}\n\n"
                "## Questions to Consider\n"
            )
            for question in lateral_result.questions:
                response_text += f"- {question}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=response_text),),
                    is_error=False,
                    meta={
                        "persona": lateral_result.persona.value,
                        "approach_summary": lateral_result.approach_summary,
                        "questions_count": len(lateral_result.questions),
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.lateral_think.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Lateral thinking failed: {e}",
                    tool_name="ouroboros_lateral_think",
                )
            )
