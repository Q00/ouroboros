"""Brownfield repository registry — DB-backed business logic.

Manages the global brownfield registry in ``~/.ouroboros/ouroboros.db``
via :class:`~ouroboros.persistence.brownfield.BrownfieldStore`.

Business-level operations:
- Scan-root discovery for valid seed git repos/worktrees
- Linked worktree discovery from normal repo roots via Git metadata
- README/CLAUDE.md parsing for one-line description generation (Frugal model)
- Async CRUD delegated to BrownfieldStore

All brownfield data is stored in the SQLite database.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

log = structlog.get_logger()

# Re-export BrownfieldRepo as BrownfieldEntry for backward compat
BrownfieldEntry = BrownfieldRepo

# ── Constants ──────────────────────────────────────────────────────

_FRUGAL_MODEL = "anthropic/claude-3-5-haiku-20241022"

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "__pycache__",
        ".cache",
        "Library",
        ".Trash",
        "vendor",
        ".gradle",
        "build",
        "dist",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".cargo",
        "Pods",
        ".npm",
        ".nvm",
        ".local",
        ".docker",
        ".rustup",
        "go",
    }
)

_DESC_SYSTEM_PROMPT = (
    "You are a concise technical writer. "
    "Given the content of a project's README or CLAUDE.md, "
    "produce exactly ONE short sentence (max 15 words) describing the project. "
    "Reply with only that sentence — no quotes, no bullet points."
)


# ── Scan root discovery ────────────────────────────────────────────


def _has_origin_remote(repo_path: Path) -> bool:
    """Check whether a git repo has a configured ``origin`` remote.

    Args:
        repo_path: Path to the repository root (parent of ``.git``).

    Returns:
        True if ``git remote get-url origin`` returns a non-empty URL.
    """
    return _origin_remote_url(repo_path) is not None


def _origin_remote_url(repo_path: Path) -> str | None:
    """Return the configured ``origin`` remote URL for a git repo, if present."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        origin = result.stdout.strip()
        return origin or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _origin_host(remote_url: str) -> str:
    """Extract the host from common Git remote URL forms."""
    parsed = urlparse(remote_url)
    if parsed.hostname:
        return parsed.hostname.lower()

    # SCP-like remotes: git@github.com:owner/repo.git
    if ":" in remote_url:
        before_path = remote_url.split(":", 1)[0]
        if "@" in before_path:
            return before_path.rsplit("@", 1)[-1].lower()

    return ""


def _has_github_origin(repo_path: Path) -> bool:
    """Check whether a git repo has a GitHub ``origin`` remote."""
    origin = _origin_remote_url(repo_path)
    return origin is not None and _origin_host(origin) == "github.com"


def _is_git_worktree(repo_path: Path) -> bool:
    """Check whether a path is inside a valid Git working tree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False

    return result.returncode == 0 and result.stdout.strip() == "true"


def _list_git_worktrees(repo_path: Path) -> list[Path]:
    """Return linked worktree paths reported by Git for a normal repo root."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    worktrees: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktree_path = line.removeprefix("worktree ").strip()
            if worktree_path:
                worktrees.append(Path(worktree_path))
    return worktrees


def _repo_entry(repo_path: Path) -> dict[str, str] | None:
    """Build a scan result entry for an existing repository directory."""
    try:
        resolved = repo_path.resolve()
    except OSError:
        return None

    if not resolved.is_dir():
        return None

    return {"path": str(resolved), "name": resolved.name}


def _add_repo_and_worktrees(
    repo_path: Path,
    repos_by_path: dict[str, dict[str, str]],
    *,
    include_linked_worktrees: bool,
) -> None:
    """Add a seed repo and optionally any Git-reported linked worktrees.

    The seed path must be found by the filesystem walk. For normal repository
    roots, linked worktrees come from ``git worktree list`` and may be outside
    the walk root.
    """
    if not _is_git_worktree(repo_path):
        return

    candidates = [repo_path]
    if include_linked_worktrees:
        candidates.extend(_list_git_worktrees(repo_path))

    for candidate in candidates:
        if not _is_git_worktree(candidate):
            continue
        entry = _repo_entry(candidate)
        if entry is None:
            continue
        repos_by_path.setdefault(entry["path"], entry)


def scan_home_for_repos(
    root: Path | None = None,
) -> list[dict[str, str]]:
    """Walk a root directory to find valid git repos/worktrees.

    Scanning rules:
    - Repositories are discovered by filesystem walking under ``root`` only.
    - A seed repo/worktree is any walked directory with a ``.git`` directory or file.
    - Prune subdirectories once a seed is found (no nested repo walk).
    - Skip hardcoded noise directories (node_modules, .venv, etc.).
    - Skip dot-prefixed directories during filesystem walking.
    - Normal repo roots are expanded via ``git worktree list --porcelain``.
    - Linked worktrees reported by normal repo roots may be included outside ``root``.
    - Linked worktree seeds are registered self-only and do not pull main/sibling
      worktrees outside ``root``.
    - Local repos and repos with any remote name are included.

    Args:
        root: Directory to start the seed filesystem walk. Defaults to the
            current user's home directory.

    Returns:
        Sorted list of ``{path, name}`` dicts for each discovered repo/worktree.
    """
    if root is None:
        root = Path.home()

    repos_by_path: dict[str, dict[str, str]] = {}

    # os.walk with topdown=True so we can modify dirs in-place to prune
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        current = Path(dirpath)

        has_git_dir = ".git" in dirnames
        has_git_file = ".git" in filenames
        if has_git_dir or has_git_file:
            # A .git directory is a normal repo root, so it can safely expand to
            # its Git-reported worktree family. A .git file is a linked worktree
            # seed; register it, but do not use it as a portal to pull in the
            # main repo or siblings. Users may scan/pass a specific worktree to
            # keep the rest of the repository out of AI context.
            _add_repo_and_worktrees(
                current,
                repos_by_path,
                include_linked_worktrees=has_git_dir,
            )
            log.debug("brownfield.scan.found", path=str(current))

            # Prune: don't descend into this repo's subdirectories
            dirnames.clear()
            continue

        # Prune skip directories
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]

    repos = list(repos_by_path.values())
    repos.sort(key=lambda r: r["path"])
    log.info("brownfield.scan.complete", root=str(root), found=len(repos))
    return repos


# ── README / CLAUDE.md description generation ─────────────────────


def _read_readme_content(repo_path: Path, max_chars: int = 3000) -> str | None:
    """Read README or CLAUDE.md content from a repo, truncated.

    Checks in order: CLAUDE.md, README.md, README.rst, README.txt, README.

    Args:
        repo_path: Path to the repository root.
        max_chars: Maximum characters to read.

    Returns:
        File content (truncated) or None if not found.
    """
    candidates = [
        "CLAUDE.md",
        "README.md",
        "README.rst",
        "README.txt",
        "README",
    ]
    for name in candidates:
        filepath = repo_path / name
        if filepath.is_file():
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
                return text[:max_chars]
            except OSError:
                continue
    return None


async def generate_desc(
    repo_path: Path,
    llm_adapter: LLMAdapter,
    model: str = _FRUGAL_MODEL,
) -> str:
    """Generate a one-line description for a repo using a Frugal-tier LLM.

    Reads README/CLAUDE.md and asks a Haiku-class model for a short summary.
    Falls back to the directory name if no README is found or LLM fails.

    Args:
        repo_path: Path to the repository root.
        llm_adapter: LLM adapter for the completion call.
        model: Model identifier (defaults to Frugal/Haiku-class).

    Returns:
        One-line description string.
    """
    content = _read_readme_content(repo_path)
    if not content:
        return ""

    messages = [
        Message(role=MessageRole.SYSTEM, content=_DESC_SYSTEM_PROMPT),
        Message(
            role=MessageRole.USER,
            content=f"Project at: {repo_path.name}\n\n{content}",
        ),
    ]
    config = CompletionConfig(
        model=model,
        temperature=0.0,
        max_tokens=60,
    )

    try:
        result = await llm_adapter.complete(messages, config)
        if result.is_ok:
            desc = result.value.content.strip().rstrip(".")
            # Sanity: cap at 120 chars
            return desc[:120]
    except (ProviderError, OSError) as exc:
        log.warning(
            "brownfield.desc_generation_failed",
            path=str(repo_path),
            error=str(exc),
            exc_info=exc,
        )

    return ""


# ── High-level orchestration ───────────────────────────────────────


async def scan_and_register(
    store: BrownfieldStore,
    llm_adapter: LLMAdapter | None = None,  # noqa: ARG001
    root: Path | None = None,
    *,
    model: str = _FRUGAL_MODEL,  # noqa: ARG001
) -> list[BrownfieldRepo]:
    """Scan a root directory for repos/worktrees and bulk-register them in the DB.

    This is the main entry point for brownfield scanning.

    1. Walk ``root`` (the current user's home directory when omitted) to find
       valid seed git repos/worktrees.
    2. For each normal repo root, include Git-reported linked worktrees even
       when they live outside ``root``. Linked worktree seeds are self-only.
    3. Upsert all found repos while preserving existing names, descriptions,
       and default flags. Default selection is handled by setup/MCP flows.

    Description generation is deferred to ``set_default_repo`` (Frugal model).
    The ``llm_adapter`` and ``model`` params are accepted for API compatibility
    but are not used during scanning.

    Args:
        store: Initialized BrownfieldStore.
        llm_adapter: Unused — kept for backward API compatibility.
        root: Directory to walk for seed repos/worktrees. Defaults to the
            current user's home directory.
        model: Unused — kept for backward API compatibility.

    Returns:
        List of BrownfieldRepo instances discovered and upserted by THIS scan.
        Repos that were registered manually or by previous scans but not
        re-discovered by this scan are not included; callers that need the
        full registry should call :py:meth:`BrownfieldStore.list` directly.
    """
    scanned = scan_home_for_repos(root)

    if not scanned:
        log.info("brownfield.scan_and_register.no_repos")
        return []

    # Upsert scanned repos — register() does INSERT OR UPDATE for
    # existing paths, preserving is_default and desc for repos already
    # in the DB.  Manual entries outside the scan root are NOT deleted.
    # Preserve user-curated names for existing repos by checking first.
    existing_repos = {r.path: r for r in await store.list()}
    scanned_paths: set[str] = set()
    for repo_dict in scanned:
        path = repo_dict["path"]
        name = repo_dict["name"]
        scanned_paths.add(path)
        if path in existing_repos and existing_repos[path].name:
            # Preserve existing name; register() will still upsert desc/default
            name = existing_repos[path].name
        await store.register(path=path, name=name)

    log.info("brownfield.upsert_registered", count=len(scanned_paths))

    # Return only the repos that were just discovered/upserted. The full
    # registry can include manually-registered or previously-scanned repos
    # outside the current scan root, and conflating them with "what this
    # scan found" leaks state into a boundary-sensitive operation.
    return [r for r in await store.list() if r.path in scanned_paths]


async def get_default_brownfield_context(
    store: BrownfieldStore,
) -> list[BrownfieldRepo]:
    """Get the default brownfield repos for PM interview context.

    Returns all repos marked as default to support multi-default.

    Args:
        store: Initialized BrownfieldStore.

    Returns:
        List of default BrownfieldRepo instances (may be empty).
    """
    return await store.get_defaults()


# ── Register & set_default handlers ───────────────────────────────


async def register_repo(
    store: BrownfieldStore,
    path: str,
    name: str | None = None,
    desc: str | None = None,
    *,
    llm_adapter: LLMAdapter | None = None,
    model: str = _FRUGAL_MODEL,
) -> BrownfieldRepo:
    """Register a single repository in the brownfield DB.

    Handles both manual registration and scan-result registration.
    Generates a one-line description via LLM if an adapter is provided
    and no description is given.

    If ``name`` is omitted, the directory basename is used.

    Args:
        store: Initialized BrownfieldStore.
        path: Absolute filesystem path to the repository.
        name: Human-readable name. Defaults to ``Path(path).name``.
        desc: One-line description. If None and an LLM adapter is given,
              a description is auto-generated from README/CLAUDE.md.
        llm_adapter: Optional LLM adapter for description generation.
        model: Model for description generation.

    Returns:
        The registered BrownfieldRepo.
    """
    repo_path = Path(path)
    resolved_name = name or repo_path.name
    # Resolve only if the path exists on disk (avoids macOS /System/Volumes
    # prefix for non-existent paths in tests and cross-machine registrations).
    canonical_path = str(repo_path.resolve()) if repo_path.exists() else path

    # Auto-generate description if not provided and LLM adapter is available
    if desc is None and llm_adapter is not None:
        try:
            desc = await generate_desc(repo_path, llm_adapter, model)
        except (ProviderError, OSError) as exc:
            log.warning(
                "brownfield.register_repo.desc_failed",
                path=canonical_path,
                error=str(exc),
                exc_info=exc,
            )

    repo = await store.register(
        path=canonical_path,
        name=resolved_name,
        desc=desc or None,
    )

    log.info(
        "brownfield.register_repo",
        path=canonical_path,
        name=resolved_name,
        desc=desc[:60] if desc else "",
    )

    return repo


async def set_default_repo(
    store: BrownfieldStore,
    path: str,
    *,
    llm_adapter: LLMAdapter | None = None,
    model: str = _FRUGAL_MODEL,
) -> BrownfieldRepo | None:
    """Set a registered repository as a default brownfield context.

    Marks the specified repo as default WITHOUT clearing the default flag
    on other repos, supporting multi-default scenarios.

    If the repo's ``desc`` is empty and an ``llm_adapter`` is provided,
    a one-line description is auto-generated from the repo's README/CLAUDE.md
    using a Frugal (Haiku-class) model and stored in the DB.

    Args:
        store: Initialized BrownfieldStore.
        path: Absolute filesystem path of the repo to set as default.
        llm_adapter: Optional LLM adapter for description generation.
        model: Model identifier for description generation.

    Returns:
        The updated BrownfieldRepo, or None if the path is not registered.
    """
    repo = await store.update_is_default(path, is_default=True)

    if repo is None:
        log.warning("brownfield.set_default_repo.not_found", path=path)
        return None

    # Auto-generate desc if empty and LLM adapter is available
    if not repo.desc and llm_adapter is not None:
        try:
            desc = await generate_desc(Path(repo.path), llm_adapter, model)
            if desc:
                updated = await store.update_desc(repo.path, desc)
                if updated is not None:
                    repo = updated
                    log.info(
                        "brownfield.set_default_repo.desc_generated",
                        path=path,
                        desc=desc[:60],
                    )
        except (ProviderError, OSError) as exc:
            log.warning(
                "brownfield.set_default_repo.desc_failed",
                path=path,
                error=str(exc),
                exc_info=exc,
            )

    log.info("brownfield.set_default_repo", path=path, name=repo.name)
    return repo


# ── Sync convenience wrappers (for non-async callers) ─────────────


def _run_async(coro):
    """Run an async coroutine from sync context, handling event loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in an async context — create a new thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def load_brownfield_repos_as_dicts(
    store: BrownfieldStore | None = None,
) -> list[dict[str, str]]:
    """Load brownfield repos from DB and return as plain dicts.

    Convenience wrapper for callers that expect ``list[dict[str, str]]``.

    Args:
        store: Optional BrownfieldStore. Creates a temporary one if None.

    Returns:
        List of repo dicts with keys: path, name, desc.
    """

    async def _load() -> list[dict[str, str]]:
        own_store = store is None
        s = store or BrownfieldStore()
        try:
            if own_store:
                await s.initialize()
            repos = await s.list()
            return [r.to_dict() for r in repos]
        finally:
            if own_store:
                await s.close()

    return _run_async(_load())
