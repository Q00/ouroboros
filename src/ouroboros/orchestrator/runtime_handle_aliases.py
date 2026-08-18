"""Selector aliases for the ``RuntimeHandle`` backend boundary.

Split out of ``orchestrator/adapter.py`` (module-size ratchet, #1797): the
table itself has no behavior, just data, so it can live anywhere importable
by that module.
"""

from __future__ import annotations

# Keep this boundary map limited to canonical selectors and legacy spellings
# already exercised by current runtimes or persisted RuntimeHandle payloads.
RUNTIME_HANDLE_BACKEND_ALIASES: dict[str, str] = {
    "claude": "claude",
    "claude_code": "claude",
    "claude_mcp": "claude_mcp",
    "codex": "codex_cli",
    "codex_cli": "codex_cli",
    "codex_mcp": "codex_mcp",
    "opencode": "opencode",
    "opencode_cli": "opencode",
    "hermes": "hermes_cli",
    "hermes_cli": "hermes_cli",
    "kiro": "kiro",
    "kiro_cli": "kiro",
    "copilot": "copilot_cli",
    "copilot_cli": "copilot_cli",
    "goose": "goose",
    "goose_cli": "goose",
    "pi": "pi",
    "pi_cli": "pi",
    "gjc": "gjc",
    "gjc_cli": "gjc",
    "gemini": "gemini_cli",
    "gemini_cli": "gemini_cli",
    "grok": "grok_cli",
    "grok_cli": "grok_cli",
    "antigravity": "antigravity_cli",
    "antigravity_cli": "antigravity_cli",
    "zcode": "zcode_cli",
    "zcode_cli": "zcode_cli",
    "host": "host",
    "host_dispatch": "host",
}
