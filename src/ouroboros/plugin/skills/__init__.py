"""Ouroboros Skills Package.

This package provides an enhanced skill system with:
- Auto-discovery from .claude-plugin/skills/
- Hot-reload without restart
- Magic keyword detection
- Skill composition support
- Auto-generated documentation

Usage:
    # Get the global registry
    from ouroboros.plugin.skills import get_registry

    registry = get_registry()
    await registry.discover_all()

    # Detect and route magic keywords
    from ouroboros.plugin.skills import route_to_skill

    skill_name, match_type = route_to_skill("ooo interview")
"""

from ouroboros.plugin.skills.docs import (
    OutputFormat,
    SkillDocumentation,
    SkillDocumentationGenerator,
    generate_skill_docs,
    list_available_skills,
)
from ouroboros.plugin.skills.executor import (
    ExecutionContext,
    ExecutionRecord,
    ExecutionResult,
    ExecutionStatus,
    SkillExecutor,
    get_executor,
)
from ouroboros.plugin.skills.keywords import (
    KeywordMatch,
    MagicKeywordDetector,
    MatchType,
    detect_magic_keywords,
    is_magic_command,
    route_to_skill,
)
from ouroboros.plugin.skills.registry import (
    SkillFileWatcher,
    SkillInstance,
    SkillMetadata,
    SkillMode,
    SkillRegistry,
    get_registry,
)

__all__ = [
    # Registry
    "SkillRegistry",
    "SkillMetadata",
    "SkillInstance",
    "SkillMode",
    "SkillFileWatcher",
    "get_registry",
    # Executor
    "SkillExecutor",
    "ExecutionContext",
    "ExecutionResult",
    "ExecutionRecord",
    "ExecutionStatus",
    "get_executor",
    # Keywords
    "MagicKeywordDetector",
    "KeywordMatch",
    "MatchType",
    "detect_magic_keywords",
    "route_to_skill",
    "is_magic_command",
    # Documentation
    "SkillDocumentationGenerator",
    "SkillDocumentation",
    "OutputFormat",
    "generate_skill_docs",
    "list_available_skills",
]

# Version info
__version__ = "1.0.0"
