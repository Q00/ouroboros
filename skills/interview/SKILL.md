---
name: interview
description: "Socratic interview to crystallize vague requirements"
mcp_tool: ouroboros_interview
mcp_args:
  initial_context: "$1"
  cwd: "$CWD"
---

# /ouroboros:interview

Socratic interview to crystallize vague requirements into clear specifications.

## Usage

```
ooo interview [topic]
/ouroboros:interview [topic]
```

**Trigger keywords:** "interview me", "clarify requirements"

## Instructions

When the user invokes this skill:

### Step 0: Version Check (runs before interview)

Before starting the interview, check if a newer version is available:

```bash
# Fetch latest release tag from GitHub (timeout 3s to avoid blocking)
curl -s --max-time 3 https://api.github.com/repos/Q00/ouroboros/releases/latest | grep -o '"tag_name": "[^"]*"' | head -1
```

Compare the result with the current version in `.claude-plugin/plugin.json`.
- If a newer version exists, ask the user via `AskUserQuestion`:
  ```json
  {
    "questions": [{
      "question": "Ouroboros <latest> is available (current: <local>). Update before starting?",
      "header": "Update",
      "options": [
        {"label": "Update now", "description": "Update plugin to latest version (restart required to apply)"},
        {"label": "Skip, start interview", "description": "Continue with current version"}
      ],
      "multiSelect": false
    }]
  }
  ```
  - If "Update now":
    1. Run `claude plugin marketplace update ouroboros` via Bash (refresh marketplace index). If this fails, tell the user "⚠️ Marketplace refresh failed, continuing…" and proceed.
    2. Run `claude plugin update ouroboros@ouroboros` via Bash (update plugin/skills). If this fails, inform the user and stop — do NOT proceed to step 3.
    3. Detect the user's Python package manager and upgrade the MCP server:
       - Check which tool installed `ouroboros-ai` by running these in order:
         - `uv tool list 2>/dev/null | grep "^ouroboros-ai "` → if found, use `uv tool upgrade ouroboros-ai`
         - `pipx list 2>/dev/null | grep "^  ouroboros-ai "` → if found, use `pipx upgrade ouroboros-ai`
         - Otherwise, print: "Also upgrade the MCP server: `pip install --upgrade ouroboros-ai`" (do NOT run pip automatically)
    4. Tell the user: "Updated! Restart your session to apply, then run `ooo interview` again."
  - If "Skip": proceed immediately.
- If versions match, the check fails (network error, timeout, rate limit 403/429), or parsing fails/returns empty: **silently skip** and proceed.

Then choose the execution path:

### Step 0.5: Load MCP Tools (Required before Path A/B decision)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step before deciding between Path A and Path B.**

1. Use the `ToolSearch` tool to find and load the interview MCP tool:
   ```
   ToolSearch query: "+ouroboros interview"
   ```
   This searches for tools with "ouroboros" in the name related to "interview".

2. The tool will typically be named `mcp__plugin_ouroboros_ouroboros__ouroboros_interview` (with a plugin prefix). After ToolSearch returns, the tool becomes callable.

3. If ToolSearch finds the tool → proceed to **Path A**.
   If ToolSearch returns no matching tools → proceed to **Path B**.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Path A: MCP Mode (Preferred)

If the `ouroboros_interview` MCP tool is available (loaded via ToolSearch above), use it for persistent, structured interviews.

**Architecture**: MCP is a pure question generator. You (the main session) are the answerer and router.

```
MCP (question generator) ←→ You (answerer + router) ←→ User (human judgment only)
```

**Role split**:
- **MCP**: Generates Socratic questions, manages interview state, scores ambiguity. Does NOT read code.
- **You (main session)**: Receives MCP questions, answers them by reading code (Read/Glob/Grep), or routes to the user when human judgment is needed.
- **User**: Only answers questions that require human decisions (goals, acceptance criteria, business logic, preferences).

#### Interview Flow

1. **Start a new interview**:
   ```
   Tool: ouroboros_interview
   Arguments:
     initial_context: <user's topic or idea>
     cwd: <current working directory>
   ```
   Returns a session ID and the first question.

2. **For each question from MCP, apply the routing paths below:**

   **PATH 1 — Code Answer** (describe current state from codebase):
   When the question asks about existing tech stack, frameworks, dependencies,
   current patterns, architecture, or file structure:
   - Use Read/Glob/Grep to find the factual answer
   - **Description, not prescription**: "The project uses JWT" is fact.
     "The new feature should also use JWT" is a DECISION — route to PATH 2.
   - Evaluate confidence and choose sub-path:

   **PATH 1a — Auto-confirm** (high-confidence factual, no user block):
   When ALL of the following are true:
   - The answer is found as an **exact match** in a manifest or config file
     (e.g., `pyproject.toml`, `package.json`, `Dockerfile`, `go.mod`, `.env.example`)
   - The answer is **purely descriptive** — it describes what exists, not what
     the new feature should do
   - There is **no ambiguity** — a single, clear answer (not multiple candidates)

   Then:
   - Send the answer to MCP immediately with `[from-code][auto-confirmed]` prefix
   - Display a brief notification to the user (do NOT block):
     `"ℹ️ Auto-confirmed: Python 3.12, FastAPI framework (pyproject.toml)"`
   - The user can correct at any time by saying "that's wrong" — re-send correction to MCP
   - Increment the auto-confirm counter (see Dialectic Rhythm Guard below)

   Examples of auto-confirmable facts:
   - Programming language (from pyproject.toml, package.json, go.mod)
   - Framework (from dependencies in manifest)
   - Python/Node version (from config files)
   - Package manager (from lock files present)
   - CI/CD tool (from .github/workflows/, Jenkinsfile, etc.)

   **PATH 1b — Code Confirmation** (medium/low confidence, user confirms):
   When the codebase has relevant information but confidence is not high enough
   for auto-confirm (inferred from patterns, multiple candidates, or no manifest match):
   - Present findings to user as a **confirmation question** via AskUserQuestion:
     ```json
     {
       "questions": [{
         "question": "MCP asks: What auth method does the project use?\n\nI found: JWT-based auth in src/auth/jwt.py\n\nIs this correct?",
         "header": "Q<N> — Code Confirmation",
         "options": [
           {"label": "Yes, correct", "description": "Use this as the answer"},
           {"label": "No, let me correct", "description": "I'll provide the right answer"}
         ],
         "multiSelect": false
       }]
     }
     ```
   - Prefix answer with `[from-code]` when sending to MCP
   - Increment the auto-confirm counter (see Dialectic Rhythm Guard below)

   **PATH 2 — Human Judgment** (decisions only humans can make):
   When the question asks about goals, vision, acceptance criteria, business logic,
   preferences, tradeoffs, scope, or desired behavior for NEW features:
   - Present question directly to user via AskUserQuestion with suggested options
   - Prefix answer with `[from-user]` when sending to MCP

   **PATH 3 — Code + Judgment** (facts exist but interpretation needed):
   When code contains relevant facts BUT the question also requires judgment
   (e.g., "I see a saga pattern in orders/. Should payments use the same?"):
   - Read relevant code first
   - Present BOTH the code findings AND the question to user
   - If any part of the question requires judgment, route the ENTIRE question to user
   - Prefix answer with `[from-user]` (human made the decision)

   **PATH 4 — Research Interlude** (external knowledge needed):
   When the question asks about third-party APIs, pricing models, library
   capabilities, version compatibility, security advisories, or industry
   standards that are NOT answerable from the local codebase:
   - Use WebFetch/WebSearch to gather external information
   - Present findings to user as a **confirmation question** via AskUserQuestion
     (same pattern as PATH 1, but with web sources instead of code):
     ```json
     {
       "questions": [{
         "question": "MCP asks: What rate limits does the Stripe API have?\n\nI found: Stripe allows 100 read ops/sec and 25 write ops/sec in live mode.\n\nIs this correct?",
         "header": "Q<N> — Research Confirmation",
         "options": [
           {"label": "Yes, correct", "description": "Use this as the answer"},
           {"label": "No, let me correct", "description": "I'll provide the right answer"}
         ],
         "multiSelect": false
       }]
     }
     ```
   - Prefix answer with `[from-research]` when sending to MCP
   - **Facts, not decisions**: "Stripe rate limit is 100 req/s" is research.
     "We should use Stripe" is a DECISION — route to PATH 2.

   **When in doubt, use PATH 2.** It's safer to ask the user than to guess.

3. **Send the answer back to MCP**:
   ```
   Tool: ouroboros_interview
   Arguments:
     session_id: <session ID>
     answer: "[from-code][auto-confirmed] Python 3.12, FastAPI (pyproject.toml)"
             or "[from-code] JWT-based auth in src/auth/jwt.py"
             or "[from-user] Stripe Billing"
             or "[from-research] Stripe: 100 read ops/sec live mode"
   ```
   MCP records the answer, generates the next question, and returns it.

6. **Keep a visible ambiguity ledger**:
   Track independent ambiguity tracks (scope, constraints, outputs, verification).
   Do NOT let the interview collapse onto a single subtopic.

7. **Repeat steps 2-6** until the user says "done" or MCP signals seed-ready.

8. **Seed-ready Acceptance Guard**:
   When MCP signals seed-ready, do NOT relay completion blindly. Before
   announcing completion or suggesting `ooo seed`, apply the canonical Seed
   Closer criteria from `src/ouroboros/agents/seed-closer.md` as the single
   source of truth for closure readiness. Run the check from the main session's
   perspective, including any code, research, or brownfield context MCP did not
   see.

   If any material decision remains unresolved, do not announce seed-ready.
   If the local challenge finds a material gap, explicitly override the MCP
   signal: `"MCP says seed-ready, but I am not accepting it yet because <gap>."`
   Explain the gap briefly and ask the single highest-impact follow-up question,
   routed through PATH 2 or PATH 3 as appropriate.

9. **Prefer stopping over over-interviewing**:
   When the Seed-ready Acceptance Guard passes, suggest `ooo seed`.

10. After completion, suggest the next step:
   `📍 Next: ooo seed to crystallize these requirements into a specification`

#### Dialectic Rhythm Guard

Track consecutive non-user answers (PATH 1a auto-confirms, PATH 1b code
confirmations, and PATH 4 research confirmations). If **3 consecutive questions**
were answered without direct user judgment (PATH 1a, 1b, or PATH 4), the next
question MUST be routed to **PATH 2** (directly to user), even if it appears
code- or research-answerable.

This preserves the Socratic dialectic rhythm — the interview is with the human,
not the codebase or external docs. Auto-confirmed answers especially need this
guard: if the AI answers too many questions on its own, the user loses awareness
of what the AI is assuming about their project.

Reset the counter whenever user answers directly (PATH 2 or PATH 3).

#### Retry on Failure

If MCP returns `is_error=true` with `meta.recoverable=true`:
1. Tell user: "Question generation encountered an issue. Retrying..."
2. Call `ouroboros_interview(session_id=...)` to resume (max 2 retries).
   State (including any recorded answers) is persisted before the error,
   so resuming will not lose progress.
3. If still failing: "MCP is having trouble. Switching to direct interview mode."
   Then switch to Path B and continue from where you left off.

**Advantages of MCP mode**: State persists to disk, ambiguity scoring, direct `ooo seed` integration via session ID. Code-enriched confirmation questions reduce user burden — only human-judgment questions require user input.

### Path B: Plugin Fallback (No MCP Server)

If the MCP tool is NOT available, fall back to agent-based interview:

1. Read `src/ouroboros/agents/socratic-interviewer.md` and adopt that role
2. **Pre-scan the codebase**: Use Glob to check for config files (`pyproject.toml`, `package.json`, `go.mod`, etc.). If found, use Read/Grep to scan key files and incorporate findings into your questions as confirmation-style ("I see X. Should I assume Y?") rather than open-ended discovery ("Do you have X?")
3. Ask clarifying questions based on the user's topic and codebase context
4. **Present each question using AskUserQuestion** with contextually relevant suggested answers (same format as Path A step 2)
5. Use Read, Glob, Grep, WebFetch to explore further context if needed
6. Maintain the same ambiguity ledger and breadth-check behavior as in Path A:
   - Track multiple independent ambiguity threads
   - Revisit unresolved threads every few rounds
   - Do not let one detailed subtopic crowd out the rest of the original request
7. Prefer closure only after applying the Seed-ready Acceptance Guard above. Ask whether to move to `ooo seed` rather than continuing to generate narrower questions.
8. Continue until the user says "done"
9. Interview results live in conversation context (not persisted)
10. After completion, suggest the next step in `📍 Next:` format:
   `📍 Next: ooo seed to crystallize these requirements into a specification`

## Interviewer Behavior

**MCP (question generator)** is ONLY a questioner:
- Always generates a question targeting the biggest source of ambiguity
- Preserves breadth across independent ambiguity tracks
- NEVER writes code, edits files, or runs commands

**You (main session)** are a Socratic facilitator:
- Read `src/ouroboros/agents/socratic-interviewer.md` to understand the interview methodology
- You CAN use Read/Glob/Grep to scan the codebase for answering MCP questions
- For high-confidence factual questions (PATH 1a), auto-confirm and notify the user
- For all other questions, present to user as confirmation or direct question
- You NEVER make decisions on behalf of the user — auto-confirm is for FACTS only
- You are the final gate on MCP seed-ready signals: apply the canonical Seed
  Closer criteria before suggesting `ooo seed`
- The Dialectic Rhythm Guard prevents over-automation: after 3 consecutive
  non-user answers, the next question MUST go directly to the user

## Example Session

```
User: ooo interview Add payment module to existing project

MCP Q1: "Is this a greenfield or brownfield project?"
→ PATH 1a: exact match in pyproject.toml + src/ directory
→ ℹ️ Auto-confirmed: Brownfield, Python 3.12 / FastAPI (pyproject.toml)
→ [from-code][auto-confirmed] sent to MCP (counter: 1)

MCP Q2: "What payment provider will you use?"
→ PATH 2: human decision — no code can answer this
→ User: "Stripe"
→ [from-user] sent to MCP (counter reset to 0)

MCP Q3: "What authentication method does the project use?"
→ PATH 1b: found src/auth/jwt.py but inferred (not manifest)
→ "I found JWT-based auth in src/auth/jwt.py. Is this correct?"
→ User: "Yes, correct"
→ [from-code] sent to MCP (counter: 1)

MCP Q4: "How should payment failures affect order state?"
→ PATH 2: design decision
→ User: "Saga pattern for rollback"
→ [from-user] sent to MCP (counter reset to 0)

MCP Q5: "What are the acceptance criteria for this feature?"
→ PATH 2: requires human judgment
→ User: "Successful Stripe charge, webhook handling, refund support"

📍 Next: `ooo seed` to crystallize these requirements into a specification
```

## Next Steps

After interview completion, use `ooo seed` to generate the Seed specification.
