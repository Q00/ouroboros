"""Git workflow detection from CLAUDE.md.

Parses project CLAUDE.md files to detect git workflow preferences
(PR-based, branch rules, etc.) so that automated tools like Ralph
can respect the user's configured workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import structlog

log = structlog.get_logger()


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
    """Check whether the current git branch must be treated as protected.

    This is a safety guard whose only purpose is to stop automation from
    committing to a branch the user declared off-limits. It therefore **fails
    closed**: whenever the branch cannot be positively determined, the branch
    is reported as protected so that automation declines to commit.

    Returns ``True`` when:

    * git reports a branch listed in ``config.protected_branches``;
    * HEAD is detached -- ``git rev-parse --abbrev-ref HEAD`` prints the
      literal ``HEAD`` and exits 0 -- because an automated commit there would
      not be reachable from any branch;
    * the branch cannot be determined at all: git is missing, the call times
      out, ``project_root`` is not a git repository, git exits non-zero, or
      git returns empty output. Each of these is logged as a warning with the
      reason.

    Returns ``False`` only when git successfully reports a concrete branch name
    that is not in ``config.protected_branches``.

    Args:
        project_root: Root directory of the project.
        config: Git workflow configuration.

    Returns:
        True if the branch is protected or could not be determined, False only
        when git confirms a concrete non-protected branch.
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
    except subprocess.TimeoutExpired:
        log.warning(
            "git_workflow.protected_branch_undetermined",
            project_root=str(project_root),
            reason="timeout",
            detail="git rev-parse did not complete within 5s",
            fail_closed=True,
        )
        return True
    except OSError as exc:
        # Covers FileNotFoundError (git not installed, or project_root does
        # not exist) and other OS-level failures spawning the subprocess.
        log.warning(
            "git_workflow.protected_branch_undetermined",
            project_root=str(project_root),
            reason="git_spawn_failed",
            detail=f"{type(exc).__name__}: {exc}",
            project_root_exists=project_root.is_dir(),
            fail_closed=True,
        )
        return True

    if result.returncode != 0:
        # Most commonly: project_root is not a git repository.
        log.warning(
            "git_workflow.protected_branch_undetermined",
            project_root=str(project_root),
            reason="git_error",
            returncode=result.returncode,
            detail=(result.stderr or "").strip()[:200],
            fail_closed=True,
        )
        return True

    current_branch = result.stdout.strip()

    if not current_branch:
        log.warning(
            "git_workflow.protected_branch_undetermined",
            project_root=str(project_root),
            reason="empty_output",
            detail="git rev-parse succeeded but reported no branch name",
            fail_closed=True,
        )
        return True

    if current_branch == "HEAD":
        log.warning(
            "git_workflow.protected_branch_undetermined",
            project_root=str(project_root),
            reason="detached_head",
            detail="HEAD is detached; automated commits would be unreachable",
            fail_closed=True,
        )
        return True

    return current_branch in config.protected_branches
