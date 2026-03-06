# OpenClaw Integration

> **[OpenClaw](https://github.com/openclaw/openclaw)** is a personal AI gateway that routes messages from Telegram, WhatsApp, Discord, and other channels to AI agents. This integration lets you run Ouroboros specification interviews over any of those channels — no terminal required.

---

## How It Works

```
User (Telegram/WhatsApp/etc)
        │
        ▼
  OpenClaw Agent
  (reads SKILL.md)
        │
        ▼
  openclaw_bridge.py          ← thin CLI wrapper (one-shot per call)
        │
        ▼
  Ouroboros Core Classes      ← InterviewEngine, AmbiguityScorer, SeedGenerator
  (via LiteLLM)
        │
        ▼
  Session State (JSON)        ← persisted between messages
```

The bridge is designed for async, message-based environments: each command does **one step and exits**, persisting state to a JSON file so the agent can pick it up on the next message — even minutes later.

---

## Installation

**Prerequisites:** Python 3.14+, `uv`, an Anthropic (or OpenAI) API key.

```bash
# Clone the repo
git clone https://github.com/Q00/ouroboros
cd ouroboros

# Install dependencies
uv sync

# Test the bridge
python3.14 openclaw_bridge.py start "I want to build a task manager"
```

**Install the OpenClaw skill:**

Copy `skills/socratic-spec/` to your OpenClaw skills directory:

```bash
cp -r skills/socratic-spec ~/.openclaw/skills/socratic-spec
```

---

## The Bridge CLI

`openclaw_bridge.py` is a thin wrapper around Ouroboros's actual classes. It does not reimplement any logic — it delegates directly to `InterviewEngine`, `AmbiguityScorer`, and `SeedGenerator`.

### Commands

```bash
# Start a new interview
python3.14 openclaw_bridge.py start "your idea or goal"

# Record a user response and get the next question
python3.14 openclaw_bridge.py respond <session-id> "user's answer"

# Score ambiguity (0.0 = fully clear, 1.0 = fully vague)
python3.14 openclaw_bridge.py score <session-id>

# Generate a YAML Seed spec from the interview
python3.14 openclaw_bridge.py seed <session-id>

# Check interview state
python3.14 openclaw_bridge.py status <session-id>

# Mark interview as complete
python3.14 openclaw_bridge.py complete <session-id>
```

All commands output **JSON** — easy to parse by any agent runtime.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required (or `OPENAI_API_KEY`) |
| `SOCRATIC_MODEL` | `claude-sonnet-4-5` | LiteLLM model string |
| `SOCRATIC_SESSIONS_DIR` | `~/openclaw/workspace/socratic-sessions/` | Session state directory |

---

## The OpenClaw Skill

The `socratic-spec` skill (`skills/socratic-spec/SKILL.md`) tells OpenClaw agents:

- **When to activate** — on keywords like "spec", "socratic", "clarify", "interview me"
- **The interview loop** — start → respond → score → seed → handoff
- **The ambiguity gate** — `≤ 0.2` required before generating a seed
- **How to format** — scores, questions, and seed presented in clean Markdown

The agent calls the bridge via `exec`, reads the JSON output, and forwards questions/results to the user in natural language — over whatever channel they're using.

---

## Interview Flow

```
1. User: "I want to build a trading bot"
          │
          ▼
2. Agent → bridge start → returns first Socratic question
3. Agent forwards question to user

4. User answers
          │
          ▼
5. Agent → bridge respond → returns next question
   ... (minimum 3 rounds) ...

6. User: "c'est bon" / "done" / "let's go"
          │
          ▼
7. Agent → bridge score → returns ambiguity breakdown
   e.g. Score: 0.18 ✅ READY  (or 0.42 ❌ NEEDS WORK → more rounds)

8. Agent → bridge seed → returns YAML spec saved to session dir

9. Agent presents Seed and asks: execute / refine / hand off?
```

---

## Example Output

### Ambiguity Score

```json
{
  "session_id": "interview_20260305_001842",
  "ambiguity_score": 0.18,
  "verdict": "READY",
  "dimensions": {
    "goal_clarity": { "score": 0.12, "justification": "Goal is concrete and measurable" },
    "constraint_clarity": { "score": 0.20, "justification": "Tech stack defined" },
    "success_criteria": { "score": 0.22, "justification": "Acceptance criteria specific" }
  }
}
```

### Generated Seed (excerpt)

```yaml
goal: "Build a spec-first task management CLI for solo developers"
constraints:
  - "Python 3.14+"
  - "Terminal UI (Textual)"
  - "Offline-first, SQLite storage"
acceptance_criteria:
  - "Create, complete, and delete tasks via keyboard shortcuts"
  - "All state persists across restarts"
metadata:
  ambiguity_score: 0.18
```

---

## Architecture Notes

- The bridge uses **one-shot execution** — no long-running process, no daemon
- State is stored as **plain JSON** files, one per session
- All LLM calls go through **LiteLLM**, respecting the `top_p` fix for Anthropic models (see PR #XXX)
- Works with any OpenClaw-compatible agent runtime (main agent, dedicated agents, etc.)

---

## Files

```
ouroboros/
  openclaw_bridge.py          ← bridge CLI (lives at repo root)
  skills/
    socratic-spec/
      SKILL.md                ← OpenClaw skill definition
```

---

## Related

- [Running with Claude Code](../running-with-claude-code.md)
- [Getting Started](../getting-started.md)
- [Ouroboros GitHub](https://github.com/Q00/ouroboros)
