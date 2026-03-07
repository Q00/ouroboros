---
name: socratic-spec
description: "Run a Socratic specification interview using Ouroboros to turn vague ideas into validated YAML specs — before writing a single line of code or drafting any deliverable. Use when: (1) user says 'spec', 'socratic', 'clarify', 'interview me', 'think this through', or 'ouroboros', (2) user describes a vague project, feature, proposal, or strategy that needs structured clarification, (3) before any large deliverable (app, proposal, dashboard, training, strategy). NOT for: quick one-liner tasks, requests with already-clear requirements."
---

# Socratic Spec — Specification-First Clarity Engine

> Built on [Ouroboros](https://github.com/Q00/ouroboros) by @Q00.
> "Stop prompting. Start specifying."

Uses Ouroboros's actual `InterviewEngine`, `AmbiguityScorer`, and `SeedGenerator` via a thin CLI bridge. Domain-agnostic: code, proposals, training, product specs, strategy — anything that needs clarity before execution.

## Setup

Set the path to your Ouroboros install via env var:

```bash
export OUROBOROS_PATH="${OUROBOROS_PATH:-$HOME/ouroboros}"
```

All commands run through the bridge module:

```bash
cd "$OUROBOROS_PATH" && \
source .venv/bin/activate && \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
python -m ouroboros.integrations.openclaw_bridge <command> [args]
```

Session state persists to `$SOCRATIC_SESSIONS_DIR` (default: `~/socratic-sessions/`).

Override model: `SOCRATIC_MODEL=<litellm-model-string>`

## Interview Flow

### Phase 1 — Start

```bash
python -m ouroboros.integrations.openclaw_bridge start "user's initial idea"
```

Returns `session_id` + first Socratic question. Forward the question to the user.

### Phase 2 — Interview Loop

For each user response:

```bash
python -m ouroboros.integrations.openclaw_bridge respond <session-id> "user's answer"
```

Returns next question. Repeat until:
- User says "done" / "enough" / "let's go" / "c'est bon" → Phase 3
- After 10+ rounds → suggest scoring

Minimum 3 rounds before allowing exit.

### Phase 3 — Score Ambiguity

```bash
python -m ouroboros.integrations.openclaw_bridge score <session-id>
```

**Gate: score ≤ 0.2 (80% clarity) required to proceed.**

Report to user:
```
📊 Ambiguity Score: X.XX

  Goal Clarity:      0.XX (40%) — [justification]
  Constraints:       0.XX (30%) — [justification]
  Success Criteria:  0.XX (30%) — [justification]

  Verdict: ✅ READY / ❌ NEEDS WORK — [weak dimensions]
```

If NEEDS WORK → ask targeted follow-ups on weak dimensions, then re-score.

### Phase 4 — Generate Seed

```bash
python -m ouroboros.integrations.openclaw_bridge seed <session-id>
```

Generates an immutable YAML spec saved to `$SOCRATIC_SESSIONS_DIR/<id>-seed.yaml`. Present goal, constraints, and acceptance criteria to the user in clean Markdown.

### Phase 5 — Handoff

Ask the user:
- "Ready to execute based on this spec?"
- "Want to refine any section?"
- "Hand this to a coding agent / use for a proposal / etc.?"

The Seed is the contract for whatever comes next.

## Utility Commands

```bash
python -m ouroboros.integrations.openclaw_bridge status <session-id>    # check state
python -m ouroboros.integrations.openclaw_bridge complete <session-id>  # mark complete manually
```

## Notes

- Wraps Ouroboros's actual Python classes — not a reimplementation
- The 0.2 threshold is Ouroboros's: "remaining unknowns small enough that execution-level decisions can resolve them"
- Session state survives across conversations via JSON persistence
