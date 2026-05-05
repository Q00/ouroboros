"""MCP-owned Ralph loop runner.

This module is the first runtime-owned slice for issue #528.  It keeps
Ralph's multi-generation loop out of client-side skill pseudo-code by
running repeated ``evolve_step`` calls inside one background job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

_TERMINAL_SUCCESS_ACTIONS = frozenset({"converged"})
_TERMINAL_FAILURE_ACTIONS = frozenset({"failed", "interrupted", "exhausted", "stagnated"})


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


@dataclass(frozen=True, slots=True)
class RalphIteration:
    """One evolve_step iteration executed by Ralph."""

    generation: int | None
    action: str
    qa_verdict: str | None = None
    is_error: bool = False


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
        status = "failed"

        for iteration_index in range(1, config.max_generations + 1):
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

            result = await self.evolve_handler.handle(arguments)
            if result.is_err:
                raise RuntimeError(str(result.error))

            final_result = result.value
            action = str(final_result.meta.get("action", "unknown"))
            generation = _coerce_int(final_result.meta.get("generation"))
            qa_verdict = _extract_qa_verdict(final_result.meta)
            iterations.append(
                RalphIteration(
                    generation=generation,
                    action=action,
                    qa_verdict=qa_verdict,
                    is_error=final_result.is_error,
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

            # Gen 2+ reconstructs state from EventStore by lineage_id.
            seed_content = None
        else:
            if final_result is not None:
                status = "failed"
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
