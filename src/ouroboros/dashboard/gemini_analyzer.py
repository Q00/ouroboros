"""Gemini 3 Context Analyzer for 50+ Iteration History.

This module leverages Gemini 3's 1M token context window to analyze
entire iteration histories in a single pass, enabling holistic pattern
recognition that was previously impossible with smaller context models.

Key Features:
- Full iteration history analysis (50-200+ iterations)
- Pattern recognition across entire problem-solving trajectory
- Devil's Advocate critical analysis
- Multi-dimensional progress tracking
- Integrated pattern and dependency analysis

Integrations:
- PatternAnalyzer: Detect recurring failure patterns
- DependencyPredictor: Predict AC blocking dependencies
- EnhancedDevilAdvocate: Deep Socratic challenging
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import litellm
import structlog

from ouroboros.core.types import Result
from ouroboros.core.errors import ProviderError

# Import unified models
from ouroboros.dashboard.models import (
    IterationData,
    IterationOutcome,
    AnalysisInsight,
    ProgressTrajectory,
    FullHistoryAnalysis,
    ConvergenceState,
)

if TYPE_CHECKING:
    from ouroboros.dashboard.api_logger import GeminiAPILogger
    from ouroboros.dashboard.pattern_analyzer import PatternAnalyzer, FailurePattern
    from ouroboros.dashboard.dependency_predictor import DependencyPredictor, ACDependency
    from ouroboros.dashboard.devil_advocate import EnhancedDevilAdvocate, ChallengeResult

log = structlog.get_logger()


class GeminiContextAnalyzer:
    """Analyzer that leverages Gemini 3's 1M token context.

    This class is the core innovation for the hackathon - it demonstrates
    how Gemini 3's massive context window enables analysis patterns that
    were impossible with smaller context models.

    Key differentiators:
    1. Analyze 50-200+ iterations in a SINGLE API call
    2. Holistic pattern recognition across entire history
    3. Detect long-range dependencies and recurring patterns
    4. Provide Devil's Advocate critique of the full trajectory

    Usage:
        analyzer = GeminiContextAnalyzer(api_key="...")
        history = [IterationData(...) for _ in range(100)]

        result = await analyzer.analyze_full_history(history)
        if result.is_ok:
            analysis = result.value
            print(f"Analyzed {analysis.total_iterations} iterations")
            print(f"Token count: {analysis.token_count:,}")
            for insight in analysis.insights:
                print(f"- {insight.title}: {insight.description}")
    """

    # Gemini 3 model with 1M token context
    MODEL = "gemini-2.5-pro-preview-05-06"

    # Token estimation: ~4 chars per token for English
    CHARS_PER_TOKEN = 4

    # Maximum tokens we'll use (leave buffer for response)
    MAX_INPUT_TOKENS = 800_000

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_logger: GeminiAPILogger | None = None,
        temperature: float = 0.3,
        max_output_tokens: int = 8192,
    ) -> None:
        """Initialize the Gemini Context Analyzer.

        Args:
            api_key: Google API key. If None, uses GOOGLE_API_KEY env var.
            api_logger: Optional logger for API calls.
            temperature: Sampling temperature (lower = more deterministic).
            max_output_tokens: Maximum tokens for response.
        """
        self._api_key = api_key
        self._api_logger = api_logger
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for a given text.

        Args:
            text: The text to estimate tokens for.

        Returns:
            Estimated token count.
        """
        return len(text) // self.CHARS_PER_TOKEN

    def _build_history_prompt(
        self,
        iterations: list[IterationData],
        problem_context: str,
    ) -> str:
        """Build the full history analysis prompt.

        This method constructs a comprehensive prompt that includes:
        1. Problem context and constraints
        2. Full iteration history with all details
        3. Analysis instructions for Gemini 3

        Args:
            iterations: List of iteration data points.
            problem_context: Description of the problem being solved.

        Returns:
            Complete prompt string.
        """
        # Build iteration history section
        history_lines = []
        for it in iterations:
            history_lines.append(f"""
### Iteration {it.iteration_id}
- **Timestamp**: {it.timestamp.isoformat()}
- **Phase**: {it.phase}
- **Action**: {it.action}
- **Result**: {it.result}
- **State**: {json.dumps(it.state, indent=2)}
- **Metrics**: {json.dumps(it.metrics)}
- **Reasoning**: {it.reasoning or "N/A"}
""")

        history_text = "\n".join(history_lines)

        prompt = f"""You are an expert AI system analyzer. You have been given the COMPLETE iteration history of an AI agent solving a complex problem. Your task is to analyze this entire history holistically, leveraging your ability to see patterns across ALL iterations simultaneously.

## Problem Context
{problem_context}

## Complete Iteration History ({len(iterations)} iterations)
{history_text}

## Analysis Instructions

Analyze this complete history and provide:

1. **INSIGHTS**: Identify patterns, anomalies, and key findings across the entire trajectory. For each insight:
   - Type: "pattern" (recurring behavior), "anomaly" (unusual deviation), "recommendation" (improvement suggestion), or "critical" (fundamental issue)
   - Title: Brief, descriptive title
   - Description: Detailed explanation
   - Confidence: Your confidence score (0-1)
   - Affected Iterations: Which iterations this relates to
   - Evidence: Specific examples from the history

2. **PROGRESS TRAJECTORIES**: Track multiple dimensions:
   - efficiency: Time/steps to achieve goals
   - correctness: Success rate of actions
   - coverage: Exploration of solution space
   - For each, identify the trend and any inflection points

3. **DEVIL'S ADVOCATE CRITIQUE**: As a critical reviewer, identify:
   - Is the agent solving the ROOT CAUSE or just symptoms?
   - What fundamental assumptions might be wrong?
   - What alternative approaches were not explored?
   - What are the hidden risks in the current trajectory?

4. **EXECUTIVE SUMMARY**: A 2-3 sentence summary of the overall analysis.

Respond in the following JSON format:
{{
  "insights": [
    {{
      "insight_type": "pattern|anomaly|recommendation|critical",
      "title": "...",
      "description": "...",
      "confidence": 0.0-1.0,
      "affected_iterations": [1, 2, 3],
      "evidence": ["...", "..."]
    }}
  ],
  "trajectories": [
    {{
      "dimension": "efficiency|correctness|coverage",
      "values": [[iteration_id, value], ...],
      "trend": "improving|degrading|stable|volatile",
      "inflection_points": [iteration_ids]
    }}
  ],
  "devil_advocate_critique": "...",
  "summary": "..."
}}
"""
        return prompt

    async def analyze_full_history(
        self,
        iterations: list[IterationData],
        problem_context: str = "AI agent solving a complex optimization problem",
    ) -> Result[FullHistoryAnalysis, ProviderError]:
        """Analyze the complete iteration history in a single Gemini 3 call.

        This is the key innovation - using Gemini 3's 1M token context to
        analyze 50-200+ iterations holistically, finding patterns that
        would be impossible to detect with smaller context windows.

        Args:
            iterations: List of iteration data points (50+ recommended).
            problem_context: Description of the problem being solved.

        Returns:
            Result containing FullHistoryAnalysis or ProviderError.
        """
        if not iterations:
            return Result.err(ProviderError(
                "No iterations to analyze",
                provider="gemini",
            ))

        # Build the complete prompt
        prompt = self._build_history_prompt(iterations, problem_context)
        token_estimate = self._estimate_tokens(prompt)

        log.info(
            "gemini.analyze.started",
            iteration_count=len(iterations),
            estimated_tokens=token_estimate,
            model=self.MODEL,
        )

        if token_estimate > self.MAX_INPUT_TOKENS:
            log.warning(
                "gemini.analyze.token_limit_warning",
                estimated_tokens=token_estimate,
                max_tokens=self.MAX_INPUT_TOKENS,
            )

        # Prepare API call
        messages = [
            {"role": "user", "content": prompt}
        ]

        start_time = time.perf_counter()
        request_id = f"gemini-analysis-{int(time.time())}"

        # Log the request
        if self._api_logger:
            await self._api_logger.log_request(
                request_id=request_id,
                model=self.MODEL,
                messages=messages,
                token_estimate=token_estimate,
            )

        try:
            # Make the API call
            response = await litellm.acompletion(
                model=self.MODEL,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_output_tokens,
                api_key=self._api_key,
                response_format={"type": "json_object"},
            )

            elapsed_ms = (time.perf_counter() - start_time) * 1000

            # Extract response content
            content = response.choices[0].message.content
            usage = response.usage

            # Log the response
            if self._api_logger:
                await self._api_logger.log_response(
                    request_id=request_id,
                    response=content,
                    usage={
                        "prompt_tokens": usage.prompt_tokens if usage else 0,
                        "completion_tokens": usage.completion_tokens if usage else 0,
                        "total_tokens": usage.total_tokens if usage else 0,
                    },
                    elapsed_ms=elapsed_ms,
                )

            log.info(
                "gemini.analyze.completed",
                iteration_count=len(iterations),
                elapsed_ms=elapsed_ms,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            )

            # Parse the JSON response
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                log.error("gemini.analyze.json_parse_error", error=str(e))
                return Result.err(ProviderError(
                    f"Failed to parse Gemini response as JSON: {e}",
                    provider="gemini",
                ))

            # Convert to domain objects
            insights = [
                AnalysisInsight(
                    insight_type=i.get("insight_type", "pattern"),
                    title=i.get("title", ""),
                    description=i.get("description", ""),
                    confidence=float(i.get("confidence", 0.5)),
                    affected_iterations=i.get("affected_iterations", []),
                    evidence=i.get("evidence", []),
                )
                for i in parsed.get("insights", [])
            ]

            trajectories = [
                ProgressTrajectory(
                    dimension=t.get("dimension", "unknown"),
                    values=[(v[0], v[1]) for v in t.get("values", [])],
                    trend=t.get("trend", "stable"),
                    inflection_points=t.get("inflection_points", []),
                )
                for t in parsed.get("trajectories", [])
            ]

            analysis = FullHistoryAnalysis(
                total_iterations=len(iterations),
                token_count=usage.prompt_tokens if usage else token_estimate,
                analysis_time_ms=elapsed_ms,
                insights=insights,
                trajectories=trajectories,
                devil_advocate_critique=parsed.get("devil_advocate_critique", ""),
                summary=parsed.get("summary", ""),
                raw_response=parsed,
            )

            return Result.ok(analysis)

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            # Log the error
            if self._api_logger:
                await self._api_logger.log_error(
                    request_id=request_id,
                    error=str(e),
                    elapsed_ms=elapsed_ms,
                )

            log.error(
                "gemini.analyze.failed",
                error=str(e),
                elapsed_ms=elapsed_ms,
            )

            return Result.err(ProviderError(
                f"Gemini API call failed: {e}",
                provider="gemini",
                details={"original_exception": type(e).__name__},
            ))

    async def get_devil_advocate_analysis(
        self,
        iterations: list[IterationData],
        current_solution: str,
        goal: str,
    ) -> Result[str, ProviderError]:
        """Get focused Devil's Advocate analysis.

        Uses Gemini 3's full context to critically examine the solution
        against the complete history of attempts.

        Args:
            iterations: Complete iteration history.
            current_solution: The proposed solution.
            goal: The original goal.

        Returns:
            Result containing critique text or ProviderError.
        """
        # Build iteration summary
        history_summary = "\n".join([
            f"- Iteration {it.iteration_id}: {it.action} -> {it.result}"
            for it in iterations
        ])

        prompt = f"""You are a Devil's Advocate analyst. Given the COMPLETE history of {len(iterations)} iterations and the proposed solution, critically examine whether this solution addresses the ROOT CAUSE or merely treats SYMPTOMS.

## Goal
{goal}

## Complete Iteration History
{history_summary}

## Proposed Solution
{current_solution}

## Your Task

Provide a critical analysis covering:
1. **Root Cause Assessment**: Does this solution address the fundamental problem?
2. **Hidden Assumptions**: What assumptions might be wrong?
3. **Unexplored Alternatives**: What approaches were not tried?
4. **Risk Analysis**: What could go wrong?
5. **Confidence Score**: How confident are you in this solution (0-100%)?

Be constructive but thorough. Your analysis helps improve the solution."""

        messages = [{"role": "user", "content": prompt}]

        try:
            response = await litellm.acompletion(
                model=self.MODEL,
                messages=messages,
                temperature=0.4,  # Slightly higher for creative critique
                max_tokens=2048,
                api_key=self._api_key,
            )

            content = response.choices[0].message.content
            return Result.ok(content)

        except Exception as e:
            return Result.err(ProviderError(
                f"Devil's Advocate analysis failed: {e}",
                provider="gemini",
            ))

    # =========================================================================
    # Integrated Analysis Methods (new)
    # =========================================================================

    async def analyze_with_patterns(
        self,
        iterations: list[IterationData],
        problem_context: str = "AI agent solving a complex optimization problem",
    ) -> Result[dict[str, Any], ProviderError]:
        """Analyze history with integrated pattern detection.

        Combines full history analysis with PatternAnalyzer for
        comprehensive failure pattern detection.

        Args:
            iterations: List of iteration data points.
            problem_context: Description of the problem being solved.

        Returns:
            Result containing combined analysis or ProviderError.
        """
        from ouroboros.dashboard.pattern_analyzer import PatternAnalyzer

        # Run pattern analysis
        pattern_analyzer = PatternAnalyzer()
        patterns = await pattern_analyzer.analyze_patterns(iterations)
        pattern_clusters = await pattern_analyzer.cluster_patterns(patterns)
        pattern_network = pattern_analyzer.build_pattern_network(patterns)
        pattern_summary = pattern_analyzer.get_summary()

        # Run full history analysis
        history_result = await self.analyze_full_history(iterations, problem_context)

        if history_result.is_err:
            return history_result

        analysis = history_result.value

        # Combine results
        combined = {
            "history_analysis": {
                "total_iterations": analysis.total_iterations,
                "token_count": analysis.token_count,
                "analysis_time_ms": analysis.analysis_time_ms,
                "insights": [
                    {
                        "type": i.insight_type,
                        "title": i.title,
                        "description": i.description,
                        "confidence": i.confidence,
                    }
                    for i in analysis.insights
                ],
                "trajectories": [
                    {
                        "dimension": t.dimension,
                        "trend": t.trend,
                        "inflection_points": t.inflection_points,
                    }
                    for t in analysis.trajectories
                ],
                "devil_advocate_critique": analysis.devil_advocate_critique,
                "summary": analysis.summary,
            },
            "pattern_analysis": {
                "patterns": [p.to_dict() for p in patterns],
                "clusters": [
                    {
                        "cluster_id": c.cluster_id,
                        "label": c.cluster_label,
                        "total_occurrences": c.total_occurrences,
                        "affected_acs": list(c.affected_acs),
                    }
                    for c in pattern_clusters
                ],
                "network": pattern_network.to_dict(),
                "summary": pattern_summary,
            },
        }

        return Result.ok(combined)

    async def get_dependency_analysis(
        self,
        iterations: list[IterationData],
        ac_ids: list[str] | None = None,
    ) -> Result[dict[str, Any], ProviderError]:
        """Analyze AC dependencies from iteration history.

        Uses DependencyPredictor to identify blocking relationships
        between ACs based on failure patterns.

        Args:
            iterations: List of iteration data points.
            ac_ids: Optional list of AC IDs to analyze.

        Returns:
            Result containing dependency analysis or ProviderError.
        """
        from ouroboros.dashboard.dependency_predictor import DependencyPredictor

        predictor = DependencyPredictor()
        dependencies = await predictor.predict_dependencies(iterations, ac_ids)

        execution_order = predictor.get_execution_order()
        critical_path = predictor.get_critical_path()
        dependency_tree = predictor.build_dependency_tree()
        summary = predictor.get_summary()

        result = {
            "dependencies": [d.to_dict() for d in dependencies],
            "execution_order": execution_order,
            "critical_path": critical_path,
            "dependency_tree": dependency_tree.to_dict(),
            "summary": summary,
        }

        return Result.ok(result)

    async def get_deep_challenge(
        self,
        artifact: str,
        goal: str,
        iterations: list[IterationData],
        depth: int = 1,
    ) -> Result[dict[str, Any], ProviderError]:
        """Get deep Devil's Advocate challenge with full context.

        Uses EnhancedDevilAdvocate with pattern awareness for
        comprehensive root cause analysis.

        Args:
            artifact: The solution/artifact to challenge.
            goal: The original goal.
            iterations: Iteration history for context.
            depth: Depth of Socratic questioning (1-5).

        Returns:
            Result containing challenge analysis or ProviderError.
        """
        from ouroboros.dashboard.pattern_analyzer import PatternAnalyzer
        from ouroboros.dashboard.devil_advocate import EnhancedDevilAdvocate

        # First detect patterns
        pattern_analyzer = PatternAnalyzer()
        patterns = await pattern_analyzer.analyze_patterns(iterations)

        # Build iteration history string
        history_lines = []
        for it in iterations[-50:]:  # Last 50 for context
            history_lines.append(
                f"- Iteration {it.iteration_id}: {it.action} -> {it.result}"
            )
        iteration_history = "\n".join(history_lines)

        # Run enhanced devil's advocate
        devil = EnhancedDevilAdvocate()
        challenge_result = await devil.challenge(
            artifact=artifact,
            goal=goal,
            iteration_history=iteration_history,
            patterns=patterns,
            depth=depth,
        )

        result = {
            "challenges": [c.to_dict() for c in challenge_result.challenges],
            "is_root_solution": challenge_result.is_root_solution,
            "overall_confidence": challenge_result.overall_confidence,
            "recommendation": challenge_result.recommendation,
            "context_tokens_used": challenge_result.context_tokens_used,
            "primary_challenge": (
                challenge_result.primary_challenge.to_dict()
                if challenge_result.primary_challenge
                else None
            ),
        }

        return Result.ok(result)

    def compute_convergence_state(
        self,
        iterations: list[IterationData],
    ) -> ConvergenceState:
        """Compute convergence state from iterations.

        Analyzes iteration outcomes to determine overall convergence.

        Args:
            iterations: List of iteration data points.

        Returns:
            ConvergenceState with current metrics.
        """
        total = len(iterations)
        successful = sum(
            1 for i in iterations
            if i.outcome == IterationOutcome.SUCCESS
        )
        failed = sum(
            1 for i in iterations
            if i.outcome in (IterationOutcome.FAILURE, IterationOutcome.STAGNANT)
        )

        # Track AC satisfaction
        ac_satisfaction: dict[str, bool] = {}
        for it in iterations:
            if it.ac_id:
                if it.outcome == IterationOutcome.SUCCESS:
                    ac_satisfaction[it.ac_id] = True
                elif it.ac_id not in ac_satisfaction:
                    ac_satisfaction[it.ac_id] = False

        total_acs = len(ac_satisfaction) or 1
        satisfied_acs = sum(1 for v in ac_satisfaction.values() if v)
        satisfaction_pct = (satisfied_acs / total_acs) * 100

        # Calculate convergence rate
        convergence_rate = successful / total if total > 0 else 0.0

        # Detect stagnation (last 5 iterations)
        is_stagnant = False
        if len(iterations) >= 5:
            recent = iterations[-5:]
            recent_outcomes = [i.outcome for i in recent if i.outcome]
            if all(o == IterationOutcome.FAILURE for o in recent_outcomes):
                is_stagnant = True
            elif all(o == IterationOutcome.STAGNANT for o in recent_outcomes):
                is_stagnant = True

        return ConvergenceState(
            total_iterations=total,
            successful_iterations=successful,
            failed_iterations=failed,
            satisfaction_percentage=satisfaction_pct,
            convergence_rate=convergence_rate,
            is_converging=convergence_rate > 0.1 and not is_stagnant,
            is_stagnant=is_stagnant,
            estimated_remaining=-1,
            ac_satisfaction=ac_satisfaction,
            context_utilization=0.0,
        )


__all__ = [
    # Re-export from models for backward compatibility
    "IterationData",
    "IterationOutcome",
    "AnalysisInsight",
    "ProgressTrajectory",
    "FullHistoryAnalysis",
    "ConvergenceState",
    # Main analyzer
    "GeminiContextAnalyzer",
]
