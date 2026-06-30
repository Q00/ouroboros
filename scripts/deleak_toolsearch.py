#!/usr/bin/env python3
"""De-leak Claude-specific `ToolSearch` from shared SKILL.md prose.

Replaces the concrete runtime tool name `ToolSearch` with runtime-agnostic
"tool discovery" phrasing (matching the already-migrated seed/interview
template), while PRESERVING verbatim: the `+ouroboros X` discovery queries, the
`ouroboros_*` MCP tool names, the `deferred-schema guard` strings, and the
unstuck fail-loud contract meaning. The ubiquitous-language convention now lives
in the MCP server `instructions` field, so each runtime maps "tool discovery" to
its own mechanism (Claude -> ToolSearch) without the shared prose naming one.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Laggard skills still hardcoding ToolSearch (seed/interview already migrated).
SKILLS = [
    "evaluate",
    "run",
    "status",
    "evolve",
    "qa",
    "ralph",
    "pm",
    "setup",
    "brownfield",
    "unstuck",
]

# Ordered, literal replacements. Specific multi-word phrases first; a catch-all
# last guarantees no stray `ToolSearch` survives.
REPLACEMENTS: list[tuple[str, str]] = [
    (
        "Use the `ToolSearch` tool to find and load",
        "Use the active runtime's tool-discovery capability to find and load",
    ),
    ("If `ToolSearch` is not available", "If runtime tool discovery is not available"),
    ("If `ToolSearch` is available", "If runtime tool discovery is available"),
    ("After ToolSearch returns", "After runtime tool discovery returns"),
    ("If ToolSearch finds", "If runtime tool discovery finds"),
    ("loaded via ToolSearch above", "loaded via runtime tool discovery above"),
    ("names returned by `ToolSearch`", "names returned by runtime tool discovery"),
    (
        "Call `ToolSearch` with query",
        "Use the active runtime's tool-discovery capability with query",
    ),
    ("until `ToolSearch` runs", "until runtime tool discovery runs"),
    ("If `ToolSearch` cannot load", "If runtime tool discovery cannot load"),
    ("cannot be loaded via `ToolSearch`", "cannot be loaded via runtime tool discovery"),
    ("[ToolSearch loads", "[runtime tool discovery loads"),
    ("ToolSearch query:", "tool discovery query:"),
    # Catch-all stragglers (backticked then bare).
    ("`ToolSearch`", "runtime tool discovery"),
    ("ToolSearch", "runtime tool discovery"),
]


def main() -> int:
    trees = [REPO / "skills", REPO / ".claude-plugin" / "skills"]
    total_changed = 0
    leftovers: list[str] = []
    for tree in trees:
        for skill in SKILLS:
            path = tree / skill / "SKILL.md"
            if not path.exists():
                print(f"  SKIP (absent): {path}")
                continue
            text = path.read_text(encoding="utf-8")
            before = text.count("ToolSearch")
            for old, new in REPLACEMENTS:
                text = text.replace(old, new)
            after = text.count("ToolSearch")
            if after:
                leftovers.append(f"{path}: {after} ToolSearch remain")
            if before:
                path.write_text(text, encoding="utf-8")
                total_changed += 1
                print(f"  {path.relative_to(REPO)}: {before} -> {after}")
    print(f"\nFiles changed: {total_changed}")
    if leftovers:
        print("LEFTOVERS:")
        for line in leftovers:
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
