"""Brownfield Repository Management MCP tool handler.

Provides an ``ouroboros_brownfield`` MCP tool for managing brownfield
repository registrations in the SQLite database. Supports four actions:

- **scan** — Walk ``scan_root`` (or the current user's home directory when omitted) for seed git
  repos/worktrees and register them in the DB. For each normal repo root, also
  register Git-reported linked worktrees, even when those linked worktrees live
  outside the scan root. Linked worktree seeds are registered themselves, but do
  not pull their main/sibling worktrees outside the scan root. Local repos and
  repos with any remote name are eligible.
- **register** — Manually register a single repository by path.
- **query** — List all registered repos or get the current default.
- **set_default** — Set a registered repo as the default brownfield context.

Follows the action-dispatch pattern from ``pm_handler.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.brownfield import (
    register_repo,
    scan_and_register,
)
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore

log = structlog.get_logger()

_TOOL_NAME = "ouroboros_brownfield"


def _detect_action(arguments: dict[str, Any]) -> str:
    """Auto-detect the action from parameter presence when action is omitted.

    Detection rules (evaluated in order):
    1. If ``action`` is explicitly provided, return it as-is.
    2. If ``is_default`` is present → ``"set_default"``
    3. If ``path`` is present → ``"register"``
    4. Otherwise → ``"query"`` (safe default — read-only).
    """
    explicit = arguments.get("action")
    if explicit:
        return explicit

    if "indices" in arguments:
        return "set_defaults"

    if "is_default" in arguments:
        return "set_default"

    if arguments.get("path"):
        return "register"

    return "query"


@dataclass
class BrownfieldHandler:
    """Handler for the ouroboros_brownfield MCP tool.

    Manages brownfield repository registrations with action-based dispatch:

    - ``scan`` — Walk a caller-supplied root for seed repos/worktrees, then include
      Git-reported linked worktrees for normal repo roots.
    - ``register`` — Manually register one repo.
    - ``query`` — List repos or fetch the default.
    - ``set_default`` — Set a repo as the default brownfield context.

    Each action delegates to the appropriate :class:`BrownfieldStore` method
    and the ``bigbang.brownfield`` business logic layer.
    """

    _store: BrownfieldStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition with action-based parameters."""
        return MCPToolDefinition(
            name=_TOOL_NAME,
            description=(
                "Manage brownfield repository registrations. "
                "Scan a requested root for repos/worktrees, register/query repos, "
                "or set the default brownfield context for PM interviews."
            ),
            parameters=(
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description=(
                        "Action to perform: 'scan' to discover seed repos/worktrees "
                        "from scan_root plus Git-reported linked worktrees from normal repos,"
                        " 'register' to add a single repo,"
                        " 'query' to list all repos or get default,"
                        " 'set_default' to toggle a repo's default flag"
                        " (supports multiple defaults; does NOT clear others)."
                        " Auto-detected from parameters when omitted."
                    ),
                    required=False,
                    enum=("scan", "register", "query", "set_default", "set_defaults"),
                ),
                MCPToolParameter(
                    name="path",
                    type=ToolInputType.STRING,
                    description=(
                        "Absolute filesystem path of the repository. "
                        "Required for 'register' and 'set_default' actions."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="name",
                    type=ToolInputType.STRING,
                    description=(
                        "Human-readable name for the repository. "
                        "Used with 'register'. Defaults to directory name."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="desc",
                    type=ToolInputType.STRING,
                    description=(
                        "One-line description of the repository. Used with 'register'. Optional."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="is_default",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "For 'set_default' action: set to true to mark as default, "
                        "false to unmark. Defaults to true."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="default_only",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "When true with 'query' action, return only the default repo "
                        "instead of the full list. Defaults to false."
                    ),
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="scan_root",
                    type=ToolInputType.STRING,
                    description=(
                        "Root directory for the 'scan' filesystem walk. "
                        "Defaults to the current user's home directory. "
                        "Only seed repos/worktrees are discovered by walking this root; "
                        "linked worktrees reported by Git for normal repo roots may be "
                        "included even when their paths are outside scan_root. Linked "
                        "worktree seeds are registered themselves only."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="indices",
                    type=ToolInputType.STRING,
                    description=(
                        "Comma-separated repo numbers from the scan list "
                        "(e.g. '6,18,19'). Used with 'set_defaults' action to "
                        "replace all defaults at once."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="offset",
                    type=ToolInputType.INTEGER,
                    description=("Number of rows to skip for 'query' pagination. Defaults to 0."),
                    required=False,
                    default=0,
                ),
                MCPToolParameter(
                    name="limit",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Maximum number of rows to return for 'query' pagination. "
                        "Omit for no limit."
                    ),
                    required=False,
                ),
            ),
        )

    async def _get_store(self) -> BrownfieldStore:
        """Return the injected store or create and initialize a new one."""
        if self._store is not None:
            return self._store
        store = BrownfieldStore()
        await store.initialize()
        self._store = store
        return store

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a brownfield management request with action-based dispatch.

        Action is auto-detected from parameter presence when ``action`` is
        omitted:

        - ``path`` present → ``register``
        - Otherwise → ``query``
        """
        action = _detect_action(arguments)
        owned_store = self._store is None

        try:
            if action == "scan":
                return await self._handle_scan(arguments)

            if action == "register":
                return await self._handle_register(arguments)

            if action == "query":
                return await self._handle_query(arguments)

            if action == "set_default":
                return await self._handle_set_default(arguments)

            if action == "set_defaults":
                return await self._handle_set_defaults(arguments)

            return Result.err(
                MCPToolError(
                    f"Unknown action: {action!r}. "
                    "Must be one of: scan, register, query, set_default, set_defaults",
                    tool_name=_TOOL_NAME,
                )
            )

        except Exception as e:
            log.error("brownfield_handler.unexpected_error", error=str(e), action=action)
            return Result.err(
                MCPToolError(
                    f"Brownfield operation failed: {e}",
                    tool_name=_TOOL_NAME,
                )
            )
        finally:
            if owned_store and self._store is not None:
                await self._store.close()
                self._store = None

    # ──────────────────────────────────────────────────────────────
    # scan — Discover repos/worktrees from a caller-provided root
    # ──────────────────────────────────────────────────────────────

    async def _handle_scan(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Scan a caller-provided root for git repos/worktrees and register them.

        Delegates to ``bigbang.brownfield.scan_and_register`` which handles
        directory walking, linked worktree discovery, LLM description generation,
        and DB upsert. The directory walk is bounded to ``scan_root`` (or the
        current user's home directory when omitted), but linked worktrees
        reported by Git for discovered normal repo roots may be outside that
        root. Linked worktree seeds are registered themselves only.
        """
        scan_root_str = arguments.get("scan_root")
        if scan_root_str:
            # Resolve before validating so the existence/dir checks apply to
            # the same path the walk will actually traverse. Without this,
            # symlinks and TOCTOU swaps could let a caller pass an exists+dir
            # check while the underlying walk runs against a different target.
            scan_root = Path(scan_root_str).resolve()
            if not scan_root.exists():
                return Result.err(
                    MCPToolError(
                        f"scan_root does not exist: {scan_root_str!r}",
                        tool_name=_TOOL_NAME,
                    )
                )
            if not scan_root.is_dir():
                return Result.err(
                    MCPToolError(
                        f"scan_root is not a directory: {scan_root_str!r}",
                        tool_name=_TOOL_NAME,
                    )
                )
        else:
            scan_root = None

        store = await self._get_store()

        # scan_and_register handles the full workflow:
        # walk dirs -> discover linked worktrees -> upsert
        repos = await scan_and_register(
            store=store,
            llm_adapter=None,  # No LLM in MCP context for now
            root=scan_root,
        )

        repos_data = [r.to_dict() for r in repos]
        defaults = await store.get_defaults()

        # Build compact list — {id}. {name} using SQLite rowid
        lines = [f"Scan complete. {len(repos)} repositories registered.", ""]
        for i, r in enumerate(repos, 1):
            rid = r.id if r.id is not None else i
            marker = " *" if r.is_default else ""
            lines.append(f"{rid:>2}. {r.name}{marker}")
        lines.append("")
        if defaults:
            default_ids = ", ".join(str(d.id or "?") for d in defaults)
            names = ", ".join(d.name for d in defaults)
            lines.append(f"Defaults (* marked): {default_ids} ({names})")
        else:
            lines.append("No defaults set.")
        summary = "\n".join(lines)

        log.info(
            "brownfield_handler.scan_complete",
            count=len(repos),
            defaults=[d.path for d in defaults],
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=summary,
                    ),
                ),
                is_error=False,
                meta={
                    "action": "scan",
                    "count": len(repos),
                    "repos": repos_data,
                    # "default" is kept for backward-compat/legacy; callers
                    # should prefer "defaults" (list) instead.
                    "default": defaults[0].to_dict() if defaults else None,
                    "defaults": [d.to_dict() for d in defaults],
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # register — Manually register a single repo
    # ──────────────────────────────────────────────────────────────

    async def _handle_register(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Register a single repository by path.

        Delegates to :func:`bigbang.brownfield.register_repo` for
        business-level validation and optional LLM description generation.
        """
        path = arguments.get("path")
        if not path:
            return Result.err(
                MCPToolError(
                    "'path' is required for 'register' action",
                    tool_name=_TOOL_NAME,
                )
            )

        name = arguments.get("name")
        desc = arguments.get("desc")

        store = await self._get_store()
        repo = await register_repo(
            store=store,
            path=path,
            name=name,
            desc=desc,
        )

        log.info(
            "brownfield_handler.registered",
            path=repo.path,
            name=repo.name,
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"Registered: {repo.name} ({repo.path})",
                    ),
                ),
                is_error=False,
                meta={
                    "action": "register",
                    "repo": repo.to_dict(),
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # query — List repos or get default
    # ──────────────────────────────────────────────────────────────

    async def _handle_query(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """List registered repos with offset/limit pagination, or return the default repo."""
        default_only = arguments.get("default_only", False)

        store = await self._get_store()

        if default_only:
            defaults = list(await store.get_defaults())
            if not defaults:
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text="No default brownfield repository set.",
                            ),
                        ),
                        is_error=False,
                        meta={
                            "action": "query",
                            "default_only": True,
                            "default": None,
                            "defaults": [],
                        },
                    )
                )
            # Backward compat: "default" is the first; "defaults" is the full list
            first_default: BrownfieldRepo = defaults[0]
            defaults_data = [d.to_dict() for d in defaults]
            names = ", ".join(d.name for d in defaults)
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=f"Default(s): {names}",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "action": "query",
                        "default_only": True,
                        "default": first_default.to_dict(),
                        "defaults": defaults_data,
                    },
                )
            )

        # Pagination parameters
        offset = int(arguments.get("offset", 0))
        limit_raw = arguments.get("limit")
        limit: int | None = int(limit_raw) if limit_raw is not None else None

        # Total count for pagination metadata
        total = await store.count()

        # Paginated list
        repos = await store.list(offset=offset, limit=limit)
        defaults = list(await store.get_defaults())

        if not repos and total == 0:
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="No brownfield repositories registered. Run 'scan' to discover repos.",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "action": "query",
                        "total": 0,
                        "count": 0,
                        "offset": offset,
                        "limit": limit,
                        "repos": [],
                        "default": None,
                        "defaults": [],
                    },
                )
            )

        lines = [f"Brownfield repositories ({total} total, showing {len(repos)}):"]
        for r in repos:
            marker = " [default]" if r.is_default else ""
            desc_part = f" — {r.desc}" if r.desc else ""
            lines.append(f"  • {r.name}{marker}: {r.path}{desc_part}")

        repos_data = [r.to_dict() for r in repos]
        defaults_data = [d.to_dict() for d in defaults]
        paginated_first_default = defaults[0] if defaults else None

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="\n".join(lines),
                    ),
                ),
                is_error=False,
                meta={
                    "action": "query",
                    "total": total,
                    "count": len(repos),
                    "offset": offset,
                    "limit": limit,
                    "repos": repos_data,
                    "default": paginated_first_default.to_dict()
                    if paginated_first_default
                    else None,
                    "defaults": defaults_data,
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # set_default — Change the default repo
    # ──────────────────────────────────────────────────────────────

    async def _handle_set_default(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Toggle a repo's default flag without clearing other defaults.

        Uses ``update_is_default`` which sets the flag on the target repo
        only — it does **not** clear ``is_default`` on other repos.  This
        intentionally supports multiple simultaneous defaults so that PM
        interviews can reference several brownfield repositories at once.

        Delegates to :meth:`BrownfieldStore.update_is_default` for the
        underlying write.
        """
        path = arguments.get("path")
        if not path:
            return Result.err(
                MCPToolError(
                    "'path' is required for 'set_default' action",
                    tool_name=_TOOL_NAME,
                )
            )

        is_default = arguments.get("is_default", True)
        store = await self._get_store()

        if is_default is False:
            # Just clear this repo's default without touching others
            repo = await store.update_is_default(path, is_default=False)
        else:
            # Set as default — use update_is_default to NOT clear others
            repo = await store.update_is_default(path, is_default=True)

        if repo is None:
            return Result.err(
                MCPToolError(
                    f"Repository not found: {path}. Register it first.",
                    tool_name=_TOOL_NAME,
                )
            )

        log.info(
            "brownfield_handler.default_set",
            path=path,
            name=repo.name,
        )

        action_text = "Default set" if is_default else "Default removed"
        # Fetch all current defaults for accurate reporting
        all_defaults = await store.get_defaults()
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"{action_text}: {repo.name} ({repo.path})",
                    ),
                ),
                is_error=False,
                meta={
                    "action": "set_default",
                    "is_default": is_default,
                    "repo": repo.to_dict(),
                    "defaults": [d.to_dict() for d in all_defaults],
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # set_defaults — Replace all defaults by ID list
    # ──────────────────────────────────────────────────────────────

    async def _handle_set_defaults(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Replace all defaults using a comma-separated list of repo IDs."""
        indices_str = arguments.get("indices", "")

        # Empty string means "clear all defaults" (documented as indices="" for "none")
        if indices_str is None:
            return Result.err(
                MCPToolError(
                    "'indices' is required for 'set_defaults' action (e.g. '6,18,19' or '' to clear all)",
                    tool_name=_TOOL_NAME,
                )
            )

        try:
            ids = [int(x.strip()) for x in str(indices_str).split(",") if x.strip()]
        except ValueError:
            return Result.err(
                MCPToolError(
                    f"Invalid indices: {indices_str!r}. Must be comma-separated numbers.",
                    tool_name=_TOOL_NAME,
                )
            )

        store = await self._get_store()
        defaults = await store.set_defaults_by_ids(ids)

        if not defaults:
            # Distinguish clear-all (empty ids) from no-match (non-empty ids)
            if not ids:
                msg = "All defaults cleared."
            else:
                msg = "No defaults set (no matching IDs found)."
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=msg,
                        ),
                    ),
                    is_error=False,
                    meta={"action": "set_defaults", "defaults": [], "cleared": not ids},
                )
            )

        names = ", ".join(f"{d.id}. {d.name}" for d in defaults)
        log.info("brownfield_handler.set_defaults", ids=ids, names=names)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"Defaults updated: {names}",
                    ),
                ),
                is_error=False,
                meta={
                    "action": "set_defaults",
                    "defaults": [d.to_dict() for d in defaults],
                },
            )
        )
