---
name: interview
description: "Socratic interview to crystallize vague requirements"
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

### Step 0: Pre-Interview Codebase Scan (Both Modes)

**Before asking any questions**, scan the current project to understand what already exists:

1. **Project structure**: Run `Glob` on common patterns to detect the tech stack:
   - `**/package.json`, `**/pyproject.toml`, `**/go.mod`, `**/Cargo.toml`, `**/*.csproj`
   - `**/tsconfig.json`, `**/docker-compose.yml`, `**/Dockerfile`

2. **Key files**: Read up to 3 config files found above (first 50 lines each)

3. **Architecture patterns**: Run `Glob` to identify project layout:
   - `src/**`, `app/**`, `cmd/**`, `internal/**`, `lib/**`
   - `tests/**`, `test/**`, `__tests__/**`
   - `api/**`, `routes/**`, `controllers/**`, `models/**`

4. **Build a context summary** (keep under 500 words):
   ```
   Project: <name from config>
   Tech Stack: <detected languages/frameworks>
   Key Directories: <src layout>
   Existing Patterns: <auth, DB, API, testing, etc.>
   Dependencies: <top 10 notable deps>
   ```

5. **Include this summary** when starting the interview (embed in `initial_context` for MCP mode, or use as internal context for Plugin mode)

**Why**: This transforms open questions ("Do you have auth?") into confirmation questions ("I see JWT auth in `src/auth/`. Should I rely on that?"). The user shouldn't answer questions the codebase already answers.

Then choose the execution path:

### Path A: MCP Mode (Preferred)

If the `ouroboros_interview` MCP tool is available, use it for persistent, structured interviews:

1. **Start a new interview**:
   ```
   Tool: ouroboros_interview
   Arguments:
     initial_context: "<user's topic>\n\n## Codebase Context\n<summary from Step 0>"
   ```
   The tool returns a session ID and the first question.

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

4. **Repeat steps 2-3** until the user says "done" or requirements are clear.

5. After completion, suggest `ooo seed` to generate the Seed specification.

**Advantages of MCP mode**: State persists to disk (survives session restarts), ambiguity scoring, direct integration with `ooo seed` via session ID, structured input with AskUserQuestion.

### Path B: Plugin Fallback (No MCP Server)

If the MCP tool is NOT available, fall back to agent-based interview:

1. Read `agents/socratic-interviewer.md` and adopt that role
2. **Use the codebase context from Step 0** to inform all questions
3. Ask clarifying questions based on the user's topic — prefer confirmation questions over open questions when codebase evidence exists
4. **Present each question using AskUserQuestion** with contextually relevant suggested answers (same format as Path A step 2)
5. Use Read, Glob, Grep, WebFetch to explore further context as needed
6. Continue until the user says "done"
7. Interview results live in conversation context (not persisted)

## Interviewer Behavior (Both Modes)

The interviewer is **ONLY a questioner**:
- Always ends responses with a question
- Targets the biggest source of ambiguity
- NEVER writes code, edits files, or runs commands

## Example Session

```
User: ooo interview Build a REST API

[Scanning project... Found: Python 3.12, FastAPI, SQLAlchemy, pytest, src/ layout]

Q1: I see you're using FastAPI with SQLAlchemy in `src/`.
    Should this new REST API extend the existing app, or be a separate service?
User: Extend the existing app

Q2: Your models in `src/models/` use SQLAlchemy ORM with Alembic migrations.
    What new entities does this API need beyond what's already defined?
User: Tasks and tags — tasks have a title, status, and tags

Q3: I see pytest in your dev dependencies and existing tests in `tests/api/`.
    Should the new endpoints follow the same test pattern?
User: Yes, same pattern

User: ooo seed  [Generate seed from interview]
```

## Next Steps

After interview completion, use `ooo seed` to generate the Seed specification.
