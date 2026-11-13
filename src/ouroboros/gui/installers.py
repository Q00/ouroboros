"""One-click install commands for the agent tools on the Connect page.

Only commands published by each tool's own docs are listed (sources noted
inline). The page shows the exact command and asks before running it; a
tool with no published one-liner gets a link to its install guide instead.
"""

from __future__ import annotations

import shutil
import sys
from typing import Any

_NPM = "npm"
_BREW = "brew"
_CURL = "curl"

# id -> ordered methods. "argv" runs without a shell; "script" pipes a
# published installer URL into bash, exactly as the vendor documents it.
_METHODS: dict[str, list[dict[str, Any]]] = {
    # code.claude.com/docs (native installer is the documented default)
    "claude": [
        {
            "id": "script",
            "label": "Official installer",
            "script": "https://claude.ai/install.sh",
            "requires": _CURL,
        },
        {
            "id": "npm",
            "label": "npm",
            "argv": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
            "requires": _NPM,
        },
    ],
    # docs/runtime-guides/codex.md
    "codex": [
        {
            "id": "npm",
            "label": "npm",
            "argv": ["npm", "install", "-g", "@openai/codex"],
            "requires": _NPM,
        },
        {
            "id": "brew",
            "label": "Homebrew",
            "argv": ["brew", "install", "--cask", "codex"],
            "requires": _BREW,
        },
    ],
    # docs/runtime-guides/opencode.md
    "opencode": [
        {
            "id": "script",
            "label": "Official installer",
            "script": "https://opencode.ai/install",
            "requires": _CURL,
        },
        {
            "id": "npm",
            "label": "npm",
            "argv": ["npm", "install", "-g", "opencode-ai@latest"],
            "requires": _NPM,
        },
    ],
    # docs/runtime-guides/gemini.md
    "gemini": [
        {
            "id": "npm",
            "label": "npm",
            "argv": ["npm", "install", "-g", "@google/gemini-cli"],
            "requires": _NPM,
        },
    ],
    # docs.github.com/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli
    "copilot": [
        {
            "id": "npm",
            "label": "npm",
            "argv": ["npm", "install", "-g", "@github/copilot"],
            "requires": _NPM,
        },
    ],
    # kiro.dev/docs/cli/installation
    "kiro": [
        {
            "id": "script",
            "label": "Official installer",
            "script": "https://cli.kiro.dev/install",
            "requires": _CURL,
        },
        {
            "id": "brew",
            "label": "Homebrew",
            "argv": ["brew", "install", "kiro-cli"],
            "requires": _BREW,
        },
    ],
    # block.github.io/goose/docs/getting-started/installation
    "goose": [
        {
            "id": "brew",
            "label": "Homebrew",
            "argv": ["brew", "install", "block-goose-cli"],
            "requires": _BREW,
        },
        {
            "id": "script",
            "label": "Official installer",
            "script": "https://github.com/block/goose/releases/download/stable/download_cli.sh",
            "env": "CONFIGURE=false",
            "requires": _CURL,
        },
    ],
    # hermes-agent.nousresearch.com/docs/getting-started/installation
    "hermes": [
        {
            "id": "script",
            "label": "Official installer",
            "script": "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh",
            "requires": _CURL,
        },
    ],
    # docs/runtime-guides/pi.md
    "pi": [
        {
            "id": "npm",
            "label": "npm",
            "argv": ["npm", "install", "-g", "--ignore-scripts", "@earendil-works/pi-coding-agent"],
            "requires": _NPM,
        },
    ],
    # docs/runtime-guides/grok.md
    "grok": [
        {
            "id": "script",
            "label": "Official installer",
            "script": "https://x.ai/cli/install.sh",
            "requires": _CURL,
        },
    ],
}


def _display(method: dict[str, Any]) -> str:
    if "script" in method:
        env = f"{method['env']} " if method.get("env") else ""
        return f"curl -fsSL {method['script']} | {env}bash"
    return " ".join(method["argv"])


def available(method: dict[str, Any]) -> bool:
    """True when the tool the method needs (npm, brew, curl) is on PATH."""
    if sys.platform == "win32" and "script" in method:
        return False
    return shutil.which(str(method["requires"])) is not None


def methods_for(client_id: str) -> list[dict[str, Any]]:
    """Serializable install options for one tool, best first."""
    return [
        {
            "id": method["id"],
            "label": method["label"],
            "command": _display(method),
            "requires": method["requires"],
            "available": available(method),
        }
        for method in _METHODS.get(client_id, [])
    ]


def find(client_id: str, method_id: str) -> dict[str, Any] | None:
    for method in _METHODS.get(client_id, []):
        if method["id"] == method_id:
            return method
    return None


def argv(method: dict[str, Any]) -> list[str]:
    """The child-process argv for a method (fixed tokens; nothing user-supplied)."""
    if "script" in method:
        env = f"{method['env']} " if method.get("env") else ""
        return ["/bin/bash", "-c", f"set -o pipefail; curl -fsSL {method['script']} | {env}bash"]
    return list(method["argv"])


__all__ = ["argv", "available", "find", "methods_for"]
