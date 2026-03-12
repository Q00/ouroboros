"""Skill Registry with auto-discovery and hot-reload.

This module provides a skill registry that:
- Auto-discovers skills from .claude-plugin/skills/ directory
- Hot-reloads skills when SKILL.md files change
- Validates skill metadata from frontmatter
- Provides trigger pattern matching for magic keywords
- Maintains thread-safe skill storage with version tracking
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from threading import RLock
from typing import Any

import structlog
import yaml

from ouroboros.core.types import Result

# Optional file watching support
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

    # Create stub classes for type hints
    class FileSystemEventHandler:  # type: ignore
        pass

    class Observer:  # type: ignore
        pass


log = structlog.get_logger()
_MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SkillMode(Enum):
    """Skill execution mode."""

    PLUGIN = "plugin"  # Works without MCP server
    MCP = "mcp"  # Requires MCP server


@dataclass(frozen=True)
class SkillMetadata:
    """Metadata for a discovered skill.

    Attributes:
        name: Skill name (directory name)
        path: Path to skill directory
        trigger_keywords: Natural language triggers
        magic_prefixes: Magic prefixes like "ooo:", "ouroboros:"
        description: Brief description from SKILL.md
        version: Skill version
        mode: Execution mode (plugin or mcp)
        requires_mcp: Whether MCP server is required
        intercept_eligible: Whether exact-prefix interception can dispatch to MCP
        mcp_tool: Backing MCP tool name for exact-prefix interception
        mcp_args: MCP argument template mapping from frontmatter
        intercept_validation_error: Validation failure reason when not eligible
    """

    name: str
    path: Path
    trigger_keywords: tuple[str, ...] = ()
    magic_prefixes: tuple[str, ...] = ()
    description: str = ""
    version: str = "1.0.0"
    mode: SkillMode = SkillMode.PLUGIN
    requires_mcp: bool = False
    intercept_eligible: bool = False
    mcp_tool: str | None = None
    mcp_args: dict[str, Any] | None = None
    intercept_validation_error: str | None = None


@dataclass
class SkillInstance:
    """Runtime skill instance.

    Attributes:
        metadata: Skill metadata
        spec: Full parsed SKILL.md content
        last_modified: File modification timestamp
        is_loaded: Whether skill is currently loaded
    """

    metadata: SkillMetadata
    spec: dict[str, Any]
    last_modified: float
    is_loaded: bool = True


# File watcher implementation (only when watchdog is available)
if WATCHDOG_AVAILABLE:

    class SkillFileWatcher(FileSystemEventHandler):
        """File system watcher for hot-reload support.

        Monitors the skills directory for changes to SKILL.md files
        and triggers reload when files are modified.
        """

        def __init__(self, registry: SkillRegistry) -> None:
            """Initialize the watcher.

            Args:
                registry: The skill registry to notify of changes.
            """
            super().__init__()
            self._registry = registry
            self._loop = asyncio.new_event_loop()
            self._loop_thread: Any = None

        def _schedule_reload(self, skill_path: Path) -> None:
            """Schedule a reload task in the event loop.

            Args:
                skill_path: Path to the modified SKILL.md file.
            """
            if self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._registry.reload_skill(skill_path),
                    self._loop,
                )
            else:
                # Fallback: create new task
                asyncio.create_task(self._registry.reload_skill(skill_path))

        def on_modified(self, event: Any) -> None:
            """Handle file modification events.

            Args:
                event: Watchdog file system event.
            """
            if event.is_directory:
                return

            src_path = Path(event.src_path)
            if src_path.name == "SKILL.md":
                log.info("plugin.skill.file_modified", path=str(src_path))
                self._schedule_reload(src_path.parent)
else:
    # Stub class when watchdog is not available
    class SkillFileWatcher:  # type: ignore
        """Stub file watcher when watchdog is not available."""

        def __init__(self, registry: SkillRegistry) -> None:
            self._registry = registry


class SkillRegistry:
    """Auto-discovering, hot-reloading skill registry.

    The registry scans .claude-plugin/skills/ for skill definitions,
    parses SKILL.md frontmatter, and maintains an index of
    trigger patterns for fast matching.

    Features:
        - Zero-config discovery
        - Hot-reload without restart
        - Thread-safe operations
        - Fast keyword indexing
    """

    DEFAULT_SKILL_DIR = Path("skills")

    def __init__(self, skill_dir: Path | None = None) -> None:
        """Initialize the skill registry.

        Args:
            skill_dir: Path to skills directory. Defaults to .claude-plugin/skills/.
        """
        self._skill_dir = skill_dir or self.DEFAULT_SKILL_DIR
        self._skills: dict[str, SkillInstance] = {}
        self._lock = RLock()
        self._trigger_index: dict[str, set[str]] = {}
        self._prefix_index: dict[str, set[str]] = {}
        self._observer: Observer | None = None
        self._watcher: SkillFileWatcher | None = None
        self._discovery_complete = False

    @property
    def skill_dir(self) -> Path:
        """Get the skill directory path."""
        return self._skill_dir

    @property
    def is_watching(self) -> bool:
        """Check if file watcher is active."""
        if not WATCHDOG_AVAILABLE:
            return False
        return self._observer is not None and self._observer.is_alive()

    async def discover_all(self) -> dict[str, SkillMetadata]:
        """Discover and load all skills from the skills directory.

        Scans the skills directory for SKILL.md files, parses each file,
        and builds the trigger/prefix indexes for fast matching.

        Returns:
            Dictionary mapping skill names to their metadata.
        """
        if not self._skill_dir.exists():
            log.warning("plugin.skill_directory_not_found", path=str(self._skill_dir))
            return {}

        log.info("plugin.skill.discovery_start", path=str(self._skill_dir))

        for skill_path in self._skill_dir.glob("*/SKILL.md"):
            await self._load_skill(skill_path)

        # Start file watcher for hot-reload
        self._start_watcher()

        self._discovery_complete = True
        log.info("plugin.skill.discovery_complete", count=len(self._skills))

        return {name: instance.metadata for name, instance in self._skills.items()}

    def get_all_metadata(self) -> dict[str, SkillMetadata]:
        """Get metadata for all loaded skills.

        Returns:
            Dictionary mapping skill names to their metadata.
        """
        with self._lock:
            return {
                name: instance.metadata
                for name, instance in self._skills.items()
                if instance.is_loaded
            }

    def get_skill(self, name: str) -> SkillInstance | None:
        """Get a skill instance by name.

        Args:
            name: The skill name.

        Returns:
            The skill instance if found and loaded, None otherwise.
        """
        with self._lock:
            instance = self._skills.get(name)
            if instance and instance.is_loaded:
                return instance
        return None

    def find_by_magic_prefix(self, prefix: str) -> list[SkillMetadata]:
        """Find skills that match a magic prefix.

        Args:
            prefix: The magic prefix to match (e.g., "ooo", "ouroboros:").

        Returns:
            List of matching skill metadata, sorted by specificity.
        """
        with self._lock:
            # Direct prefix match
            if prefix in self._prefix_index:
                skill_names = self._prefix_index[prefix]
                return [
                    self._skills[name].metadata
                    for name in skill_names
                    if self._skills[name].is_loaded
                ]

            # Substring match for shorter prefixes
            matches = []
            prefix_lower = prefix.lower()
            for _skill_name, instance in self._skills.items():
                if not instance.is_loaded:
                    continue
                for magic_prefix in instance.metadata.magic_prefixes:
                    if magic_prefix.lower().startswith(prefix_lower):
                        matches.append(instance.metadata)
                        break

            return matches

    def find_by_trigger_keyword(self, text: str) -> list[SkillMetadata]:
        """Find skills matching trigger keywords in text.

        Args:
            text: The text to search for trigger keywords.

        Returns:
            List of matching skill metadata.
        """
        with self._lock:
            text_lower = text.lower()
            matched_names: set[str] = set()

            for keyword, skill_names in self._trigger_index.items():
                if keyword.lower() in text_lower:
                    matched_names.update(skill_names)

            return [
                self._skills[name].metadata
                for name in matched_names
                if name in self._skills and self._skills[name].is_loaded
            ]

    async def reload_skill(self, skill_path: Path) -> Result[SkillInstance, str]:
        """Hot-reload a skill from its path.

        Args:
            skill_path: Path to the skill directory or SKILL.md file.

        Returns:
            Result containing the reloaded instance or an error message.
        """
        # Resolve to SKILL.md if directory given
        if skill_path.is_dir():
            skill_md = skill_path / "SKILL.md"
        else:
            skill_md = skill_path
            skill_path = skill_path.parent

        if not skill_md.exists():
            return Result.err(f"SKILL.md not found at {skill_md}")

        skill_name = skill_path.name

        try:
            instance = await self._load_skill(skill_md)
            log.info(
                "plugin.skill.reloaded",
                skill=skill_name,
                version=instance.metadata.version,
            )
            return Result.ok(instance)
        except Exception as e:
            log.error("plugin.skill.reload_failed", skill=skill_name, error=str(e))
            return Result.err(f"Failed to reload {skill_name}: {e}")

    def _start_watcher(self) -> None:
        """Start the file system watcher for hot-reload.

        Creates a background thread that monitors the skills directory
        for changes to SKILL.md files.
        """
        if not WATCHDOG_AVAILABLE:
            log.info(
                "plugin.skill.watcher_unavailable",
                message="watchdog not installed, hot-reload disabled",
            )
            return

        if self._observer is not None:
            return  # Already watching

        try:
            self._watcher = SkillFileWatcher(self)
            self._observer = Observer()
            self._observer.schedule(
                self._watcher,
                str(self._skill_dir),
                recursive=True,
            )
            self._observer.start()
            log.info("plugin.skill.watcher_started", path=str(self._skill_dir))
        except Exception as e:
            log.warning("plugin.skill.watcher_failed", error=str(e))

    def stop_watcher(self) -> None:
        """Stop the file system watcher."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=1.0)
            self._observer = None
            log.info("plugin.skill.watcher_stopped")

    async def _load_skill(self, skill_md_path: Path) -> SkillInstance:
        """Load a skill from its SKILL.md file.

        Args:
            skill_md_path: Path to the SKILL.md file.

        Returns:
            The loaded skill instance.

        Raises:
            ValueError: If the skill file is invalid.
        """
        skill_dir = skill_md_path.parent
        skill_name = skill_dir.name
        mtime = skill_md_path.stat().st_mtime

        # Parse SKILL.md
        content = skill_md_path.read_text(encoding="utf-8")
        spec = self._parse_skill_md(content)

        # Extract metadata
        frontmatter = spec.get("frontmatter", {})
        (
            intercept_eligible,
            mcp_tool,
            mcp_args,
            intercept_validation_error,
        ) = self._extract_intercept_metadata(
            frontmatter,
            spec.get("frontmatter_error"),
        )
        metadata = SkillMetadata(
            name=skill_name,
            path=skill_dir,
            trigger_keywords=self._extract_trigger_keywords(frontmatter),
            magic_prefixes=self._extract_magic_prefixes(frontmatter, skill_name),
            description=frontmatter.get("description", spec.get("first_line", "")),
            version=frontmatter.get("version", "1.0.0"),
            mode=SkillMode.MCP
            if frontmatter.get("mode") == "mcp" or intercept_eligible
            else SkillMode.PLUGIN,
            requires_mcp=bool(frontmatter.get("requires_mcp", False) or intercept_eligible),
            intercept_eligible=intercept_eligible,
            mcp_tool=mcp_tool,
            mcp_args=mcp_args,
            intercept_validation_error=intercept_validation_error,
        )

        instance = SkillInstance(
            metadata=metadata,
            spec=spec,
            last_modified=mtime,
            is_loaded=True,
        )

        # Update storage
        with self._lock:
            self._skills[skill_name] = instance
            self._index_skill(skill_name, metadata)

        return instance

    def _parse_skill_md(self, content: str) -> dict[str, Any]:
        """Parse SKILL.md content into structured data.

        Args:
            content: The raw SKILL.md file content.

        Returns:
            Dictionary with frontmatter, sections, and first_line.
        """
        lines = content.split("\n")

        # Extract frontmatter (YAML-like metadata at top)
        frontmatter: dict[str, Any] = {}
        frontmatter_error: str | None = None
        content_start = 0

        # Check for YAML frontmatter
        if lines and lines[0].strip() == "---":
            closing_index = next(
                (i for i in range(1, len(lines)) if lines[i].strip() == "---"),
                None,
            )
            if closing_index is None:
                frontmatter_error = "Unterminated frontmatter block"
                log.warning(
                    "plugin.skill.frontmatter_parse_failed",
                    error=frontmatter_error,
                )
                content_start = 1
            else:
                frontmatter_block = "\n".join(lines[1:closing_index])
                if frontmatter_block.strip():
                    try:
                        parsed_frontmatter = yaml.safe_load(frontmatter_block)
                    except yaml.YAMLError as exc:
                        frontmatter_error = str(exc)
                        log.warning(
                            "plugin.skill.frontmatter_parse_failed",
                            error=frontmatter_error,
                        )
                    else:
                        if parsed_frontmatter is None:
                            frontmatter = {}
                        elif isinstance(parsed_frontmatter, dict):
                            frontmatter = {
                                str(key).lower(): value
                                for key, value in parsed_frontmatter.items()
                            }
                        else:
                            frontmatter_error = "Frontmatter must parse to a mapping"
                            log.warning(
                                "plugin.skill.frontmatter_parse_failed",
                                error=frontmatter_error,
                            )
                content_start = closing_index + 1

        # Get first line of actual content
        first_line = ""
        for line in lines[content_start:]:
            if line.strip() and not line.startswith("#"):
                first_line = line.strip()
                break
            elif line.startswith("#") and not first_line:
                first_line = line.lstrip("#").strip()

        # Extract sections
        sections: dict[str, str] = {}
        current_section = "intro"
        current_content: list[str] = []

        for line in lines[content_start:]:
            if line.startswith("##") and line[2:].strip():
                # Save previous section
                if current_content:
                    sections[current_section] = "\n".join(current_content).strip()
                    current_content = []
                # Start new section
                current_section = line[2:].strip().lower().replace(" ", "_")
            else:
                current_content.append(line)

        # Save last section
        if current_content:
            sections[current_section] = "\n".join(current_content).strip()

        return {
            "frontmatter": frontmatter,
            "frontmatter_error": frontmatter_error,
            "sections": sections,
            "first_line": first_line,
            "raw": content,
        }

    def _extract_trigger_keywords(
        self,
        frontmatter: dict[str, Any],
    ) -> tuple[str, ...]:
        """Extract trigger keywords from frontmatter."""
        return self._normalize_string_sequence(frontmatter.get("triggers"))

    def _extract_intercept_metadata(
        self,
        frontmatter: dict[str, Any],
        frontmatter_error: str | None,
    ) -> tuple[bool, str | None, dict[str, Any] | None, str | None]:
        """Extract and validate MCP interception metadata from frontmatter."""
        if frontmatter_error:
            return (
                False,
                None,
                None,
                f"frontmatter parse failed: {frontmatter_error}",
            )

        raw_mcp_tool = frontmatter.get("mcp_tool")
        if raw_mcp_tool is None:
            return False, None, None, "missing required frontmatter key: mcp_tool"
        if not isinstance(raw_mcp_tool, str) or not raw_mcp_tool.strip():
            return False, None, None, "mcp_tool must be a non-empty string"

        mcp_tool = raw_mcp_tool.strip()
        if _MCP_TOOL_NAME_PATTERN.fullmatch(mcp_tool) is None:
            return (
                False,
                None,
                None,
                "mcp_tool must contain only letters, digits, and underscores",
            )

        if "mcp_args" not in frontmatter:
            return False, None, None, "missing required frontmatter key: mcp_args"

        raw_mcp_args = frontmatter.get("mcp_args")
        if not self._is_valid_dispatch_mapping(raw_mcp_args):
            return (
                False,
                None,
                None,
                "mcp_args must be a mapping with string keys and YAML-safe values",
            )

        return True, mcp_tool, self._clone_dispatch_value(raw_mcp_args), None

    def _is_valid_dispatch_mapping(self, value: Any) -> bool:
        """Validate dispatch args are mapping-shaped and recursively serializable."""
        if not isinstance(value, Mapping):
            return False

        return all(
            isinstance(key, str) and bool(key.strip()) and self._is_valid_dispatch_value(item)
            for key, item in value.items()
        )

    def _is_valid_dispatch_value(self, value: Any) -> bool:
        """Validate a dispatch template value recursively."""
        if value is None or isinstance(value, str | int | float | bool):
            return True

        if isinstance(value, Mapping):
            return self._is_valid_dispatch_mapping(value)

        if isinstance(value, list | tuple):
            return all(self._is_valid_dispatch_value(item) for item in value)

        return False

    def _clone_dispatch_value(self, value: Any) -> Any:
        """Clone validated dispatch metadata into plain Python containers."""
        if isinstance(value, Mapping):
            return {key: self._clone_dispatch_value(item) for key, item in value.items()}

        if isinstance(value, list | tuple):
            return [self._clone_dispatch_value(item) for item in value]

        return value

    def _normalize_string_sequence(self, raw: Any) -> tuple[str, ...]:
        """Normalize a frontmatter field into a de-duplicated string tuple."""
        values: list[str] = []

        if isinstance(raw, str):
            candidates: list[Any] = raw.split(",")
        elif isinstance(raw, list | tuple):
            candidates = list(raw)
        elif raw is None:
            candidates = []
        else:
            candidates = [raw]

        for candidate in candidates:
            if isinstance(candidate, str):
                parts = candidate.split(",")
            else:
                parts = [str(candidate)]

            for part in parts:
                normalized = part.strip()
                if normalized:
                    values.append(normalized)

        return tuple(dict.fromkeys(values))

    def _extract_magic_prefixes(
        self,
        frontmatter: dict[str, Any],
        skill_name: str,
    ) -> tuple[str, ...]:
        """Extract magic prefixes from frontmatter.

        Args:
            frontmatter: Parsed frontmatter dictionary.
            skill_name: The skill name.

        Returns:
            Tuple of magic prefix strings.
        """
        prefixes = list(self._normalize_string_sequence(frontmatter.get("magic_prefixes")))

        # Auto-generate from skill name
        prefixes.append(f"ouroboros:{skill_name}")
        prefixes.append(f"ooo:{skill_name}")
        prefixes.append(f"/ouroboros:{skill_name}")

        return tuple(dict.fromkeys(prefixes))

    def _index_skill(self, skill_name: str, metadata: SkillMetadata) -> None:
        """Index a skill's triggers and prefixes for fast lookup.

        Args:
            skill_name: The skill name.
            metadata: The skill metadata.
        """
        # Index trigger keywords
        for keyword in metadata.trigger_keywords:
            if keyword not in self._trigger_index:
                self._trigger_index[keyword] = set()
            self._trigger_index[keyword].add(skill_name)

        # Index magic prefixes
        for prefix in metadata.magic_prefixes:
            if prefix not in self._prefix_index:
                self._prefix_index[prefix] = set()
            self._prefix_index[prefix].add(skill_name)


# Global singleton instance
_global_registry: SkillRegistry | None = None
_registry_lock = RLock()


def get_registry(skill_dir: Path | None = None) -> SkillRegistry:
    """Get or create the global skill registry singleton.

    Args:
        skill_dir: Optional custom skills directory.

    Returns:
        The global SkillRegistry instance.
    """
    global _global_registry

    with _registry_lock:
        if _global_registry is None:
            _global_registry = SkillRegistry(skill_dir)
        return _global_registry
