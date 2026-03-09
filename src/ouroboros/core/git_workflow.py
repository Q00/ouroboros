"""Git workflow detection from CLAUDE.md.

Parses project CLAUDE.md files to detect git workflow preferences
(PR-based, branch rules, etc.) so that automated tools like Ralph
can respect the user's configured workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class GitWorkflowConfig:
    """Detected git workflow configuration.

    Attributes:
        use_branches: Whether to create feature branches instead of committing to main.
        branch_pattern: Template for branch names. Supports {lineage_id} and {task} placeholders.
        auto_pr: Whether to automatically create a PR after pushing.
        protected_branches: Branch names that should never receive direct commits.
        source: Which file(s) the configuration was detected from.
    """

    use_branches: bool = False
    branch_pattern: str = "ooo/{task}"
    auto_pr: bool = False
    protected_branches: tuple[str, ...] = ("main", "master")
    source: str = ""


# Patterns that indicate a PR-based workflow
_PR_WORKFLOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"pr[- ]based\s+workflow", re.IGNORECASE),
    re.compile(r"always\s+create\s+(?:a\s+)?(?:pull\s+request|pr)", re.IGNORECASE),
    re.compile(r"never\s+(?:commit|push)\s+(?:directly\s+)?to\s+main", re.IGNORECASE),
    re.compile(r"never\s+(?:commit|push)\s+(?:directly\s+)?to\s+master", re.IGNORECASE),
    re.compile(r"create\s+(?:a\s+)?(?:feature\s+)?branch", re.IGNORECASE),
    re.compile(r"open\s+(?:a\s+)?(?:pull\s+request|pr)", re.IGNORECASE),
    re.compile(r"feature\s+branch\s+workflow", re.IGNORECASE),
    re.compile(r"branch\s+and\s+(?:open\s+)?(?:a\s+)?pr", re.IGNORECASE),
)

# Patterns that indicate protected branches
_PROTECTED_BRANCH_PATTERN = re.compile(
    r"(?:never|don'?t|do\s+not)\s+(?:commit|push)\s+(?:directly\s+)?to\s+(\w+)",
    re.IGNORECASE,
)


def detect_git_workflow(project_root: Path) -> GitWorkflowConfig:
    """Detect git workflow preferences from CLAUDE.md files.

    Searches for CLAUDE.md in the project root and parent directories.
    Parses the content for workflow-related patterns.

    Args:
        project_root: Root directory of the project.

    Returns:
        GitWorkflowConfig with detected preferences.
        Defaults to permissive config if no preferences found.
    """
    claude_md_content = ""
    source = ""

    # Check project CLAUDE.md first, then parent directories
    for candidate in [
        project_root / "CLAUDE.md",
        project_root / ".claude" / "CLAUDE.md",
    ]:
        if candidate.exists():
            try:
                claude_md_content = candidate.read_text(encoding="utf-8")
                source = str(candidate)
                break
            except OSError:
                continue

    if not claude_md_content:
        return GitWorkflowConfig()

    # Detect PR-based workflow
    use_branches = any(pattern.search(claude_md_content) for pattern in _PR_WORKFLOW_PATTERNS)

    # Detect protected branches
    protected = set()
    for match in _PROTECTED_BRANCH_PATTERN.finditer(claude_md_content):
        protected.add(match.group(1).lower())

    # Default protected branches if PR workflow detected but none specified
    if use_branches and not protected:
        protected = {"main", "master"}

    # Detect explicit auto-PR preference (requires "auto" keyword to avoid
    # false positives on general "create a PR" workflow instructions)
    auto_pr = bool(
        re.search(
            r"auto(?:matically)?\s+(?:create|open)\s+(?:a\s+)?(?:pull\s+request|pr)",
            claude_md_content,
            re.IGNORECASE,
        )
    )

    return GitWorkflowConfig(
        use_branches=use_branches,
        branch_pattern="ooo/{task}",
        auto_pr=auto_pr,
        protected_branches=tuple(sorted(protected)) if protected else ("main", "master"),
        source=source,
    )


def is_on_protected_branch(project_root: Path, config: GitWorkflowConfig) -> bool:
    """Check if the current git branch is protected.

    Args:
        project_root: Root directory of the project.
        config: Git workflow configuration.

    Returns:
        True if on a protected branch.
    """
    import subprocess  # noqa: S404

    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=5,
        )
        if result.returncode == 0:
            current_branch = result.stdout.strip()
            return current_branch in config.protected_branches
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return False
