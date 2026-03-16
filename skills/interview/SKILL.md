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

If the `ouroboros_interview` MCP tool is available (loaded via ToolSearch above), use it for persistent, structured interviews:

1. **Start a new interview**:
   ```
   Tool: ouroboros_interview
   Arguments:
     initial_context: <user's topic or idea>
     cwd: <current working directory>
   ```
   The tool auto-detects brownfield projects from `cwd` and scans the codebase
   before asking the first question. The first question will cite specific
   files/patterns found in the project. Returns a session ID and question.

2. **Present the question using AskUserQuestion**:
   After receiving a question from the tool, present it via `AskUserQuestion` with contextually relevant suggested answers:
   ```json
   {
     "questions": [{
       "question": "<question from MCP tool>",
       "header": "Q<N>",
       "options": [
         {"label": "<option 1>", "description": "<brief explanation>"},
         {"label": "<option 2>", "description": "<brief explanation>"}
       ],
       "multiSelect": false
     }]
   }
   ```

   **Generating options** — analyze the question and suggest 2-3 likely answers:
   - Binary questions (greenfield/brownfield, yes/no): use the natural choices
   - Technology choices: suggest common options for the context
   - Open-ended questions: suggest representative answer categories
   - The user can always type a custom response via "Other"

3. **Relay the answer back**:
   ```
   Tool: ouroboros_interview
   Arguments:
     session_id: <session ID from step 1>
     answer: <user's selected option or custom text>
   ```
   The tool records the answer, generates the next question, and returns it.

4. **Keep a visible ambiguity ledger while interviewing**:
   Before or during the first 1-2 questions, identify the independent ambiguity tracks in the user's request.
   Examples:
   - For a feature request: scope, constraints, outputs, verification
   - For a PR/review task: item-by-item validity, allowed code paths, non-goals, expected deliverables
   - For a migration: source of truth, compatibility constraints, rollout boundaries

   Maintain this ledger mentally and do NOT let the interview collapse onto a single deep subtopic unless you have already checked whether the other tracks are resolved.

5. **Run periodic breadth checks**:
   Every few rounds, or sooner if one thread has become very detailed, ask a breadth-check question that revisits unresolved tracks.
   Good examples:
   - "We seem aligned on the adapter refactor. Are the review adjudication output and path constraints also fixed now?"
   - "We have the implementation path. Do we still need to settle acceptance tests or output format?"

   Use breadth checks especially when:
   - The original request contains a list of review findings, bugs, subproblems, or deliverables
   - The user mentions both implementation work and a written output
   - The conversation starts refining one file or one abstraction for many consecutive rounds

6. **Repeat steps 2-5** until the user says "done" or requirements are clear.

7. **Prefer stopping over over-interviewing**:
   When the following are already explicit, do not keep drilling into narrower sub-questions:
   - In-scope vs out-of-scope boundaries
   - Required outputs or deliverables
   - Acceptance-test or verification expectations
   - Important non-goals / frozen public contracts
   - Enough detail to generate a Seed without inventing missing behavior

   At that point, ask a closure question or suggest moving to `ooo seed` instead of opening a new deep thread.

8. After completion, suggest the next step in `📍 Next:` format:
   `📍 Next: ooo seed to crystallize these requirements into a specification`

**Advantages of MCP mode**: State persists to disk (survives session restarts), ambiguity scoring, direct integration with `ooo seed` via session ID, structured input with AskUserQuestion.

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

## Interviewer Behavior (Both Modes)

The interviewer is **ONLY a questioner**:
- Always ends responses with a question
- Targets the biggest source of ambiguity
- Preserves breadth across independent ambiguity tracks instead of over-focusing on one thread
- Periodically checks whether the interview is already specific enough to stop
- NEVER writes code, edits files, or runs commands

## Example Session

```
User: ooo interview Build a REST API

Q1: What domain will this REST API serve?
User: It's for task management

Q2: What operations should tasks support?
User: Create, read, update, delete

Q3: Will tasks have relationships (e.g., subtasks, tags)?
User: Yes, tags for organizing

📍 Next: `ooo seed` to crystallize these requirements into a specification

User: ooo seed  [Generate seed from interview]
```

## Next Steps

After interview completion, use `ooo seed` to generate the Seed specification.
