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

When the user invokes this skill, choose the execution path:

### Path A: MCP Mode (Preferred)

If the `ouroboros_interview` MCP tool is available, use it for persistent, structured interviews:

1. **Start a new interview**:
   ```
   Tool: ouroboros_interview
   Arguments:
     initial_context: <user's topic or idea>
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
2. Ask clarifying questions based on the user's topic
3. **Present each question using AskUserQuestion** with contextually relevant suggested answers (same format as Path A step 2)
4. Use Read, Glob, Grep, WebFetch to explore context if needed
5. Continue until the user says "done"
6. Interview results live in conversation context (not persisted)

## Interviewer Behavior (Both Modes)

The interviewer is **ONLY a questioner**:
- Always ends responses with a question
- Targets the biggest source of ambiguity
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

User: ooo seed  [Generate seed from interview]
```

## Next Steps

After interview completion, use `ooo seed` to generate the Seed specification.
