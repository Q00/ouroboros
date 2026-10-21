---
name: tutorial
description: "Interactive tutorial teaching Ouroboros hands-on"
disable-model-invocation: true
---

# /ouroboros:tutorial

Interactive tutorial that teaches Ouroboros through hands-on experience.

## Usage

```
ooo tutorial
/ouroboros:tutorial
```

## Principles

1. Show something useful in the first 30 seconds.
2. Learn by doing: the user answers questions about their own idea.
3. Reveal features gradually.
4. Always end a phase with one specific next action.
5. Match the audience: technical users get the run, evaluate, and evolve
   details; non-technical users get the interview and plain language.

## Flow (about 5 minutes)

1. **Hook.** Promise that in five minutes their vague idea becomes a precise
   specification, then ask what they want to build. If they have no idea, use a
   task-management CLI as the example.
2. **Interview.** Ask 3-4 Socratic questions about their idea (the problem it
   solves, the main users, the one feature that makes it useful now). Point out
   that each answer surfaced an assumption they had not stated.
3. **Mini-Seed.** Summarize their answers in Seed vocabulary:

   ```
   Goal: <their goal, refined>
   Constraints: <2-3 constraints they named>
   Acceptance criteria: <2-3 checkable outcomes>
   ```

   Do not invent an ambiguity score. Explain that `ooo interview` measures
   ambiguity and continues until it is low enough to freeze a real Seed.
4. **The workflow.** `Interview → Seed → Execute → Evaluate`, with evaluation
   feeding back into the next Seed generation:

   ```
   ooo interview "your idea"   # expose assumptions
   ooo seed                    # freeze the Seed
   ooo run                     # execute it
   ooo evaluate                # verify against the acceptance criteria
   ooo status                  # check drift
   ```

   `ooo unstuck` helps when blocked. `run`, `evaluate`, `evolve`, and `status`
   need the Ouroboros MCP server (`ooo setup`).
5. **Next step.** Offer: start a real project (`ooo interview "your actual
   project idea"`), the full reference (`ooo help`), or setup (`ooo setup`).

If the user asks how this differs from prompting an AI assistant directly:
without a spec, the assistant guesses and you rewrite the prompt until it
matches; with Ouroboros, the interview exposes hidden assumptions first, so the
build starts from an agreed Seed. If it seems complicated, three commands cover
most use: `ooo interview`, `ooo seed`, `ooo run`.

The tutorial is done when the user has experienced the interview, understands
what a Seed is, and has chosen a next step.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
