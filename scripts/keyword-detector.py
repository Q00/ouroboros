#!/usr/bin/env python3
"""Magic Keyword Detector for Ouroboros.

Detects trigger keywords in user prompts and suggests
the appropriate Ouroboros skill to invoke.

IMPORTANT: If MCP is not configured (ooo setup not run),
ALL ooo commands (except setup/help) redirect to setup first.

Hook: UserPromptSubmit
Input: User prompt text via stdin (piped by Claude Code)
Output: Modified prompt with skill suggestion appended
"""

import json
from pathlib import Path
import sys

# Skills that work without MCP setup (bypass the setup gate)
SETUP_BYPASS_SKILLS = ["/ouroboros:setup", "/ouroboros:help"]

# Keyword → skill mapping
# "ooo <cmd>" prefix always works; natural language keywords also supported
KEYWORD_MAP = [
    # ooo prefix shortcuts (checked first for priority)
    {"patterns": ["ooo interview", "ooo socratic"], "skill": "/ouroboros:interview"},
    {"patterns": ["ooo seed", "ooo crystallize"], "skill": "/ouroboros:seed"},
    {"patterns": ["ooo run", "ooo execute"], "skill": "/ouroboros:run"},
    {"patterns": ["ooo eval", "ooo evaluate"], "skill": "/ouroboros:evaluate"},
    {"patterns": ["ooo evolve"], "skill": "/ouroboros:evolve"},
    {"patterns": ["ooo stuck", "ooo unstuck", "ooo lateral"], "skill": "/ouroboros:unstuck"},
    {"patterns": ["ooo status", "ooo drift"], "skill": "/ouroboros:status"},
    {"patterns": ["ooo ralph"], "skill": "/ouroboros:ralph"},
    {"patterns": ["ooo tutorial"], "skill": "/ouroboros:tutorial"},
    {"patterns": ["ooo welcome"], "skill": "/ouroboros:welcome"},
    {"patterns": ["ooo setup"], "skill": "/ouroboros:setup"},
    {"patterns": ["ooo help"], "skill": "/ouroboros:help"},
    {"patterns": ["ooo qa"], "skill": "/ouroboros:qa"},
    {"patterns": ["ooo update", "ooo upgrade"], "skill": "/ouroboros:update"},
    # Natural language triggers
    {
        "patterns": [
            "interview me",
            "clarify requirements",
            "clarify my requirements",
            "socratic interview",
            "socratic questioning",
        ],
        "skill": "/ouroboros:interview",
    },
    {
        "patterns": ["crystallize", "generate seed", "create seed", "freeze requirements"],
        "skill": "/ouroboros:seed",
    },
    {
        "patterns": ["ouroboros run", "execute seed", "run seed", "run workflow"],
        "skill": "/ouroboros:run",
    },
    {
        "patterns": ["evaluate this", "3-stage check", "three-stage", "verify execution"],
        "skill": "/ouroboros:evaluate",
    },
    {
        "patterns": ["evolve", "evolutionary loop", "iterate until converged"],
        "skill": "/ouroboros:evolve",
    },
    {
        "patterns": [
            "think sideways",
            "i'm stuck",
            "im stuck",
            "i am stuck",
            "break through",
            "lateral thinking",
        ],
        "skill": "/ouroboros:unstuck",
    },
    {
        "patterns": [
            "am i drifting",
            "drift check",
            "session status",
            "check drift",
            "goal deviation",
        ],
        "skill": "/ouroboros:status",
    },
    {
        "patterns": ["ralph", "don't stop", "must complete", "until it works", "keep going"],
        "skill": "/ouroboros:ralph",
    },
    {"patterns": ["ouroboros setup", "setup ouroboros"], "skill": "/ouroboros:setup"},
    {"patterns": ["ouroboros help"], "skill": "/ouroboros:help"},
    {
        "patterns": ["update ouroboros", "upgrade ouroboros"],
        "skill": "/ouroboros:update",
    },
]


def is_mcp_configured() -> bool:
    """Check if MCP server is registered in ~/.claude/mcp.json."""
    try:
        mcp_path = Path.home() / ".claude" / "mcp.json"
        if not mcp_path.exists():
            return False
        return "ouroboros" in mcp_path.read_text()
    except Exception:
        return False


def is_first_time() -> bool:
    """Check if this is the user's first interaction (welcome not yet completed)."""
    try:
        prefs_path = Path.home() / ".ouroboros" / "prefs.json"
        if not prefs_path.exists():
            return True
        prefs = json.loads(prefs_path.read_text())
        return not prefs.get("welcomeCompleted", False)
    except Exception:
        return True


def detect_keywords(text: str) -> dict:
    """Detect keywords in user prompt text."""
    lower = text.lower().strip()

    for entry in KEYWORD_MAP:
        for pattern in entry["patterns"]:
            if pattern in lower:
                return {
                    "detected": True,
                    "keyword": pattern,
                    "suggested_skill": entry["skill"],
                }

    # Bare "ooo" (with no subcommand) → welcome (first-touch experience)
    if lower in ("ooo", "ooo?"):
        return {
            "detected": True,
            "keyword": "ooo",
            "suggested_skill": "/ouroboros:welcome",
        }

    return {"detected": False, "keyword": None, "suggested_skill": None}


def main() -> None:
    # Read user prompt from stdin
    try:
        user_input = sys.stdin.read().strip()
    except Exception:
        user_input = ""

    result = detect_keywords(user_input)

    # First-time user: append welcome suggestion to their first message
    if not result["detected"] and is_first_time():
        skill_name = "welcome"
        print(f"""{user_input}

<skill-suggestion>
🎯 MATCHED SKILLS (use AskUserQuestion to let user choose):
- /ouroboros:{skill_name} - First time using Ouroboros! Starting welcome experience.
IMPORTANT: Auto-triggering welcome experience now. Use AskUserQuestion to confirm or skip.
</skill-suggestion>
""")
        return

    if result["detected"]:
        skill = result["suggested_skill"]
        keyword = result["keyword"]

        # Gate check: if MCP not configured and skill requires it, redirect to setup
        if skill not in SETUP_BYPASS_SKILLS and not is_mcp_configured():
            print(f"""{user_input}

<skill-suggestion>
🎯 REQUIRED SKILL:
- /ouroboros:setup - Ouroboros setup required. Run "ooo setup" first to register the MCP server.
</skill-suggestion>
""")
        else:
            skill_name = skill.replace("/ouroboros:", "")
            print(f"""{user_input}

<skill-suggestion>
🎯 MATCHED SKILLS:
- /ouroboros:{skill_name} - Detected "{keyword}"
</skill-suggestion>
""")
    else:
        # Pass through unchanged when no keyword detected
        print(user_input)


if __name__ == "__main__":
    main()
