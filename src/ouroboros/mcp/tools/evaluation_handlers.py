"""Evaluation-phase tool handlers for Ouroboros MCP server.

Contains handlers for drift measurement, evaluation, and lateral thinking tools:
- MeasureDriftHandler: Measures goal deviation from seed specification.
- EvaluateHandler: Three-stage evaluation pipeline (mechanical, semantic, consensus).
- LateralThinkHandler: Generates alternative thinking approaches via personas.
"""

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
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)


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
                    description="The execution output/artifact to evaluate",
                    required=True,
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
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evaluation request.

        Args:
            arguments: Tool arguments including session_id, artifact, and optional seed_content.

        Returns:
            Result containing evaluation results or error.
        """
        from pathlib import Path

        from ouroboros.evaluation import (
            EvaluationContext,
            EvaluationPipeline,
            PipelineConfig,
            SemanticConfig,
            build_mechanical_config,
        )

        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        seed_content = arguments.get("seed_content")
        acceptance_criterion = arguments.get("acceptance_criterion")
        artifact_type = arguments.get("artifact_type", "code")
        trigger_consensus = arguments.get("trigger_consensus", False)

        log.info(
            "mcp.tool.evaluate",
            session_id=session_id,
            has_seed=seed_content is not None,
            trigger_consensus=trigger_consensus,
        )

        try:
            # Extract goal/constraints from seed if provided
            goal = ""
            constraints: tuple[str, ...] = ()
            seed_id = session_id  # fallback

            if seed_content:
                try:
                    seed_dict = yaml.safe_load(seed_content)
                    seed = Seed.from_dict(seed_dict)
                    goal = seed.goal
                    constraints = tuple(seed.constraints)
                    seed_id = seed.metadata.seed_id
                except (yaml.YAMLError, ValidationError, PydanticValidationError) as e:
                    log.warning("mcp.tool.evaluate.seed_parse_warning", error=str(e))
                    # Continue without seed data - not fatal

            # Try to enrich from session repository if event_store available
            if not goal:
                store = self.event_store or EventStore()
                try:
                    await store.initialize()
                    repo = SessionRepository(store)
                    session_result = await repo.reconstruct_session(session_id)
                    if session_result.is_ok:
                        tracker = session_result.value
                        seed_id = tracker.seed_id
                except Exception:
                    pass  # Best-effort enrichment

            # Use acceptance_criterion or derive from seed
            current_ac = acceptance_criterion or "Verify execution output meets requirements"

            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=current_ac,
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
            )

            # Use injected or create services
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
            result = await pipeline.evaluate(context)

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

            # Build result text
            result_text = self._format_evaluation_result(eval_result, code_changes=code_changes)

            # Build metadata
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
        except Exception as e:
            log.error("mcp.tool.evaluate.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Evaluation failed: {e}",
                    tool_name="ouroboros_evaluate",
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
        failed_attempts_raw = arguments.get("failed_attempts", [])

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
