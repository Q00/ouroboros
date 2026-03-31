"""Agent prompt loader -- single source of truth for all agent system prompts.

Loads agent .md files with a 3-tier resolution strategy:

1. ``OUROBOROS_AGENTS_DIR`` env var -- user-managed override directory
2. Project-root ``agents/`` directory -- canonical plugin agents
3. ``importlib.resources`` bundle    -- legacy packaged prompts (src/ouroboros/agents/)

Files in ``agents/`` may contain YAML frontmatter (``---`` delimited).
The frontmatter is stripped automatically -- only the body is returned
as the system prompt. This keeps project-root ``agents/`` as the
canonical prompt source for plugin mode and internal MCP mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import importlib.resources
import os
from pathlib import Path
import re

# ---------------------------------------------------------------------------
# Frontmatter stripping
# ---------------------------------------------------------------------------


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (``---`` delimited) from markdown content."""
    if not content.startswith("---"):
        return content
    # Find the closing ---
    end = content.find("---", 3)
    if end == -1:
        return content
    # Skip past the closing --- and any trailing newline
    body = content[end + 3:]
    return body.lstrip("\n")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _find_project_agents_dir() -> Path | None:
    """Find the project-root agents/ directory by walking up from this file."""
    # src/ouroboros/agents/loader.py → walk up to project root
    current = Path(__file__).resolve()
    for parent in current.parents:
        agents_dir = parent / "agents"
        if agents_dir.is_dir() and (agents_dir / "socratic-interviewer.md").exists():
            return agents_dir
    return None


@functools.lru_cache(maxsize=64)
def _resolve_agent_path(agent_name: str) -> Path | None:
    """Find an agent .md file using the 3-tier resolution strategy.

    Returns the first existing path, or ``None`` to signal that the
    caller should fall back to ``importlib.resources``.
    """
    filename = f"{agent_name}.md"

    # Tier 1: explicit env var override
    agents_dir = os.environ.get("OUROBOROS_AGENTS_DIR")
    if agents_dir:
        path = Path(agents_dir) / filename
        if path.exists():
            return path

    # Tier 2: project-root agents/ directory (plugin agents with frontmatter)
    project_agents = _find_project_agents_dir()
    if project_agents:
        path = project_agents / filename
        if path.exists():
            return path

    # Tier 3: fall through to importlib.resources
    return None


# ---------------------------------------------------------------------------
# Core loading
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def load_agent_prompt(agent_name: str) -> str:
    """Load the body content of an agent .md file (frontmatter stripped).

    Args:
        agent_name: File stem, e.g. ``"socratic-interviewer"``.

    Returns:
        Markdown body text (YAML frontmatter removed if present).

    Raises:
        FileNotFoundError: If the agent .md cannot be found anywhere.
    """
    path = _resolve_agent_path(agent_name)
    if path is not None:
        content = path.read_text(encoding="utf-8")
        return _strip_frontmatter(content)

    # Bundled fallback: try the force-included prompts directory first,
    # then legacy ouroboros.agents package for backward compat.
    for pkg in ("ouroboros.agents.prompts", "ouroboros.agents"):
        try:
            package = importlib.resources.files(pkg)
            resource = package.joinpath(f"{agent_name}.md")
            content = resource.read_text(encoding="utf-8")
            return _strip_frontmatter(content)
        except (FileNotFoundError, TypeError, ModuleNotFoundError):
            continue

    raise FileNotFoundError(
        f"Agent prompt not found: {agent_name}.md "
        f"(searched OUROBOROS_AGENTS_DIR, project agents/, and ouroboros.agents package)"
    )


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
