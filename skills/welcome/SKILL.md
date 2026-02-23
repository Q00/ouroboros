---
name: welcome
description: "First-touch experience for new Ouroboros users"
---

# /ouroboros:welcome

Interactive onboarding for new Ouroboros users.

## Instructions

When this skill is invoked, follow this interactive flow step by step.

---

### Step 1: Welcome Banner

Display this welcome message:

```
Welcome to Ouroboros!

The serpent that eats itself -- better every loop.

Most AI coding fails at the input, not the output.
Ouroboros fixes this by exposing hidden assumptions
BEFORE any code is written.

Interview -> Seed -> Execute -> Evaluate
    ^                            |
    +---- Evolutionary Loop -----+
```

---

### Step 2: Persona Detection

Use **AskUserQuestion** to understand the user:

```json
{
  "questions": [{
    "question": "What brings you to Ouroboros?",
    "header": "Welcome",
    "options": [
      {
        "label": "New project idea",
        "description": "I have a vague idea and want to crystallize it into a clear spec"
      },
      {
        "label": "Tired of rewriting prompts",
        "description": "AI keeps building the wrong thing because my requirements are unclear"
      },
      {
        "label": "Just exploring",
        "description": "Heard about Ouroboros and want to see what it does"
      }
    ],
    "multiSelect": false
  }]
}
```

Based on their answer, give a brief personalized response (1-2 sentences):
- **New project idea**: "Perfect. Ouroboros will expose your hidden assumptions and turn that vague idea into a precise spec."
- **Tired of rewriting**: "You're in the right place. Ouroboros makes you specify BEFORE AI builds, so you get it right the first time."
- **Just exploring**: "Welcome! Let me show you how Ouroboros transforms messy requirements into crystal-clear specifications."

---

### Step 3: MCP Check

Check if MCP is configured:

```bash
cat ~/.claude/mcp.json 2>/dev/null | grep -q ouroboros && echo "MCP_OK" || echo "MCP_MISSING"
```

**If MCP_MISSING**, use **AskUserQuestion**:

```json
{
  "questions": [{
    "question": "Ouroboros has a Python backend for advanced features (TUI dashboard, 3-stage evaluation, drift tracking). Set it up now?",
    "header": "MCP Setup",
    "options": [
      {
        "label": "Set up now",
        "description": "Register MCP server (requires Python 3.14+)"
      },
      {
        "label": "Skip for now",
        "description": "Use basic features first (interview, seed, unstuck)"
      }
    ],
    "multiSelect": false
  }]
}
```

- **Set up now**: Read and execute `skills/setup/SKILL.md`, then return to Step 4.
- **Skip for now**: Continue to Step 4.

**If MCP_OK**: Continue to Step 4.

---

### Step 4: GitHub Star

Check `~/.ouroboros/prefs.json` for `star_asked`. If not `true`, use **AskUserQuestion**:

```json
{
  "questions": [{
    "question": "Ouroboros is free and open-source. A GitHub star helps other developers discover it. Star the repo?",
    "header": "Community",
    "options": [
      {
        "label": "Star on GitHub",
        "description": "Takes 1 second -- helps the project grow"
      },
      {
        "label": "Maybe later",
        "description": "Continue with setup"
      }
    ],
    "multiSelect": false
  }]
}
```

- **Star on GitHub**: Run `gh api -X PUT /user/starred/Q00/ouroboros 2>/dev/null`
- Both options: Create `~/.ouroboros/` if needed, save `{"star_asked": true, "welcome_shown": true}` to `~/.ouroboros/prefs.json`

If `star_asked` is already `true`, just ensure `welcome_shown` is set to `true`.

---

### Step 5: Quick Reference

Show this command overview:

```
Available Commands:
+---------------------------------------------------+
| Command         | What It Does                     |
|-----------------|----------------------------------|
| ooo interview   | Socratic Q&A -- expose hidden    |
|                 | assumptions in your requirements |
| ooo seed        | Crystallize answers into spec    |
| ooo run         | Execute with visual TUI          |
| ooo evaluate    | 3-stage verification             |
| ooo unstuck     | Lateral thinking when stuck      |
| ooo help        | Full command reference           |
+---------------------------------------------------+
```

---

### Step 6: First Action

Use **AskUserQuestion** to prompt immediate action:

```json
{
  "questions": [{
    "question": "What would you like to do first?",
    "header": "Get started",
    "options": [
      {
        "label": "Start a project",
        "description": "Run a Socratic interview on your idea right now"
      },
      {
        "label": "Try the tutorial",
        "description": "Interactive hands-on learning with a sample project"
      },
      {
        "label": "Read the docs",
        "description": "Full command reference and architecture overview"
      }
    ],
    "multiSelect": false
  }]
}
```

Based on their choice:
- **Start a project**: Ask "What do you want to build?" and then read and execute `skills/interview/SKILL.md` with their answer.
- **Try the tutorial**: Read and execute `skills/tutorial/SKILL.md`.
- **Read the docs**: Read and execute `skills/help/SKILL.md`.
