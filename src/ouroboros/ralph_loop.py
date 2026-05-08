"""MCP-owned Ralph loop runner.

This module is the first runtime-owned slice for issue #528.  It keeps
Ralph's multi-generation loop out of client-side skill pseudo-code by
running repeated ``evolve_step`` calls inside one background job.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

_TERMINAL_SUCCESS_ACTIONS = frozenset({"converged"})
_TERMINAL_FAILURE_ACTIONS = frozenset({"failed", "interrupted", "exhausted", "stagnated"})

DEFAULT_PER_ITERATION_TIMEOUT_SECONDS = 1800.0
DEFAULT_OSCILLATION_WINDOW = 3
DEFAULT_GRADE_REGRESSION_WINDOW = 2

_LETTER_GRADE_MAP: dict[str, float] = {
    "A": 1.0,
    "B": 0.75,
    "C": 0.5,
    "D": 0.25,
    "F": 0.0,
}


class EvolveStepLike(Protocol):
    """Minimal handler surface consumed by :class:`RalphLoopRunner`."""

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]: ...


@dataclass(frozen=True, slots=True)
class RalphLoopConfig:
    """Configuration for a single Ralph loop job."""

    lineage_id: str
    seed_content: str | None = None
    execute: bool = True
    parallel: bool = True
    skip_qa: bool = False
    project_dir: str | None = None
    max_generations: int = 10
    per_iteration_timeout_seconds: float = DEFAULT_PER_ITERATION_TIMEOUT_SECONDS
    max_total_seconds: float | None = None
    oscillation_window: int = DEFAULT_OSCILLATION_WINDOW
    grade_regression_window: int = DEFAULT_GRADE_REGRESSION_WINDOW
    max_total_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RalphIteration:
    """One evolve_step iteration executed by Ralph."""

    generation: int | None
    action: str
    qa_verdict: str | None = None
    is_error: bool = False
    findings_hash: str | None = None
    grade: float | None = None


@dataclass(frozen=True, slots=True)
class RalphLoopResult:
    """Final result of a Ralph loop."""

    lineage_id: str
    status: str
    stop_reason: str
    iterations: tuple[RalphIteration, ...]
    final_result: MCPToolResult
    max_generations: int

    @property
    def iteration_count(self) -> int:
        return len(self.iterations)

    def to_tool_result(self) -> MCPToolResult:
        """Render the loop result as an MCP tool result."""
        lines = [
            "# Ralph Loop Result",
            "",
            f"Lineage ID: {self.lineage_id}",
            f"Status: {self.status}",
            f"Stop reason: {self.stop_reason}",
            f"Iterations: {self.iteration_count}/{self.max_generations}",
            "",
            "## Iterations",
        ]
        for index, iteration in enumerate(self.iterations, start=1):
            generation = iteration.generation if iteration.generation is not None else "?"
            qa = f", qa={iteration.qa_verdict}" if iteration.qa_verdict else ""
            lines.append(f"- {index}: generation={generation}, action={iteration.action}{qa}")
        lines.extend(["", "## Final generation output", self.final_result.text_content])

        meta = dict(self.final_result.meta)
        meta.update(
            {
                "lineage_id": self.lineage_id,
                "status": self.status,
                "stop_reason": self.stop_reason,
                "iterations": self.iteration_count,
                "max_generations": self.max_generations,
                "actions": [iteration.action for iteration in self.iterations],
                "generations": [iteration.generation for iteration in self.iterations],
            }
        )
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(lines)),),
            is_error=self.status == "failed" or self.final_result.is_error,
            meta=meta,
        )


@dataclass(slots=True)
class RalphLoopRunner:
    """Run repeated evolve_step generations until Ralph reaches a stop condition."""

    evolve_handler: EvolveStepLike
    progress_callback: Any | None = field(default=None, repr=False)

    async def run(self, config: RalphLoopConfig) -> RalphLoopResult:
        """Run a Ralph loop and return a structured result."""
        if config.max_generations < 1:
            raise ValueError("max_generations must be >= 1")

        iterations: list[RalphIteration] = []
        final_result: MCPToolResult | None = None
        seed_content = config.seed_content
        stop_reason = "max_generations reached"
        status = "completed"
        loop_start_monotonic = time.monotonic()

        for iteration_index in range(1, config.max_generations + 1):
            if (
                config.max_total_seconds is not None
                and time.monotonic() - loop_start_monotonic >= config.max_total_seconds
            ):
                status = "failed"
                stop_reason = "wall_clock_exhausted"
                # Always replace final_result. After iteration 2+ a prior
                # successful generation would otherwise leak its action/text/meta
                # into the terminal MCPToolResult, contradicting the new
                # stop_reason. The synthetic result is the only authoritative
                # record of the budget-exhausted terminal state.
                final_result = MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                "Ralph loop wall-clock budget exhausted before "
                                f"iteration {iteration_index} could start "
                                f"(max_total_seconds={config.max_total_seconds:g})."
                            ),
                        ),
                    ),
                    is_error=True,
                    meta={
                        "lineage_id": config.lineage_id,
                        "action": "wall_clock_exhausted",
                        "generation": None,
                    },
                )
                break

            arguments: dict[str, Any] = {
                "lineage_id": config.lineage_id,
                "execute": config.execute,
                "parallel": config.parallel,
                "skip_qa": config.skip_qa,
            }
            if seed_content is not None:
                arguments["seed_content"] = seed_content
            if config.project_dir:
                arguments["project_dir"] = config.project_dir

            iteration_timed_out = False
            try:
                async with asyncio.timeout(config.per_iteration_timeout_seconds) as iteration_cm:
                    result = await self.evolve_handler.handle(arguments)
            except TimeoutError:
                # Distinguish *our* wall-clock timeout from any TimeoutError raised
                # by ``evolve_handler.handle`` itself (e.g. an inner provider
                # timeout). Only when ``iteration_cm.expired()`` is True did the
                # per-iteration deadline actually fire; otherwise the inner
                # exception is the real failure and must propagate so the outer
                # caller can surface the underlying cause instead of a misleading
                # ``stop_reason=iteration_timeout``.
                if not iteration_cm.expired():
                    raise
                iteration_timed_out = True

            if iteration_timed_out:
                iterations.append(
                    RalphIteration(
                        generation=None,
                        action="iteration_timeout",
                        qa_verdict=None,
                        is_error=True,
                    )
                )
                status = "failed"
                stop_reason = "iteration_timeout"
                # Always replace final_result. After iteration 2+ a prior
                # successful generation would otherwise leak its action/text/meta
                # into the terminal MCPToolResult, contradicting the new
                # stop_reason. The synthetic result is the only authoritative
                # record of the timed-out terminal state.
                final_result = MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                "Ralph iteration "
                                f"{iteration_index} exceeded "
                                f"{config.per_iteration_timeout_seconds:.0f}s timeout."
                            ),
                        ),
                    ),
                    is_error=True,
                    meta={
                        "lineage_id": config.lineage_id,
                        "action": "iteration_timeout",
                        "generation": None,
                    },
                )
                break

            if result.is_err:
                raise RuntimeError(str(result.error))

            final_result = result.value
            action = str(final_result.meta.get("action", "unknown"))
            generation = _coerce_int(final_result.meta.get("generation"))
            qa_verdict = _extract_qa_verdict(final_result.meta)
            findings_hash = _extract_findings_hash(final_result.meta)
            grade = _extract_grade(final_result.meta)
            iterations.append(
                RalphIteration(
                    generation=generation,
                    action=action,
                    qa_verdict=qa_verdict,
                    is_error=final_result.is_error,
                    findings_hash=findings_hash,
                    grade=grade,
                )
            )

            if self.progress_callback is not None:
                await self.progress_callback(iteration_index, final_result)

            if _qa_passed(final_result.meta):
                status = "completed"
                stop_reason = "qa passed"
                break
            if action in _TERMINAL_SUCCESS_ACTIONS:
                status = "completed"
                stop_reason = action
                break
            if action in _TERMINAL_FAILURE_ACTIONS or final_result.is_error:
                status = "failed"
                stop_reason = action
                break

            if _is_oscillating(iterations, config.oscillation_window):
                status = "failed"
                stop_reason = "oscillation_detected"
                break
            if _is_grade_regressing(iterations, config.grade_regression_window):
                status = "failed"
                stop_reason = "grade_regressing"
                break

            # Gen 2+ reconstructs state from EventStore by lineage_id.
            seed_content = None
        else:
            if final_result is not None:
                status = "completed"
                stop_reason = "max_generations reached"

        if final_result is None:
            raise RuntimeError("Ralph loop produced no evolve_step result")

        return RalphLoopResult(
            lineage_id=config.lineage_id,
            status=status,
            stop_reason=stop_reason,
            iterations=tuple(iterations),
            final_result=final_result,
            max_generations=config.max_generations,
        )


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_qa_verdict(meta: dict[str, Any]) -> str | None:
    qa = meta.get("qa")
    if not isinstance(qa, dict):
        return None
    verdict = qa.get("verdict") or qa.get("status")
    return str(verdict).lower() if verdict is not None else None


def _qa_passed(meta: dict[str, Any]) -> bool:
    verdict = _extract_qa_verdict(meta)
    return verdict in {"pass", "passed"}


def _extract_findings_hash(meta: dict[str, Any]) -> str | None:
    """Compute (or pass through) a deterministic hash of evolve_step findings.

    Source priority:

    1. ``meta["findings"]`` (a list) is hashed verbatim. Synthetic test
       harnesses use this path.
    2. ``meta["findings_hash"]`` (a non-empty string) passes through unchanged.
       Producers can supply a precomputed hash to avoid re-serialization.
    3. ``meta["qa"]["differences"]`` and ``meta["qa"]["suggestions"]`` (lists)
       are combined into a stable mapping and hashed. The default
       ``EvolveStepHandler`` does not synthesize a top-level ``findings``
       field, so deriving the fingerprint from the QA verdict body is the
       only way oscillation detection can fire on the real in-process loop
       (issue #788 review-2).
    """
    findings = meta.get("findings")
    if isinstance(findings, list):
        return _hash_findings_payload(findings)
    precomputed = meta.get("findings_hash")
    if isinstance(precomputed, str) and precomputed:
        return precomputed
    qa = meta.get("qa")
    if isinstance(qa, dict):
        diffs = qa.get("differences")
        suggestions = qa.get("suggestions")
        diffs_list = diffs if isinstance(diffs, list) else None
        suggestions_list = suggestions if isinstance(suggestions, list) else None
        if diffs_list is not None or suggestions_list is not None:
            return _hash_findings_payload(
                {
                    "differences": diffs_list or [],
                    "suggestions": suggestions_list or [],
                }
            )
    return None


def _hash_findings_payload(payload: Any) -> str | None:
    """Stable JSON-then-sha256 hash for a findings payload."""
    try:
        serialized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _extract_grade(meta: dict[str, Any]) -> float | None:
    """Extract a numeric grade in [0.0, 1.0] from QA meta."""
    qa = meta.get("qa")
    if not isinstance(qa, dict):
        return None
    score = qa.get("score")
    if isinstance(score, bool):
        # Guard against bool subclass of int.
        score = None
    if isinstance(score, (int, float)):
        score_value = float(score)
        if 0.0 <= score_value <= 1.0:
            return score_value
    letter = qa.get("grade")
    if isinstance(letter, str):
        mapped = _LETTER_GRADE_MAP.get(letter.strip().upper())
        if mapped is not None:
            return mapped
    return None


def _is_oscillating(iterations: list[RalphIteration], window: int) -> bool:
    """Return True when the last ``window`` iterations share one findings_hash."""
    if window < 1 or len(iterations) < window:
        return False
    recent = iterations[-window:]
    first_hash = recent[0].findings_hash
    if first_hash is None:
        return False
    return all(item.findings_hash == first_hash for item in recent[1:])


def _is_grade_regressing(iterations: list[RalphIteration], window: int) -> bool:
    """Return True when the last ``window`` non-None grades strictly decrease."""
    if window < 2 or len(iterations) < window:
        return False
    recent = iterations[-window:]
    grades = [item.grade for item in recent]
    if any(grade is None for grade in grades):
        return False
    return all(grades[i] > grades[i + 1] for i in range(len(grades) - 1))
