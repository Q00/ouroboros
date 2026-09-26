"""Bounded, headless permissions for the experimental Copilot ACP transport.

The CLI owns tool filtering; the ACP client only grants one-shot read, edit,
and execution requests in the selected workspace. This is an approval policy,
not an OS sandbox: an explicitly permitted shell still runs as the local user.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ouroboros.copilot_permissions import resolve_copilot_permission_mode

_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "Read": ("view",),
    "Glob": ("glob",),
    "Grep": ("grep",),
    "Edit": ("edit",),
    "MultiEdit": ("edit",),
    "Write": ("create",),
    "Bash": ("bash", "read_bash", "stop_bash", "list_bash"),
    "Task": ("task", "read_agent", "write_agent", "list_agents"),
    "WebFetch": ("web_fetch",),
    "WebSearch": ("web_search",),
}
_DEFAULT_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "Bash")
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]*\Z")


def resolve_acp_permission_mode(permission_mode: str | None) -> str:
    """Keep the runner's mandatory bypass request bounded to workspace-write."""
    mode = resolve_copilot_permission_mode(permission_mode)
    return "acceptEdits" if mode == "bypassPermissions" else mode


@dataclass(frozen=True, slots=True)
class CopilotAcpPermissions:
    """An immutable per-invocation tool envelope and workspace selection."""

    cwd: Path
    mode: str
    tools: tuple[str, ...]
    _empty_tool_marker: str = field(
        default_factory=lambda: f"ouroboros_no_tools_{uuid4().hex}", init=False, repr=False
    )

    @classmethod
    def for_task(
        cls, cwd: str, permission_mode: str, tools: list[str] | None
    ) -> CopilotAcpPermissions:
        mode = resolve_acp_permission_mode(permission_mode)
        names: list[str] = []
        # Validate even in no-tools mode; never forward punctuation as CLI syntax.
        for name in _DEFAULT_TOOLS if tools is None else tools:
            if not isinstance(name, str) or not _SAFE_TOOL_NAME.fullmatch(name):
                raise ValueError(f"Invalid Copilot tool name: {name!r}")
            names.extend(_TOOL_ALIASES.get(name, (name,)))
        return cls(
            cwd=Path(cwd).resolve(),
            mode=mode,
            tools=tuple(dict.fromkeys(names)) if mode != "default" else (),
        )

    def cli_args(self) -> list[str]:
        """Expose exactly the admitted tools; never enable blanket approvals."""
        # Copilot 1.0.83 treats an empty value as unrestricted. A nonmatching
        # allow-list disables tools; a per-task nonce avoids extension collisions.
        names = ",".join(self.tools) if self.tools else self._empty_tool_marker
        return [f"--available-tools={names}"]

    @property
    def empty_tool_marker(self) -> str | None:
        """Identify Copilot's no-tools compatibility notice, not assistant text."""
        return self._empty_tool_marker if not self.tools else None

    def request_permission(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Select allow_once only for understood requests, otherwise cancel."""
        denied = {"outcome": {"outcome": "cancelled"}}
        tool = params.get("toolCall")
        options = params.get("options")
        if not self.tools or not isinstance(tool, Mapping) or not isinstance(options, list):
            return denied
        kind = tool.get("kind")
        required_tools = {
            "read": {"view", "glob", "grep"},
            "search": {"grep", "glob"},
            "edit": {"edit", "create"},
            "execute": {"bash", "read_bash", "stop_bash", "list_bash"},
        }
        if not isinstance(kind, str) or not required_tools.get(kind, set()).intersection(
            self.tools
        ):
            return denied
        raw_input = tool.get("rawInput", {})
        if not isinstance(raw_input, Mapping):
            return denied
        locations = tool.get("locations", [])
        if not isinstance(locations, list):
            return denied
        paths: list[Any] = []
        for location in locations:
            if not isinstance(location, Mapping):
                return denied
            paths.append(location.get("path"))
        for key in (
            "path",
            "file_path",
            "fileName",
            "cwd",
            "workingDirectory",
            "working_directory",
        ):
            if key in raw_input:
                paths.append(raw_input[key])
        extra_paths = raw_input.get("paths", [])
        if isinstance(extra_paths, str):
            extra_paths = [extra_paths]
        if not isinstance(extra_paths, list):
            return denied
        paths.extend(extra_paths)
        for value in paths:
            if not isinstance(value, str) or not value.strip():
                return denied
            try:
                path = Path(value).expanduser()
                path = path if path.is_absolute() else self.cwd / path
                if not path.resolve().is_relative_to(self.cwd):
                    return denied
            except (OSError, ValueError, RuntimeError):
                return denied
        # A write without a target is not enough evidence to approve a mutation.
        if kind == "edit" and not paths:
            return denied
        for option in options:
            if (
                isinstance(option, Mapping)
                and option.get("kind") == "allow_once"
                and isinstance(option.get("optionId"), str)
                and option["optionId"]
            ):
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return denied

    @property
    def allows_cli_fallback(self) -> bool:
        """Only no-tools/read-only envelopes can safely omit ACP approval RPCs."""
        return set(self.tools) <= {"view", "glob", "grep"}


__all__ = ["CopilotAcpPermissions", "resolve_acp_permission_mode"]
