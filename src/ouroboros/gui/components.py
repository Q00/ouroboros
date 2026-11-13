"""Optional components of this Ouroboros install, and one-click installs for them.

Each component is one of the package's extras (``[claude]``, ``[litellm]``,
``[mcp]``, ``[tui]``). Versions come from the installed package's own
metadata, so an install here is exactly ``pip install 'ouroboros-ai[<extra>]'``
for the running version — into the interpreter that runs the console.

``[claude]`` pins MCP 1.x and ``[mcp]`` pins MCP 2; one interpreter holds one
of them. The plan says so before anything is installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import re
import shutil
import sys
from typing import Any

_DIST = "ouroboros-ai"


@dataclass(frozen=True, slots=True)
class Component:
    extra: str
    label: str
    probe: str  # distribution whose presence means "installed"
    purpose: str


COMPONENTS: tuple[Component, ...] = (
    Component(
        "claude",
        "Claude Agent SDK",
        "claude-agent-sdk",
        "Runs Claude work in-process through the Agent SDK (the `claude` runtime).",
    ),
    Component(
        "litellm",
        "LiteLLM",
        "litellm",
        "Calls API providers (OpenRouter, OpenAI, Anthropic, Gemini, local servers…).",
    ),
    Component(
        "mcp",
        "MCP server (MCP 2)",
        "mcp",
        "Serves the ooo tools to Codex, Kiro, Copilot and other MCP hosts from this install.",
    ),
    Component(
        "tui",
        "Terminal UI",
        "textual",
        "The `ouroboros tui` monitor and the terminal settings app.",
    ),
)
_BY_EXTRA = {c.extra: c for c in COMPONENTS}
_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(==\s*[^;\s]+)?")


def _installed(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _pins(extra: str) -> list[str]:
    """Requirement strings the installed package declares for ``extra``."""
    pins: list[str] = []
    for requirement in metadata.requires(_DIST) or []:
        if f"extra == '{extra}'" not in requirement and f'extra == "{extra}"' not in requirement:
            continue
        python_marker = re.search(r"python_version\s*<\s*'([0-9.]+)'", requirement)
        if python_marker:
            limit = tuple(int(part) for part in python_marker.group(1).split("."))
            if sys.version_info[: len(limit)] >= limit:
                continue
        match = _REQ.match(requirement)
        if match:
            pins.append(match.group(1) + (match.group(2) or "").replace(" ", ""))
    return pins


def _mcp_major() -> int | None:
    version = _installed("mcp")
    try:
        return int(version.split(".", 1)[0]) if version else None
    except ValueError:
        return None


def _conflict(extra: str) -> str | None:
    major = _mcp_major()
    if extra == "claude" and major == 2:
        return (
            "This interpreter has MCP 2 (the [mcp] server profile). The Claude Agent SDK "
            "needs MCP 1.x, so installing it replaces MCP 2 here. Claude Code keeps working "
            "without the SDK: the console runs Claude work through the CLI worker."
        )
    if extra == "mcp" and _installed("claude-agent-sdk"):
        return (
            "This interpreter has the Claude Agent SDK (MCP 1.x). Installing MCP 2 here "
            "breaks the SDK. MCP hosts normally launch an isolated `uvx` MCP server instead."
        )
    return None


def installer_argv(packages: list[str]) -> list[str]:
    """Install ``packages`` into the interpreter running the console."""
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, *packages]
    return [sys.executable, "-m", "pip", "install", *packages]


def view() -> dict[str, Any]:
    rows = []
    for component in COMPONENTS:
        pins = _pins(component.extra)
        installed = _installed(component.probe)
        wanted = next(
            (p.split("==", 1)[1] for p in pins if p.startswith(component.probe + "==")), None
        )
        rows.append(
            {
                "extra": component.extra,
                "label": component.label,
                "purpose": component.purpose,
                "installed": installed,
                "wanted": wanted,
                "available": bool(pins),
                "packages": pins,
                "command": " ".join(installer_argv(pins)) if pins else None,
                "conflict": _conflict(component.extra),
            }
        )
    return {
        "python": sys.executable,
        "version": _installed(_DIST),
        "components": rows,
        "restart_needed_hint": "Restart the console after installing so it loads the new packages.",
    }


def plan(extra: str) -> dict[str, Any]:
    component = _BY_EXTRA.get(extra)
    if component is None:
        raise KeyError(extra)
    pins = _pins(extra)
    if not pins:
        raise ValueError(f"This Ouroboros build declares no packages for [{extra}].")
    return {
        "extra": extra,
        "label": component.label,
        "packages": pins,
        "argv": installer_argv(pins),
        "conflict": _conflict(extra),
    }


__all__ = ["COMPONENTS", "installer_argv", "plan", "view"]
