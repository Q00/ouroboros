"""Execution strategies for different task types.

Strategies define tools, prompts, and activity mappings for
code, research, and analysis tasks. Each strategy is a pluggable
component that the OrchestratorRunner uses to customize execution.

Usage:
    from ouroboros.orchestrator.execution_strategy import get_strategy

    strategy = get_strategy("research")
    tools = strategy.get_tools()
    fragment = strategy.get_system_prompt_fragment()
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ouroboros.orchestrator.workflow_state import ActivityType


@runtime_checkable
class ExecutionStrategy(Protocol):
    """Protocol for execution strategies.

    Each strategy provides:
    - tools: What tools the agent can use
    - system prompt fragment: How the agent should behave
    - activity map: How tool usage maps to TUI activity types
    """

    def get_tools(self) -> list[str]:
        """Return list of tool names for this strategy."""
        ...

    def get_system_prompt_fragment(self) -> str:
        """Return the strategy-specific part of the system prompt."""
        ...

    def get_task_prompt_suffix(self) -> str:
        """Return the task prompt suffix for this strategy."""
        ...

    def get_activity_map(self) -> dict[str, ActivityType]:
        """Return tool → activity type mapping."""
        ...


class CodeStrategy:
    """Strategy for code development tasks (default)."""

    def get_tools(self) -> list[str]:
        return ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

    def get_system_prompt_fragment(self) -> str:
        return (
            "You are an autonomous coding agent executing a task "
            "for the Ouroboros workflow system.\n\n"
            "## Guidelines\n"
            "- Execute each acceptance criterion thoroughly\n"
            "- Use the available tools (Read, Edit, Bash, Glob, Grep) "
            "to accomplish tasks\n"
            "- Write clean, well-tested code following project conventions\n"
            "- Report progress clearly as you work\n"
            "- If you encounter blockers, explain them clearly"
        )

    def get_task_prompt_suffix(self) -> str:
        return (
            "Please execute each criterion in order, using the available "
            "tools to read, write, and modify code as needed.\n"
            "Report your progress and results for each criterion."
        )

    def get_activity_map(self) -> dict[str, ActivityType]:
        return {
            "Read": ActivityType.EXPLORING,
            "Glob": ActivityType.EXPLORING,
            "Grep": ActivityType.EXPLORING,
            "Edit": ActivityType.BUILDING,
            "Write": ActivityType.BUILDING,
            "Bash": ActivityType.TESTING,
        }


class ResearchStrategy:
    """Strategy for research and information gathering tasks.

    Outputs are saved as markdown documents. Agents focus on
    gathering, synthesizing, and organizing information.
    """

    def get_tools(self) -> list[str]:
        # Research uses file tools for reading/writing markdown reports
        # + relies on MCP tools for web search (added separately)
        return ["Read", "Write", "Bash", "Glob", "Grep"]

    def get_system_prompt_fragment(self) -> str:
        return (
            "You are an autonomous research agent conducting systematic "
            "information gathering and analysis.\n\n"
            "## Guidelines\n"
            "- Gather information from available sources thoroughly\n"
            "- Cross-reference multiple sources for accuracy\n"
            "- Synthesize findings into clear, structured markdown documents\n"
            "- Save research outputs as .md files in the docs/ or output/ directory\n"
            "- Cite sources and provide references where applicable\n"
            "- Report progress and key findings as you work\n"
            "- If you encounter blockers, explain them clearly"
        )

    def get_task_prompt_suffix(self) -> str:
        return (
            "Please research each criterion systematically. "
            "Save your findings as markdown documents.\n"
            "Report your progress and key discoveries for each criterion."
        )

    def get_activity_map(self) -> dict[str, ActivityType]:
        return {
            "Read": ActivityType.EXPLORING,
            "Glob": ActivityType.EXPLORING,
            "Grep": ActivityType.EXPLORING,
            "Write": ActivityType.BUILDING,
            "Bash": ActivityType.EXPLORING,
        }


class AnalysisStrategy:
    """Strategy for analytical reasoning and perspective tasks.

    Focuses on structured thinking, comparison, and insight generation.
    Outputs are structured markdown documents.
    """

    def get_tools(self) -> list[str]:
        return ["Read", "Write", "Bash", "Glob", "Grep"]

    def get_system_prompt_fragment(self) -> str:
        return (
            "You are an autonomous analytical agent performing "
            "structured analysis and reasoning.\n\n"
            "## Guidelines\n"
            "- Read and understand the subject matter thoroughly\n"
            "- Apply structured analytical frameworks\n"
            "- Consider multiple perspectives and trade-offs\n"
            "- Document your analytical process clearly\n"
            "- Present findings with supporting evidence in markdown\n"
            "- Save analysis outputs as .md files\n"
            "- Report progress as you work"
        )

    def get_task_prompt_suffix(self) -> str:
        return (
            "Please analyze each criterion systematically. "
            "Save your analysis as markdown documents.\n"
            "Report your reasoning and conclusions for each criterion."
        )

    def get_activity_map(self) -> dict[str, ActivityType]:
        return {
            "Read": ActivityType.EXPLORING,
            "Glob": ActivityType.EXPLORING,
            "Grep": ActivityType.EXPLORING,
            "Write": ActivityType.BUILDING,
            "Bash": ActivityType.TESTING,
        }


# Strategy registry
_STRATEGY_REGISTRY: dict[str, ExecutionStrategy] = {
    "code": CodeStrategy(),
    "research": ResearchStrategy(),
    "analysis": AnalysisStrategy(),
}


def get_strategy(task_type: str = "code") -> ExecutionStrategy:
    """Get execution strategy for a task type.

    Args:
        task_type: Type of task ("code", "research", "analysis").

    Returns:
        ExecutionStrategy instance.

    Raises:
        ValueError: If task_type is not recognized.
    """
    strategy = _STRATEGY_REGISTRY.get(task_type.lower())
    if strategy is None:
        valid = ", ".join(sorted(_STRATEGY_REGISTRY.keys()))
        msg = f"Unknown task_type: {task_type!r}. Valid types: {valid}"
        raise ValueError(msg)
    return strategy


def register_strategy(task_type: str, strategy: ExecutionStrategy) -> None:
    """Register a custom execution strategy.

    Args:
        task_type: Type identifier for the strategy.
        strategy: ExecutionStrategy instance.
    """
    _STRATEGY_REGISTRY[task_type.lower()] = strategy


__all__ = [
    "AnalysisStrategy",
    "CodeStrategy",
    "ExecutionStrategy",
    "ResearchStrategy",
    "get_strategy",
    "register_strategy",
]
