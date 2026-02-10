"""Skill documentation generator.

This module provides:
- Auto-generate help text from SKILL.md
- List available skills with metadata
- Show skill details (description, triggers, usage)
- Format output for CLI/TUI display
- Support for filtering by mode/type
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from ouroboros.plugin.skills.registry import (
    SkillMetadata,
    SkillMode,
    SkillRegistry,
    get_registry,
)

log = structlog.get_logger()


class OutputFormat(Enum):
    """Output format for documentation."""
    TEXT = "text"  # Plain text
    MARKDOWN = "markdown"  # Markdown format
    TABLE = "table"  # Table format
    RICH = "rich"  # Rich text with formatting


@dataclass
class SkillDocumentation:
    """Documentation for a skill.

    Attributes:
        name: Skill name.
        description: Brief description.
        usage: Usage examples.
        triggers: Trigger keywords.
        magic_prefixes: Magic prefixes.
        mode: Execution mode.
        version: Skill version.
        raw_content: Raw SKILL.md content.
    """
    name: str
    description: str
    usage: str
    triggers: tuple[str, ...]
    magic_prefixes: tuple[str, ...]
    mode: SkillMode
    version: str
    raw_content: str


class SkillDocumentationGenerator:
    """Generates documentation from skill specifications.

    The generator reads SKILL.md files and produces formatted
    documentation suitable for CLI help, TUI display, or
    external documentation.
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        """Initialize the documentation generator.

        Args:
            registry: Optional skill registry. Uses global singleton if not provided.
        """
        self._registry = registry or get_registry()

    def generate_all(
        self,
        mode: SkillMode | None = None,
        format: OutputFormat = OutputFormat.TEXT,
    ) -> str:
        """Generate documentation for all skills.

        Args:
            mode: Optional filter by skill mode.
            format: Output format.

        Returns:
            Formatted documentation string.
        """
        skills = self._registry.get_all_metadata()

        # Filter by mode if specified
        if mode:
            skills = {
                name: metadata
                for name, metadata in skills.items()
                if metadata.mode == mode
            }

        if format == OutputFormat.TEXT:
            return self._format_text_all(skills)
        elif format == OutputFormat.MARKDOWN:
            return self._format_markdown_all(skills)
        elif format == OutputFormat.TABLE:
            return self._format_table_all(skills)
        else:
            return self._format_text_all(skills)

    def generate_skill(
        self,
        skill_name: str,
        format: OutputFormat = OutputFormat.TEXT,
    ) -> str | None:
        """Generate documentation for a specific skill.

        Args:
            skill_name: Name of the skill.
            format: Output format.

        Returns:
            Formatted documentation string, or None if skill not found.
        """
        skill = self._registry.get_skill(skill_name)
        if not skill:
            return None

        doc = self._create_documentation(skill)

        if format == OutputFormat.TEXT:
            return self._format_text_skill(doc)
        elif format == OutputFormat.MARKDOWN:
            return self._format_markdown_skill(doc)
        elif format == OutputFormat.TABLE:
            return self._format_table_row(doc)
        else:
            return self._format_text_skill(doc)

    def list_skills(
        self,
        mode: SkillMode | None = None,
        include_description: bool = True,
    ) -> str:
        """List all available skills.

        Args:
            mode: Optional filter by skill mode.
            include_description: Whether to include descriptions.

        Returns:
            Formatted list of skills.
        """
        skills = self._registry.get_all_metadata()

        lines = []
        lines.append(f"Available Skills ({len(skills)} total):")
        lines.append("")

        for name, metadata in sorted(skills.items()):
            if mode and metadata.mode != mode:
                continue

            mode_indicator = "MCP" if metadata.mode == SkillMode.MCP else "Plugin"

            if include_description:
                desc = metadata.description or "No description"
                lines.append(f"  {name:20} [{mode_indicator:6}] {desc}")
            else:
                lines.append(f"  {name} [{mode_indicator}]")

        return "\n".join(lines)

    def _create_documentation(self, skill) -> SkillDocumentation:
        """Create skill documentation from a skill instance.

        Args:
            skill: The skill instance.

        Returns:
            SkillDocumentation object.
        """
        metadata = skill.metadata
        spec = skill.spec

        # Extract usage section
        sections = spec.get("sections", {})
        usage = sections.get("usage", "")
        if not usage:
            usage = sections.get("how_it_works", "")

        return SkillDocumentation(
            name=metadata.name,
            description=metadata.description,
            usage=usage,
            triggers=metadata.trigger_keywords,
            magic_prefixes=metadata.magic_prefixes,
            mode=metadata.mode,
            version=metadata.version,
            raw_content=spec.get("raw", ""),
        )

    def _format_text_all(self, skills: dict[str, SkillMetadata]) -> str:
        """Format all skills as plain text.

        Args:
            skills: Dictionary of skill metadata.

        Returns:
            Formatted text string.
        """
        lines = []
        lines.append("=" * 60)
        lines.append("Ouroboros Skills Reference")
        lines.append("=" * 60)
        lines.append("")

        # Group by mode
        plugin_skills = []
        mcp_skills = []

        for name, metadata in sorted(skills.items()):
            if metadata.mode == SkillMode.MCP:
                mcp_skills.append((name, metadata))
            else:
                plugin_skills.append((name, metadata))

        if plugin_skills:
            lines.append("PLUGIN MODE SKILLS (Available Immediately)")
            lines.append("-" * 60)
            for name, metadata in plugin_skills:
                desc = metadata.description or "No description"
                lines.append(f"  {name:20} - {desc}")
            lines.append("")

        if mcp_skills:
            lines.append("MCP MODE SKILLS (Requires Setup)")
            lines.append("-" * 60)
            for name, metadata in mcp_skills:
                desc = metadata.description or "No description"
                lines.append(f"  {name:20} - {desc}")
            lines.append("")

        return "\n".join(lines)

    def _format_markdown_all(self, skills: dict[str, SkillMetadata]) -> str:
        """Format all skills as Markdown.

        Args:
            skills: Dictionary of skill metadata.

        Returns:
            Formatted Markdown string.
        """
        lines = []
        lines.append("# Ouroboros Skills Reference\n")

        # Group by mode
        for mode in [SkillMode.PLUGIN, SkillMode.MCP]:
            mode_name = "Plugin Mode" if mode == SkillMode.PLUGIN else "MCP Mode"
            mode_desc = (
                "Available without setup"
                if mode == SkillMode.PLUGIN
                else "Requires `ooo setup`"
            )

            lines.append(f"## {mode_name} Skills")
            lines.append(f"*{mode_desc}*\n")

            for name, metadata in sorted(skills.items()):
                if metadata.mode != mode:
                    continue

                lines.append(f"### `{name}`")
                lines.append(f"{metadata.description or 'No description'}\n")

                if metadata.magic_prefixes:
                    prefixes = ", ".join(f"`{p}`" for p in metadata.magic_prefixes[:3])
                    lines.append(f"**Magic prefixes:** {prefixes}\n")

                if metadata.trigger_keywords:
                    triggers = ", ".join(f'"{k}"' for k in metadata.trigger_keywords[:3])
                    lines.append(f"**Triggers:** {triggers}\n")

                lines.append("---\n")

        return "\n".join(lines)

    def _format_table_all(self, skills: dict[str, SkillMetadata]) -> str:
        """Format all skills as a table.

        Args:
            skills: Dictionary of skill metadata.

        Returns:
            Formatted table string.
        """
        lines = []
        lines.append(f"{'Skill':<20} {'Mode':<8} {'Description'}")
        lines.append("-" * 80)

        for name, metadata in sorted(skills.items()):
            mode_str = "MCP" if metadata.mode == SkillMode.MCP else "Plugin"
            desc = (metadata.description or "")[:40]
            lines.append(f"{name:<20} {mode_str:<8} {desc}")

        return "\n".join(lines)

    def _format_text_skill(self, doc: SkillDocumentation) -> str:
        """Format a single skill as plain text.

        Args:
            doc: The skill documentation.

        Returns:
            Formatted text string.
        """
        lines = []
        lines.append("=" * 60)
        lines.append(f"Skill: {doc.name}")
        lines.append(f"Version: {doc.version}")
        lines.append(f"Mode: {doc.mode.value.upper()}")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"Description: {doc.description}")
        lines.append("")

        if doc.magic_prefixes:
            lines.append("Magic Prefixes:")
            for prefix in doc.magic_prefixes:
                lines.append(f"  {prefix}")
            lines.append("")

        if doc.triggers:
            lines.append("Trigger Keywords:")
            for trigger in doc.triggers:
                lines.append(f"  {trigger}")
            lines.append("")

        if doc.usage:
            lines.append("Usage:")
            lines.append(doc.usage)

        return "\n".join(lines)

    def _format_markdown_skill(self, doc: SkillDocumentation) -> str:
        """Format a single skill as Markdown.

        Args:
            doc: The skill documentation.

        Returns:
            Formatted Markdown string.
        """
        lines = []
        lines.append(f"# `{doc.name}`")
        lines.append(f"*Version {doc.version} | {doc.mode.value.upper()} mode*\n")
        lines.append(f"{doc.description}\n")

        if doc.magic_prefixes:
            lines.append("## Magic Prefixes")
            for prefix in doc.magic_prefixes:
                lines.append(f"- `{prefix}`")
            lines.append("")

        if doc.triggers:
            lines.append("## Trigger Keywords")
            for trigger in doc.triggers:
                lines.append(f"- {trigger}")
            lines.append("")

        if doc.usage:
            lines.append("## Usage")
            lines.append(doc.usage)

        return "\n".join(lines)

    def _format_table_row(self, doc: SkillDocumentation) -> str:
        """Format a single skill as a table row.

        Args:
            doc: The skill documentation.

        Returns:
            Formatted table row string.
        """
        mode_str = "MCP" if doc.mode == SkillMode.MCP else "Plugin"
        desc = (doc.description or "")[:50]
        return f"{doc.name:<20} {mode_str:<8} {desc}"


def generate_skill_docs(
    skill_name: str | None = None,
    mode: SkillMode | None = None,
    format: OutputFormat = OutputFormat.TEXT,
    registry: SkillRegistry | None = None,
) -> str:
    """Convenience function to generate skill documentation.

    Args:
        skill_name: Optional specific skill name. If None, generates for all skills.
        mode: Optional filter by skill mode.
        format: Output format.
        registry: Optional skill registry.

    Returns:
        Formatted documentation string.
    """
    generator = SkillDocumentationGenerator(registry)

    if skill_name:
        result = generator.generate_skill(skill_name, format)
        return result or f"Skill not found: {skill_name}"

    return generator.generate_all(mode, format)


def list_available_skills(
    mode: SkillMode | None = None,
    registry: SkillRegistry | None = None,
) -> str:
    """Convenience function to list available skills.

    Args:
        mode: Optional filter by skill mode.
        registry: Optional skill registry.

    Returns:
        Formatted list of skills.
    """
    generator = SkillDocumentationGenerator(registry)
    return generator.list_skills(mode)
