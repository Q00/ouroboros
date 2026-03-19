"""Brownfield Repository Management MCP tool handler.

Provides an ``ouroboros_brownfield`` MCP tool for managing brownfield
repository registrations in the SQLite database. Supports four actions:

- **scan** — Scan home directory for git repos with GitHub origins and
  register them in the DB. Optionally generates one-line descriptions
  via a Frugal-tier LLM.
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
from ouroboros.persistence.brownfield import BrownfieldStore

log = structlog.get_logger()

_TOOL_NAME = "ouroboros_brownfield"


def _detect_action(arguments: dict[str, Any]) -> str:
    """Auto-detect the action from parameter presence when action is omitted.

    Detection rules (evaluated in order):
    1. If ``action`` is explicitly provided, return it as-is.
    2. If ``path`` is present → ``"register"``
    3. Otherwise → ``"query"`` (safe default — read-only).
    """
    explicit = arguments.get("action")
    if explicit:
        return explicit

    if arguments.get("path"):
        return "register"

    return "query"


@dataclass
class BrownfieldHandler:
    """Handler for the ouroboros_brownfield MCP tool.

    Manages brownfield repository registrations with action-based dispatch:

    - ``scan`` — Walk ``~/`` for GitHub repos and register them.
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
                "Scan home directory for repos, register/query repos, "
                "or set the default brownfield context for PM interviews."
            ),
            parameters=(
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description=(
                        "Action to perform: 'scan' to discover repos from ~/,"
                        " 'register' to add a single repo,"
                        " 'query' to list all repos or get default,"
                        " 'set_default' to change the default repo."
                        " Auto-detected from parameters when omitted."
                    ),
                    required=False,
                    enum=("scan", "register", "query", "set_default"),
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
                        "One-line description of the repository. "
                        "Used with 'register'. Optional."
                    ),
                    required=False,
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
                        "Root directory for 'scan' action. Defaults to ~/."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="offset",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Number of rows to skip for 'query' pagination. "
                        "Defaults to 0."
                    ),
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

        try:
            if action == "scan":
                return await self._handle_scan(arguments)

            if action == "register":
                return await self._handle_register(arguments)

            if action == "query":
                return await self._handle_query(arguments)

            if action == "set_default":
                return await self._handle_set_default(arguments)

            return Result.err(
                MCPToolError(
                    f"Unknown action: {action!r}. "
                    "Must be one of: scan, register, query, set_default",
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

    # ──────────────────────────────────────────────────────────────
    # scan — Discover repos from home directory
    # ──────────────────────────────────────────────────────────────

    async def _handle_scan(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Scan home directory for git repos and register them.

        Delegates to ``bigbang.brownfield.scan_and_register`` which handles
        directory walking, GitHub origin filtering, LLM description generation,
        and DB upsert.
        """
        scan_root_str = arguments.get("scan_root")
        scan_root = Path(scan_root_str) if scan_root_str else None

        store = await self._get_store()

        # scan_and_register handles the full workflow:
        # walk dirs → filter GitHub origins → generate descs → upsert
        repos = await scan_and_register(
            store=store,
            llm_adapter=None,  # No LLM in MCP context for now
            root=scan_root,
        )

        repos_data = [r.to_dict() for r in repos]
        default = await store.get_default()

        summary = f"Scan complete. {len(repos)} repositories registered."
        if default:
            summary += f"\nDefault: {default.name} ({default.path})"

        log.info(
            "brownfield_handler.scan_complete",
            count=len(repos),
            default=default.path if default else None,
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
                    "default": default.to_dict() if default else None,
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
            default = await store.get_default()
            if default is None:
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
                        },
                    )
                )
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=f"Default: {default.name} ({default.path})",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "action": "query",
                        "default_only": True,
                        "default": default.to_dict(),
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
        default = await store.get_default()

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
                    },
                )
            )

        lines = [f"Brownfield repositories ({total} total, showing {len(repos)}):"]
        for r in repos:
            marker = " [default]" if r.is_default else ""
            desc_part = f" — {r.desc}" if r.desc else ""
            lines.append(f"  • {r.name}{marker}: {r.path}{desc_part}")

        repos_data = [r.to_dict() for r in repos]

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
                    "default": default.to_dict() if default else None,
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
        """Set a registered repo as the default brownfield context.

        Delegates to :func:`bigbang.brownfield.set_default_repo` for
        business-level validation and logging.
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

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"Default set to: {repo.name} ({repo.path})",
                    ),
                ),
                is_error=False,
                meta={
                    "action": "set_default",
                    "default": repo.to_dict(),
                },
            )
        )
