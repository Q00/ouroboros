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

2. **For each question from MCP, apply 3-Path Routing:**

   **PATH 1 — Code Confirmation** (describe current state, user confirms):
   When the question asks about existing tech stack, frameworks, dependencies,
   current patterns, architecture, or file structure:
   - Use Read/Glob/Grep to find the factual answer
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
   - NEVER auto-send without user seeing and confirming
   - Prefix answer with `[from-code]` when sending to MCP
   - **Description, not prescription**: "The project uses JWT" is fact.
     "The new feature should also use JWT" is a DECISION — route to PATH 2.

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
     answer: "[from-code] JWT-based auth in src/auth/jwt.py" or "[from-user] Stripe Billing" or "[from-research] Stripe: 100 read ops/sec live mode"
   ```
   MCP records the answer, generates the next question, and returns it.

6. **Keep a visible ambiguity ledger**:
   Track independent ambiguity tracks (scope, constraints, outputs, verification).
   Do NOT let the interview collapse onto a single subtopic.

7. **Repeat steps 2-6** until the user says "done" or MCP signals seed-ready.

8. **Prefer stopping over over-interviewing**:
   When scope, outputs, AC, and non-goals are clear, suggest `ooo seed`.

9. After completion, suggest the next step:
   `📍 Next: ooo seed to crystallize these requirements into a specification`

#### Dialectic Rhythm Guard

Track consecutive PATH 1/PATH 4 (code/research confirmation) answers. If 3
consecutive questions were answered via PATH 1 or PATH 4, the next question MUST
be routed to PATH 2 (directly to user), even if it appears code- or
research-answerable. This preserves the Socratic dialectic rhythm — the interview
is with the human, not the codebase or external docs.
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
7. Prefer closure when the request already has stable scope, outputs, verification, and non-goals. Ask whether to move to `ooo seed` rather than continuing to generate narrower questions.
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
- You present every MCP question to the user (as confirmation or direct question)
- You NEVER skip a question or auto-send without user seeing it
- You NEVER make decisions on behalf of the user

## Example Session

```
User: ooo interview Add payment module to existing project

MCP Q1: "Is this a greenfield or brownfield project?"
→ [Scanning... pyproject.toml, src/ found]
→ Auto-answer: "Brownfield, Python/FastAPI project"

MCP Q2: "What payment provider will you use?"
→ This is a human decision.
→ User: "Stripe"

MCP Q3: "What authentication method does the project use?"
→ [Scanning... src/auth/jwt.py found]
→ Auto-answer: "JWT-based auth in src/auth/jwt.py"

MCP Q4: "How should payment failures affect order state?"
→ This is a design decision.
→ User: "Saga pattern for rollback"

MCP Q5: "What are the acceptance criteria for this feature?"
→ This requires human judgment.
→ User: "Successful Stripe charge, webhook handling, refund support"

📍 Next: `ooo seed` to crystallize these requirements into a specification
```

## Next Steps

After interview completion, use `ooo seed` to generate the Seed specification.
