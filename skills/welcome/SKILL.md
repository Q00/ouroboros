---
name: welcome
description: "First-touch experience for new Ouroboros users"
---

# /ouroboros:welcome

First-touch experience that converts new users into engaged community members.

## Usage

```
ooo
/ouroboros:welcome
```

## Response

When this skill is invoked:

1. **Check MCP configuration first:**
   ```bash
   cat ~/.claude/mcp.json 2>/dev/null | grep -q ouroboros && echo "MCP_OK" || echo "MCP_MISSING"
   ```

2. **If MCP_MISSING**: After showing the welcome message below, append a setup prompt at the end instead of the normal "What would you like to build today?" ending:
   ```
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
     One-Time Setup Required
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

   Ouroboros needs its MCP server registered to work properly.
   This is a one-time setup that takes ~1 minute.

   Run: ooo setup
   ```
   Then use **AskUserQuestion** to prompt setup:
   ```json
   {
     "questions": [{
       "question": "Run setup now to unlock all Ouroboros features?",
       "header": "Setup",
       "options": [
         { "label": "Run ooo setup", "description": "Register MCP server now (recommended)" },
         { "label": "Skip for now", "description": "Use basic features only (interview, seed, unstuck)" }
       ],
       "multiSelect": false
     }]
   }
   ```
   - **Run ooo setup**: Read and execute `skills/setup/SKILL.md`
   - **Skip for now**: Continue normally

3. **If MCP_OK**: Show the welcome message below as-is.

Respond with EXACTLY the following:

---

Welcome to Ouroboros! The serpent that eats itself — better every loop.

You're about to transform how you work with AI. No setup required.

---

## What Makes Ouroboros Different

**The Problem:** When you say "build me X", AI guesses what you want. You get something, realize it's wrong, rewrite prompts, and repeat. Hours wasted debugging requirements, not code.

**The Solution:** Ouroboros exposes hidden assumptions BEFORE any code is written. Through Socratic questioning, vague ideas become crystal-clear specifications. Then AI builds exactly what you specified. First try.

---

## Try It Right Now (No Setup)

```
ooo interview "I want to build a [your project idea]"
```

Watch as hidden assumptions get exposed:
- "Who are the primary users?"
- "What happens when [edge case]?"
- "Is [feature X] essential or nice-to-have?"

3 minutes later, you have a validated specification ready for AI execution.

---

## What You Can Do (Right Now)

| Command | What It Does | Setup Needed |
|:--------|:-------------|:-------------|
| `ooo interview` | Socratic questioning exposes assumptions | None |
| `ooo seed` | Crystallizes answers into immutable spec | None |
| `ooo unstuck` | 5 lateral thinking personas break blocks | None |
| `ooo tutorial` | Interactive hands-on learning | None |

---

## What You Can Do (With Quick Setup)

Run `ooo setup` (2 minutes) to unlock:

| Command | What It Does |
|:--------|:-------------|
| `ooo run` | Execute with visual TUI dashboard |
| `ooo evaluate` | 3-stage verification (Mechanical → Semantic → Consensus) |
| `ooo status` | Real-time drift detection |

---

## Why Not Just Prompt Claude Directly?

| Approach | Result |
|:---------|:-------|
| "Build me a task CLI" | Claude guesses → Wrong output → Rewrite prompt → Repeat |
| `ooo interview` → `ooo seed` → `ooo run` | Assumptions exposed → Precise spec → Right output, first try |

**The difference:** Ouroboros saves you hours of iteration by clarifying requirements upfront.

---

## Ouroboros vs oh-my-claudecode (If You're Coming from OMC)

You might be wondering how Ouroboros compares to OMC:

| What | Ouroboros | OMC |
|:-----|:----------|:-----|
| **Best For** | New projects with unclear requirements | Existing codebases |
| **Interface** | Visual TUI dashboard + CLI | CLI only |
| **Approach** | Specification-first (crystallize requirements) | Execution-first (build immediately) |
| **Cost** | 85% savings via PAL Router | Manual optimization |
| **Quality** | 3-stage evaluation pipeline | Agent-based review |
| **Debugging** | Full session replay | Session resume |

**Bottom line:** Use Ouroboros when starting something new. Use OMC when working with existing code. They work great together.

---

## Your First 5 Minutes With Ouroboros

### Minute 1: The Aha Moment
```
ooo interview "Build a personal finance tracker"
```
Answer 5-10 clarifying questions. Notice how each question reveals assumptions you didn't know you had.

### Minute 2: The Specification
```
ooo seed
```
Watch your answers crystallize into an immutable Seed specification with ambiguity scoring.

### Minute 3-5: Optional Power Mode
```
ooo setup  # Quick 2-minute setup
ooo run    # Execute with visual TUI
```

---

## Join the Community

Found this useful? Star us on GitHub!

Every star helps more developers discover Ouroboros and stop wasting time on vague requirements.

[github.com/Q00/ouroboros](https://github.com/Q00/ouroboros)

---

## Ready to Build Something Amazing?

Pick your path:

**Path A: Dive In** (Recommended)
```
ooo interview "your actual project idea"
```

**Path B: Learn First**
```
ooo tutorial  # Interactive hands-on tutorial
```

**Path C: Explore**
```
ooo help  # Full command reference
```

---

What would you like to build today?
