"""Agent prompt loader -- single source of truth for all agent system prompts.

Loads agent .md files with a 3-tier resolution strategy:

1. ``OUROBOROS_AGENTS_DIR`` env var  -- user-editable plugin agents
2. ``agents/`` (CWD)               -- plugin / developer mode
3. ``.claude-plugin/agents/`` (CWD) -- legacy fallback
4. ``importlib.resources`` bundle   -- installed-package fallback

This allows plugin users to customise agent behaviour by editing
the ``.md`` files in their plugin directory.  Changes take effect
after an MCP server restart.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import importlib.resources
import os
from pathlib import Path
import re

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _resolve_agent_path(agent_name: str) -> Path | None:
    """Find an agent .md file using the 3-tier resolution strategy.

    Returns the first existing path, or ``None`` to signal that the
    caller should fall back to ``importlib.resources``.

    Cached to prevent repeated filesystem checks and to stabilize
    path resolution even if CWD changes during process lifetime.
    """
    filename = f"{agent_name}.md"

    # Tier 1: explicit env var (plugin install)
    agents_dir = os.environ.get("OUROBOROS_AGENTS_DIR")
    if agents_dir:
        path = Path(agents_dir) / filename
        if path.exists():
            return path

    # Tier 2: CWD-relative agents/ (plugin convention)
    cwd_path = Path.cwd() / "agents" / filename
    if cwd_path.exists():
        return cwd_path

    # Tier 3: CWD-relative .claude-plugin/agents/ (legacy fallback)
    legacy_path = Path.cwd() / ".claude-plugin/agents" / filename
    if legacy_path.exists():
        return legacy_path

    # Tier 4: fall through to importlib.resources
    return None


# ---------------------------------------------------------------------------
# Core loading
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def load_agent_prompt(agent_name: str) -> str:
    """Load the full markdown content for an agent.

    Args:
        agent_name: File stem, e.g. ``"socratic-interviewer"``.

    Returns:
        Full markdown text.

    Raises:
        FileNotFoundError: If the agent .md cannot be found anywhere.
    """
    path = _resolve_agent_path(agent_name)
    if path is not None:
        return path.read_text(encoding="utf-8")

    # Bundled fallback
    package = importlib.resources.files("ouroboros.agents")
    resource = package.joinpath(f"{agent_name}.md")
    try:
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        raise FileNotFoundError(
            f"Agent prompt not found: {agent_name}.md "
            f"(searched OUROBOROS_AGENTS_DIR, agents/, "
            f".claude-plugin/agents/, and ouroboros.agents package)"
        ) from None


def clear_cache() -> None:
    """Clear the agent prompt cache. Useful for testing and plugin reload."""
    load_agent_prompt.cache_clear()
    _resolve_agent_path.cache_clear()


def load_agent_section(agent_name: str, section: str) -> str:
    """Load a specific ``## <section>`` from an agent .md file.

    Args:
        agent_name: File stem.
        section: Heading text (case-insensitive), e.g. ``"YOUR APPROACH"``.

    Raises:
        KeyError: If the section heading is not found.
    """
    content = load_agent_prompt(agent_name)
    return extract_section(content, section)


# ---------------------------------------------------------------------------
# Section / list parsing utilities
# ---------------------------------------------------------------------------


def extract_section(content: str, section: str) -> str:
    """Extract everything between ``## <section>`` and the next ``##``."""
    lines = content.split("\n")
    pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.IGNORECASE)

    start: int | None = None
    for i, line in enumerate(lines):
        if pattern.match(line.strip()):
            start = i + 1
            break
    if start is None:
        raise KeyError(f"Section '## {section}' not found")

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("## "):
            end = i
            break

    return "\n".join(lines[start:end]).strip()


def extract_list_items(section_content: str) -> tuple[str, ...]:
    """Extract ``- item`` bullet points from section text."""
    items: list[str] = []
    for line in section_content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return tuple(items)


def _extract_numbered_items(content: str) -> tuple[str, ...]:
    """Extract numbered items from markdown.

    Handles two formats:
    - ``### N. Title`` with optional body → ``"N. Title"``
    - ``N. Text`` plain numbered list   → ``"N. Text"``
    """
    items: list[str] = []

    # Try ### N. Title format first
    current_num: str | None = None
    current_title: str = ""
    for line in content.split("\n"):
        stripped = line.strip()
        match = re.match(r"^###\s+(\d+)\.\s+(.+)$", stripped)
        if match:
            if current_num is not None:
                items.append(f"{current_num}. {current_title}")
            current_num = match.group(1)
            current_title = match.group(2)
    if current_num is not None:
        items.append(f"{current_num}. {current_title}")

    if items:
        return tuple(items)

    # Fallback: plain numbered list
    for line in content.split("\n"):
        match = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
        if match:
            items.append(f"{match.group(1)}. {match.group(2)}")

    return tuple(items)


# ---------------------------------------------------------------------------
# Persona prompt data (for lateral thinking agents)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaPromptData:
    """Parsed data from a lateral thinking persona .md file."""

    system_prompt: str
    approach_instructions: tuple[str, ...]
    question_templates: tuple[str, ...]


def load_persona_prompt_data(agent_name: str) -> PersonaPromptData:
    """Load and parse a lateral thinking persona .md file.

    Extracts:
    - Opening paragraph (between ``#`` title and first ``##``) → *system_prompt*
    - ``## YOUR APPROACH`` → *approach_instructions*
    - ``## YOUR QUESTIONS`` → *question_templates*
    """
    content = load_agent_prompt(agent_name)

    # --- system_prompt: text between title and first ## ---
    lines = content.split("\n")
    philosophy_lines: list[str] = []
    past_title = False
    for line in lines:
        if line.startswith("# ") and not past_title:
            past_title = True
            continue
        if line.startswith("## "):
            break
        if past_title and line.strip():
            philosophy_lines.append(line.strip())
    system_prompt = " ".join(philosophy_lines)

    # --- approach_instructions ---
    try:
        approach_section = extract_section(content, "YOUR APPROACH")
        approach_instructions = _extract_numbered_items(approach_section)
    except KeyError:
        approach_instructions = ()

    # --- question_templates ---
    try:
        questions_section = extract_section(content, "YOUR QUESTIONS")
        question_templates = extract_list_items(questions_section)
    except KeyError:
        question_templates = ()

    return PersonaPromptData(
        system_prompt=system_prompt,
        approach_instructions=approach_instructions,
        question_templates=question_templates,
    )
