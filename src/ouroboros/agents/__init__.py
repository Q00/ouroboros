"""Agent prompt definitions -- single source of truth.

This package contains the canonical .md files defining all agent behaviors.
Both the Claude Code plugin and the Python MCP server read from here.

Usage::

    from ouroboros.agents.loader import load_agent_prompt, load_persona_prompt_data

    # Load full prompt
    prompt = load_agent_prompt("socratic-interviewer")

    # Load structured persona data
    data = load_persona_prompt_data("hacker")
"""

from ouroboros.agents.loader import (
    PersonaPromptData,
    load_agent_prompt,
    load_agent_section,
    load_persona_prompt_data,
)

__all__ = [
    "PersonaPromptData",
    "load_agent_prompt",
    "load_agent_section",
    "load_persona_prompt_data",
]
