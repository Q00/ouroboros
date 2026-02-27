"""Codebase exploration engine for brownfield project context.

This module provides automated codebase scanning to extract context
that informs the interview and seed generation phases for brownfield projects.

The explorer:
1. Scans directories for config files (go.mod, package.json, .csproj, etc.)
2. Identifies tech stack and dependencies
3. Discovers key type definitions (struct, class, interface, enum, const)
4. Summarizes protocols and patterns via LLM
5. Returns condensed context for injection into interview system prompt
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger()

# Config files that indicate tech stack
_CONFIG_FILES: dict[str, str] = {
    "go.mod": "Go",
    "go.sum": "Go",
    "Cargo.toml": "Rust",
    "package.json": "JavaScript/TypeScript",
    "tsconfig.json": "TypeScript",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "pom.xml": "Java",
    "build.gradle": "Java/Kotlin",
    "build.gradle.kts": "Kotlin",
    "Gemfile": "Ruby",
    "mix.exs": "Elixir",
    "composer.json": "PHP",
    "CMakeLists.txt": "C/C++",
    "Makefile": "Make-based",
}

# File extensions for type discovery
_TYPE_PATTERNS: dict[str, list[str]] = {
    "Go": ["*.go"],
    "Rust": ["*.rs"],
    "JavaScript/TypeScript": ["*.ts", "*.tsx", "*.js", "*.jsx"],
    "TypeScript": ["*.ts", "*.tsx"],
    "Python": ["*.py"],
    "Java": ["*.java"],
    "Java/Kotlin": ["*.java", "*.kt"],
    "Kotlin": ["*.kt"],
    "Ruby": ["*.rb"],
    "Elixir": ["*.ex", "*.exs"],
    "PHP": ["*.php"],
    "C/C++": ["*.c", "*.cpp", "*.h", "*.hpp"],
    "C#": ["*.cs"],
}

# Regex patterns for type definitions by language
_TYPE_DEF_PATTERNS: dict[str, list[str]] = {
    "Go": [
        r"^type\s+\w+\s+struct\b",
        r"^type\s+\w+\s+interface\b",
        r"^type\s+\w+\s+int\b",
        r"^const\s*\(",
        r"^func\s+\(",
    ],
    "Rust": [
        r"^pub\s+(struct|enum|trait)\s+\w+",
        r"^(struct|enum|trait)\s+\w+",
    ],
    "Python": [
        r"^class\s+\w+",
        r"^def\s+\w+",
    ],
    "TypeScript": [
        r"^(export\s+)?(interface|type|class|enum)\s+\w+",
    ],
    "JavaScript/TypeScript": [
        r"^(export\s+)?(interface|type|class|enum)\s+\w+",
    ],
    "Java": [
        r"^(public|private|protected)?\s*(class|interface|enum)\s+\w+",
    ],
    "Java/Kotlin": [
        r"^(public|private|protected)?\s*(class|interface|enum)\s+\w+",
        r"^(data\s+)?class\s+\w+",
    ],
    "Kotlin": [
        r"^(data\s+)?class\s+\w+",
        r"^(sealed\s+)?interface\s+\w+",
        r"^enum\s+class\s+\w+",
    ],
    "C#": [
        r"^(public|private|internal)?\s*(class|interface|enum|struct|record)\s+\w+",
    ],
    "C/C++": [
        r"^(typedef\s+)?struct\s+\w+",
        r"^class\s+\w+",
        r"^enum\s+(class\s+)?\w+",
    ],
}

_FALLBACK_MODEL = "openrouter/google/gemini-2.0-flash-001"


@dataclass(frozen=True, slots=True)
class CodebaseExploreResult:
    """Result of codebase exploration for a single directory.

    Attributes:
        path: Absolute path to the explored directory.
        role: 'primary' (modify this) or 'reference' (read-only).
        tech_stack: Detected technology stack description.
        key_types: Important type definitions found.
        key_patterns: Architectural patterns discovered.
        key_protocols: Protocol/API signatures discovered.
        dependencies: Key dependencies from config files.
        summary: Condensed context string for prompt injection.
    """

    path: str
    role: str
    tech_stack: str
    key_types: tuple[str, ...] = ()
    key_patterns: tuple[str, ...] = ()
    key_protocols: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    summary: str = ""


@dataclass
class CodebaseExplorer:
    """Explores existing codebases to extract context for brownfield projects.

    Scans directories, identifies tech stacks, discovers type definitions,
    and uses LLM to summarize patterns and protocols.

    Attributes:
        llm_adapter: LLM adapter for summarization.
        model: Model to use for summarization.
        max_files_per_scan: Maximum files to scan per directory.
        max_type_defs: Maximum type definitions to collect.
    """

    llm_adapter: LLMAdapter
    model: str = _FALLBACK_MODEL
    max_files_per_scan: int = 50
    max_type_defs: int = 30

    async def explore(
        self,
        paths: list[dict[str, str]],
    ) -> list[CodebaseExploreResult]:
        """Explore multiple codebase directories.

        Args:
            paths: List of dicts with 'path' and 'role' keys.
                   role is 'primary' or 'reference'.

        Returns:
            List of exploration results, one per directory.
        """
        results: list[CodebaseExploreResult] = []

        for entry in paths:
            path = entry.get("path", "")
            role = entry.get("role", "reference")
            dir_path = Path(path)

            if not dir_path.is_dir():
                log.warning(
                    "explore.directory_not_found",
                    path=path,
                )
                continue

            try:
                scan = await self._scan_directory(dir_path)
                summary = await self._summarize_with_llm(scan)

                result = CodebaseExploreResult(
                    path=path,
                    role=role,
                    tech_stack=scan.get("tech_stack", "Unknown"),
                    key_types=tuple(scan.get("key_types", [])),
                    key_patterns=tuple(scan.get("key_patterns", [])),
                    key_protocols=tuple(scan.get("key_protocols", [])),
                    dependencies=tuple(scan.get("dependencies", [])),
                    summary=summary,
                )
                results.append(result)

                log.info(
                    "explore.directory_scanned",
                    path=path,
                    tech_stack=result.tech_stack,
                    type_count=len(result.key_types),
                )

            except Exception as e:
                log.warning(
                    "explore.scan_failed",
                    path=path,
                    error=str(e),
                )

        return results

    async def _scan_directory(self, dir_path: Path) -> dict[str, Any]:
        """Scan a directory for tech stack, types, and patterns.

        Args:
            dir_path: Directory to scan.

        Returns:
            Dictionary with scan results.
        """
        import re

        scan: dict[str, Any] = {
            "path": str(dir_path),
            "tech_stack": "Unknown",
            "key_types": [],
            "key_patterns": [],
            "key_protocols": [],
            "dependencies": [],
            "config_contents": {},
        }

        # 1. Detect tech stack from config files
        detected_langs: list[str] = []
        for config_name, lang in _CONFIG_FILES.items():
            config_path = dir_path / config_name
            if config_path.exists():
                detected_langs.append(lang)
                try:
                    content = config_path.read_text(encoding="utf-8", errors="replace")
                    # Keep first 2000 chars for context
                    scan["config_contents"][config_name] = content[:2000]

                    # Extract dependencies from config files
                    scan["dependencies"].extend(self._extract_dependencies(config_name, content))
                except OSError:
                    pass

        if detected_langs:
            scan["tech_stack"] = ", ".join(dict.fromkeys(detected_langs))
        else:
            scan["tech_stack"] = "Unknown"

        # 2. Discover key type definitions
        primary_lang = detected_langs[0] if detected_langs else None
        if primary_lang and primary_lang in _TYPE_PATTERNS:
            type_globs = _TYPE_PATTERNS[primary_lang]
            type_patterns = _TYPE_DEF_PATTERNS.get(primary_lang, [])

            if type_patterns:
                compiled_patterns = [re.compile(p) for p in type_patterns]
                type_defs: list[str] = []
                files_scanned = 0

                for glob_pattern in type_globs:
                    for source_file in sorted(dir_path.rglob(glob_pattern)):
                        if files_scanned >= self.max_files_per_scan:
                            break
                        # Skip vendor/node_modules/hidden dirs
                        parts = source_file.relative_to(dir_path).parts
                        if any(
                            p.startswith(".")
                            or p
                            in ("vendor", "node_modules", "__pycache__", ".git", "dist", "build")
                            for p in parts
                        ):
                            continue

                        files_scanned += 1
                        try:
                            lines = source_file.read_text(
                                encoding="utf-8", errors="replace"
                            ).splitlines()
                            for line in lines:
                                stripped = line.strip()
                                for pattern in compiled_patterns:
                                    if pattern.search(stripped):
                                        # Include file context
                                        rel = source_file.relative_to(dir_path)
                                        type_defs.append(f"{rel}: {stripped}")
                                        break
                                if len(type_defs) >= self.max_type_defs:
                                    break
                        except OSError:
                            continue

                    if len(type_defs) >= self.max_type_defs:
                        break

                scan["key_types"] = type_defs

        # 3. Detect patterns from directory structure
        scan["key_patterns"] = self._detect_patterns(dir_path)

        return scan

    def _extract_dependencies(self, config_name: str, content: str) -> list[str]:
        """Extract key dependency names from a config file.

        Args:
            config_name: Name of the config file.
            content: File content.

        Returns:
            List of dependency identifiers.
        """
        import re

        deps: list[str] = []

        if config_name == "go.mod":
            # Extract require block dependencies
            for match in re.finditer(r"^\s+(\S+)\s+v[\d.]+", content, re.MULTILINE):
                deps.append(match.group(1))
        elif config_name == "package.json":
            import json as json_mod

            try:
                pkg = json_mod.loads(content)
                for section in ("dependencies", "devDependencies"):
                    if section in pkg and isinstance(pkg[section], dict):
                        deps.extend(pkg[section].keys())
            except (json_mod.JSONDecodeError, KeyError):
                pass
        elif config_name == "pyproject.toml":
            # Simple extraction of dependencies lines
            for match in re.finditer(r'"([a-zA-Z0-9_-]+)', content):
                dep = match.group(1)
                if dep not in ("python", "requires-python") and len(dep) > 2:
                    deps.append(dep)
        elif config_name == "requirements.txt":
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    # Extract package name (before ==, >=, etc.)
                    pkg = re.split(r"[><=!~\[]", line)[0].strip()
                    if pkg:
                        deps.append(pkg)

        return deps[:20]  # Limit to 20 dependencies

    def _detect_patterns(self, dir_path: Path) -> list[str]:
        """Detect architectural patterns from directory structure.

        Args:
            dir_path: Directory to analyze.

        Returns:
            List of detected pattern descriptions.
        """
        patterns: list[str] = []

        # Check for common patterns
        subdirs = {d.name for d in dir_path.iterdir() if d.is_dir() and not d.name.startswith(".")}

        if "cmd" in subdirs and "internal" in subdirs:
            patterns.append("Go standard layout (cmd/internal)")
        if "src" in subdirs:
            patterns.append("src-based layout")
        if "pkg" in subdirs:
            patterns.append("pkg directory for reusable packages")
        if "api" in subdirs:
            patterns.append("api directory for API definitions")
        if "proto" in subdirs or "protobuf" in subdirs:
            patterns.append("Protocol Buffer definitions")
        if "migrations" in subdirs:
            patterns.append("Database migrations")
        if "test" in subdirs or "tests" in subdirs or "__tests__" in subdirs:
            patterns.append("Dedicated test directory")
        if "docker-compose.yml" in {f.name for f in dir_path.iterdir() if f.is_file()}:
            patterns.append("Docker Compose orchestration")
        if "Dockerfile" in {f.name for f in dir_path.iterdir() if f.is_file()}:
            patterns.append("Docker containerization")

        # Check for monorepo indicators
        if "packages" in subdirs or "apps" in subdirs or "services" in subdirs:
            patterns.append("Monorepo structure")

        return patterns

    async def _summarize_with_llm(self, scan: dict[str, Any]) -> str:
        """Use LLM to produce a condensed summary of scan results.

        Args:
            scan: Raw scan results dictionary.

        Returns:
            Condensed summary string suitable for prompt injection.
        """
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        types_text = "\n".join(scan.get("key_types", [])[:20])
        patterns_text = "\n".join(f"- {p}" for p in scan.get("key_patterns", []))
        deps_text = ", ".join(scan.get("dependencies", [])[:15])
        config_text = ""
        for name, content in list(scan.get("config_contents", {}).items())[:3]:
            config_text += f"\n--- {name} ---\n{content[:800]}\n"

        user_prompt = f"""Summarize this codebase for a developer who needs to extend it.
Be concise (max 300 words). Focus on: tech stack, key types/interfaces,
architectural patterns, protocols, and important conventions.

## Tech Stack
{scan.get("tech_stack", "Unknown")}

## Key Dependencies
{deps_text or "None detected"}

## Key Type Definitions
{types_text or "None found"}

## Architectural Patterns
{patterns_text or "None detected"}

## Config Files
{config_text or "None found"}

Output a structured summary with sections: Tech Stack, Key Types, Patterns, Conventions."""

        messages = [
            Message(
                role=MessageRole.SYSTEM,
                content=(
                    "You are a codebase analyst. Produce concise, structured summaries "
                    "of existing codebases to help developers understand what already exists "
                    "before they start extending it. Be factual and specific."
                ),
            ),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=0.2,
            max_tokens=1024,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "explore.llm_summary_failed",
                path=scan.get("path", ""),
                error=str(result.error),
            )
            # Fallback: construct basic summary without LLM
            parts = [f"Tech Stack: {scan.get('tech_stack', 'Unknown')}"]
            if deps_text:
                parts.append(f"Dependencies: {deps_text}")
            if types_text:
                parts.append(f"Key Types:\n{types_text}")
            return "\n".join(parts)

        return result.value.content.strip()


def format_explore_results(results: list[CodebaseExploreResult]) -> str:
    """Format explore results for injection into interview system prompt.

    Args:
        results: List of exploration results.

    Returns:
        Formatted string suitable for system prompt injection.
    """
    if not results:
        return ""

    parts: list[str] = []
    for r in results:
        header = f"### [{r.role.upper()}] {r.path}"
        parts.append(header)
        parts.append(f"Tech: {r.tech_stack}")
        if r.dependencies:
            parts.append(f"Deps: {', '.join(r.dependencies[:10])}")
        if r.summary:
            parts.append(r.summary)
        parts.append("")

    return "\n".join(parts)
