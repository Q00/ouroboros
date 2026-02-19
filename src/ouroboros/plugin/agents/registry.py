"""Agent Registry for dynamic agent discovery and registration.

This module provides:
- Agent discovery from .claude-plugin/agents/
- Built-in agent specifications
- Agent composition and runtime creation
- Role-based agent lookup

Usage:
    registry = AgentRegistry()
    await registry.discover_custom()

    # Get agent by name
    agent = registry.get_agent("executor")

    # Get agents by role
    analysts = registry.get_agents_by_role(AgentRole.ANALYSIS)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


# =============================================================================
# Agent Role Enum
# =============================================================================


class AgentRole(Enum):
    """Agent role categories for role-based routing.

    Roles determine the default model preference and tool set.
    """

    ANALYSIS = "analysis"  # explore, analyst, debugger
    PLANNING = "planning"  # planner, critic
    EXECUTION = "execution"  # executor, deep-executor
    REVIEW = "review"  # all code-reviewers
    DOMAIN = "domain"  # dependency-expert, test-engineer
    PRODUCT = "product"  # product-manager, ux-researcher
    COORDINATION = "coordination"  # architect, vision


# =============================================================================
# Agent Specification
# =============================================================================


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Specification for a dynamic agent.

    Attributes:
        name: Unique agent identifier.
        role: Agent role category.
        model_preference: Default model tier (haiku, sonnet, opus).
        system_prompt: System prompt for the agent.
        tools: List of tools the agent can use.
        capabilities: Tuple of capability keywords.
        description: Human-readable description.
    """

    name: str
    role: AgentRole
    model_preference: str
    system_prompt: str
    tools: list[str]
    capabilities: tuple[str, ...]
    description: str


# =============================================================================
# Built-in Agent Specifications
# =============================================================================


def _get_executor_prompt() -> str:
    return """You are Executor. Your mission is to implement code changes precisely as specified.

## Core Principles
- Work ALONE. Do not spawn sub-agents.
- Prefer the smallest viable change. Do not broaden scope beyond requested behavior.
- Do not introduce new abstractions for single-use logic.
- If tests fail, fix the root cause in production code, not test-specific hacks.

## Success Criteria
- The requested change is implemented with the smallest viable diff
- All modified files pass type checking with zero errors
- Build and tests pass
- No new abstractions introduced for single-use logic

## Output Format
After completing your work, report:

## Changes Made
- `file.ts:42-55`: [what changed and why]

## Verification
- Build: [command] -> [pass/fail]
- Tests: [command] -> [X passed, Y failed]
- Diagnostics: [N errors, M warnings]

## Summary
[1-2 sentences on what was accomplished]
"""


def _get_planner_prompt() -> str:
    return """You are Planner. Your role is to create detailed execution plans for tasks.

## Planning Process
1. Analyze the task requirements and acceptance criteria
2. Identify all files that need to be read/modified
3. Plan the sequence of changes
4. Identify potential risks and edge cases
5. Create a step-by-step execution plan

## Output Format
Provide a structured plan with:
- Overview of the approach
- Required files to examine
- Step-by-step execution steps
- Risk factors and mitigation strategies
- Acceptance criteria checklist
"""


def _get_verifier_prompt() -> str:
    return """You are Verifier. Your role is to verify that implementation meets acceptance criteria.

## Verification Process
1. Review the changes made
2. Check each acceptance criterion is satisfied
3. Run tests to ensure functionality
4. Check for regressions or side effects
5. Validate code quality and conventions

## Output Format
Provide verification results with:
- Acceptance criteria status (pass/fail for each)
- Test results summary
- Code quality assessment
- Any issues found
- Final pass/fail determination
"""


def _get_analyst_prompt() -> str:
    return """You are Analyst. Your role is to analyze requirements and identify edge cases.

## Analysis Process
1. Parse the user's request for explicit requirements
2. Identify implicit requirements and assumptions
3. Consider edge cases and error conditions
4. Check for conflicts or ambiguities
5. Propose clarifying questions if needed

## Output Format
Provide analysis with:
- Explicit requirements list
- Implicit requirements identified
- Edge cases to consider
- Ambiguities requiring clarification
- Recommended acceptance criteria
"""


# Built-in agents registry
BUILTIN_AGENTS: dict[str, AgentSpec] = {
    "executor": AgentSpec(
        name="executor",
        role=AgentRole.EXECUTION,
        model_preference="sonnet",
        system_prompt=_get_executor_prompt(),
        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        capabilities=("code", "implementation", "refactoring"),
        description="General-purpose code execution with minimal scope changes",
    ),
    "planner": AgentSpec(
        name="planner",
        role=AgentRole.PLANNING,
        model_preference="sonnet",
        system_prompt=_get_planner_prompt(),
        tools=["Read", "Glob", "Grep"],
        capabilities=("planning", "analysis", "design"),
        description="Creates detailed execution plans for tasks",
    ),
    "verifier": AgentSpec(
        name="verifier",
        role=AgentRole.REVIEW,
        model_preference="haiku",
        system_prompt=_get_verifier_prompt(),
        tools=["Read", "Bash", "Grep"],
        capabilities=("verification", "testing", "quality"),
        description="Verifies implementation meets acceptance criteria",
    ),
    "analyst": AgentSpec(
        name="analyst",
        role=AgentRole.ANALYSIS,
        model_preference="haiku",
        system_prompt=_get_analyst_prompt(),
        tools=["Read", "Glob", "Grep"],
        capabilities=("analysis", "requirements", "investigation"),
        description="Analyzes requirements and identifies edge cases",
    ),
}


# =============================================================================
# Agent Registry
# =============================================================================


class AgentRegistry:
    """Dynamic agent registry with runtime composition.

    The registry manages:
    - Built-in agents (always available)
    - Custom agents loaded from .claude-plugin/agents/
    - Agent composition (creating new agents from existing ones)
    - Role-based agent lookup

    Example:
        registry = AgentRegistry()

        # Discover custom agents
        await registry.discover_custom()

        # Get agent by name
        executor = registry.get_agent("executor")

        # Get agents by role
        analysts = registry.get_agents_by_role(AgentRole.ANALYSIS)

        # Compose a new agent
        custom = registry.compose_agent(
            name="my-executor",
            base_agent="executor",
            overrides={"model_preference": "opus"}
        )
    """

    AGENT_DIR = Path(".claude-plugin/agents")

    def __init__(self) -> None:
        """Initialize the agent registry."""
        self._custom_agents: dict[str, AgentSpec] = {}
        self._role_index: dict[AgentRole, set[str]] = self._build_role_index(BUILTIN_AGENTS)

    def _build_role_index(self, agents: dict[str, AgentSpec]) -> dict[AgentRole, set[str]]:
        """Build role -> agent names index."""
        index: dict[AgentRole, set[str]] = {}
        for agent in agents.values():
            if agent.role not in index:
                index[agent.role] = set()
            index[agent.role].add(agent.name)
        return index

    async def discover_custom(self) -> dict[str, AgentSpec]:
        """Load custom agents from .claude-plugin/agents/.

        Scans the agent directory for .md files and parses them as agent specs.

        Returns:
            Dictionary of custom agent specs keyed by name.
        """
        if not self.AGENT_DIR.exists():
            log.info(
                "agents.registry.custom_dir_not_found",
                path=str(self.AGENT_DIR),
            )
            return {}

        discovered: dict[str, AgentSpec] = {}

        for agent_path in self.AGENT_DIR.glob("*.md"):
            try:
                spec = await self._parse_agent_md(agent_path)
                if spec:
                    self._custom_agents[spec.name] = spec
                    discovered[spec.name] = spec

                    # Update role index
                    if spec.role not in self._role_index:
                        self._role_index[spec.role] = set()
                    self._role_index[spec.role].add(spec.name)

                    log.info(
                        "agents.registry.custom_agent_loaded",
                        name=spec.name,
                        role=spec.role.value,
                        path=str(agent_path),
                    )
            except Exception as e:
                log.warning(
                    "agents.registry.custom_agent_parse_failed",
                    path=str(agent_path),
                    error=str(e),
                )

        log.info(
            "agents.registry.discovery_complete",
            total_custom=len(discovered),
            total_builtin=len(BUILTIN_AGENTS),
        )

        return discovered

    async def _parse_agent_md(self, path: Path) -> AgentSpec | None:
        """Parse agent specification from markdown file.

        Expected format:
            # Agent Name

            You are an agent description.

            ## Role
            Role description

            ## Capabilities
            - Capability 1
            - Capability 2

            ## Tools
            - Read
            - Write

            ## Model Preference
            sonnet (balanced)

        Args:
            path: Path to agent markdown file.

        Returns:
            AgentSpec or None if parsing fails.
        """
        content = path.read_text()
        lines = content.split("\n")

        # Parse title (first heading)
        name = path.stem
        for line in lines:
            if line.strip().startswith("# "):
                name = line.strip()[2:].strip()
                break

        # Extract sections
        current_section = ""
        sections: dict[str, list[str]] = {}

        for line in lines[1:]:  # Skip title
            if line.strip().startswith("## "):
                current_section = line.strip()[3:].strip().lower()
                sections[current_section] = []
            elif current_section and line.strip():
                sections[current_section].append(line.strip())

        # Build description
        role_text = sections.get("role", ["No description"])
        description = " ".join(role_text)

        # Parse capabilities
        capabilities_list = sections.get("capabilities", [])
        capabilities = tuple(cap.lstrip("- ").strip() for cap in capabilities_list if cap.strip())

        # Parse tools
        tools_list = sections.get("tools", [])
        tools = [
            tool.lstrip("- ").strip()
            for tool in tools_list
            if tool.strip() and not tool.strip().startswith("-")
        ]

        # Parse model preference
        model_section = sections.get("model preference", ["sonnet"])
        model_text = model_section[0].lower() if model_section else "sonnet"
        if "haiku" in model_text:
            model_preference = "haiku"
        elif "opus" in model_text:
            model_preference = "opus"
        else:
            model_preference = "sonnet"

        # Determine role from capabilities or defaults
        role = AgentRole.EXECUTION  # Default
        capabilities_lower = " ".join(capabilities).lower()
        if any(kw in capabilities_lower for kw in ["planning", "plan", "design", "architect"]):
            role = AgentRole.PLANNING
        elif any(
            kw in capabilities_lower for kw in ["analysis", "analyze", "investigate", "debug"]
        ):
            role = AgentRole.ANALYSIS
        elif any(kw in capabilities_lower for kw in ["review", "verify", "check", "audit"]):
            role = AgentRole.REVIEW

        return AgentSpec(
            name=name,
            role=role,
            model_preference=model_preference,
            system_prompt=f"You are {name}. {description}",
            tools=tools or list(BUILTIN_AGENTS["executor"].tools),
            capabilities=capabilities,
            description=description,
        )

    def get_agent(self, name: str) -> AgentSpec | None:
        """Get agent spec by name (built-in or custom).

        Args:
            name: Agent name to look up.

        Returns:
            AgentSpec if found, None otherwise.
        """
        return BUILTIN_AGENTS.get(name) or self._custom_agents.get(name)

    def get_agents_by_role(self, role: AgentRole) -> list[AgentSpec]:
        """Get all agents for a specific role.

        Args:
            role: Agent role to filter by.

        Returns:
            List of AgentSpec with the specified role.
        """
        names = self._role_index.get(role, set())
        agents: list[AgentSpec] = []

        for name in names:
            agent = self.get_agent(name)
            if agent:
                agents.append(agent)

        return agents

    def compose_agent(
        self,
        name: str,
        base_agent: str,
        overrides: dict[str, Any],
    ) -> AgentSpec:
        """Compose a new agent from existing one with overrides.

        Args:
            name: Name for the composed agent.
            base_agent: Name of the base agent to extend.
            overrides: Dictionary of fields to override.
                Supported keys: role, model_preference, system_prompt,
                tools, capabilities, description.

        Returns:
            New AgentSpec with overrides applied.

        Raises:
            ValueError: If base agent not found.
        """
        base = self.get_agent(base_agent)
        if not base:
            msg = f"Base agent {base_agent} not found"
            raise ValueError(msg)

        # Parse role override
        role_override = overrides.get("role")
        if role_override:
            if isinstance(role_override, str):
                role = AgentRole(role_override)
            else:
                role = role_override
        else:
            role = base.role

        return AgentSpec(
            name=name,
            role=role,
            model_preference=overrides.get("model", base.model_preference),
            system_prompt=overrides.get("system_prompt", base.system_prompt),
            tools=overrides.get("tools", list(base.tools)),
            capabilities=overrides.get("capabilities", base.capabilities),
            description=overrides.get("description", base.description),
        )

    def list_all_agents(self) -> dict[str, AgentSpec]:
        """List all available agents (built-in + custom).

        Returns:
            Dictionary mapping agent names to specs.
        """
        return {**BUILTIN_AGENTS, **self._custom_agents}


__all__ = [
    "AgentRegistry",
    "AgentRole",
    "AgentSpec",
    "BUILTIN_AGENTS",
]
