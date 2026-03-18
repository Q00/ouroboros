---
name: pm
description: "Generate a PM through guided PM-focused interview with automatic question classification"
---

# /ouroboros:pm

Generate a Product Requirements Document through a PM-focused Socratic interview.

## Usage

```
ooo pm [topic]
/ouroboros:pm [topic]
```

**Trigger keywords:** "pm", "product requirements", "write pm"

## Instructions

When the user invokes this skill:

### Step 1: Load MCP Tools (Required)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step first.**

1. Use the `ToolSearch` tool to find and load the PM interview MCP tool:
   ```
   ToolSearch query: "+ouroboros pm_interview"
   ```
   This searches for tools with "ouroboros" in the name related to "pm_interview".

2. The tool will typically be named `mcp__plugin_ouroboros_ouroboros__ouroboros_pm_interview` (with a plugin prefix). After ToolSearch returns, the tool becomes callable.

3. If ToolSearch finds the tool → proceed to **Path A**.
   If ToolSearch returns no matching tools → proceed to **Path B**.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Path A: MCP Mode (Required)

Use the `ouroboros_pm_interview` MCP tool for the entire interview. All business logic (question classification, reframing, decide-later deferrals, ambiguity scoring) is handled by the tool.

#### Starting a New Interview

1. **Start the interview**:
   ```
   Tool: ouroboros_pm_interview
   Arguments:
     initial_context: <user's topic or idea>
     cwd: <current working directory>
   ```

   **2-step start (brownfield repo selection):** If brownfield repos exist in the DB, the tool returns a repo selection prompt instead of starting the interview immediately. Present the returned `meta.options` to the user via `AskUserQuestion` (multiSelect). Then relay the selection back:
   ```
   Tool: ouroboros_pm_interview
   Arguments:
     session_id: <session ID from step 1>
     selected_repos: [<selected repo paths>]
   ```
   The tool then starts the interview with the selected repos as brownfield context (all assigned `role: main`).

   **Auto-greenfield:** If no repos exist in the DB, the tool skips repo selection and starts the interview immediately (greenfield mode).

   **1-step shortcut:** If `selected_repos` is provided alongside `initial_context`, the tool starts immediately with those repos (no selection prompt).

   `cwd` is used only for PM document output path, NOT for brownfield detection.
   Returns a `session_id`, the first question, and metadata including any `pending_reframe` or newly deferred items.

#### Interview Loop

2. **Display alerts BEFORE the question** — check the MCP response `meta` and display any alerts before presenting the next question:

   For each item in `meta.deferred_this_round` (technical questions auto-deferred to the dev interview):
   ```
   [DEV → deferred] "<question text>"
   ```

   For each item in `meta.decide_later_this_round` (questions marked for later decision):
   ```
   [DEV → decide-later] "<question text>"
   ```

   If `meta.pending_reframe` is present (non-null), display:
   ```
   ℹ️ This question was reframed from a technical question for PM clarity.
   ```

   Display all applicable alerts, then proceed to present the question.

3. **Present the question — render UI dynamically based on `meta.input_type`**:

   Every MCP response includes `meta.input_type` which tells you how to present the content to the user. Render accordingly:

   | `meta.input_type` | UI Rendering |
   |---|---|
   | `"freeText"` | Check `meta.status`: if it starts with `"awaiting_repo_"` or is `"awaiting_project_type"`, this is a **selection step** — show the MCP content text verbatim (it contains the numbered list) and use `AskUserQuestion` with a simple prompt asking the user to type numbers/names. Do NOT generate your own options — just let the user type freely (use "Type something" as the only option). For **interview questions** (status = `"interview_started"`), generate 2-3 suggested options as usual. |
   | `"singleSelect"` | `AskUserQuestion` with `multiSelect: false`, using `meta.options` as-is. |

   Example for `freeText` — selection step (repo add/remove):
   ```json
   {
     "questions": [{
       "question": "<content text from MCP — contains numbered list>",
       "header": "Repo selection",
       "options": [
         {"label": "Type numbers", "description": "e.g. 1,3,5 or repo-name, or 'none' to skip"}
       ],
       "multiSelect": false
     }]
   }
   ```
   The user types their answer (numbers, names, or "none") in the custom text input.

   Example for `freeText` — interview question:
   ```json
   {
     "questions": [{
       "question": "<meta.question — use verbatim>",
       "header": "Q<N>",
       "options": [
         {"label": "<option 1>", "description": "<brief explanation>"},
         {"label": "<option 2>", "description": "<brief explanation>"}
       ],
       "multiSelect": false
     }]
   }
   ```

4. **Relay the answer — use `meta.response_param` as the parameter name**:

   Every MCP response includes `meta.response_param` which tells you which parameter to pass the user's answer back as. Do NOT hardcode parameter names.

   ```
   Tool: ouroboros_pm_interview
   Arguments:
     session_id: <session ID from meta.session_id>
     <meta.response_param>: <user's response>
   ```

   Examples:
   - When `meta.response_param` is `"answer"` → `{ session_id: "...", answer: "user's text" }`
   - When `meta.response_param` is `"selected_repos"` → `{ session_id: "...", selected_repos: ["repo1", "repo2"] }`

   The tool records the response, performs any internal classification (pass-through, reframe, or decide-later for interview answers), and returns the next step along with updated metadata including the next `input_type` and `response_param`.

5. **Repeat steps 2-4** until the MCP tool response contains `meta.is_complete == true`.

   **Auto-transition to generate**: When the MCP tool returns `meta.is_complete == true`, do NOT ask the user another question. Instead, immediately proceed to **Step 6** (Generate the PM) by calling the tool with `action: "generate"` and the same `session_id`. This transition is automatic — no user confirmation needed.

   The tool determines completion via ambiguity scoring — there is no user "done" signal needed.

#### Resuming an Existing Interview

If the user has a previous session:
```
Tool: ouroboros_pm_interview
Arguments:
  session_id: <existing session ID>
```
The tool restores full state (Q&A history, deferred items, decide-later items, brownfield context) and returns the next question.

#### Generating the PM (auto-triggered on completion)

6. When `meta.is_complete` is `true`, **immediately generate the PM artifacts** (no user prompt needed):
   ```
   Tool: ouroboros_pm_interview
   Arguments:
     session_id: <session ID>
     action: "generate"
   ```
   The tool generates:
   - **PM Document**: `.ouroboros/pm.md` — natural language PM with sections for Goal, Target Users, User Stories, Success Criteria, Constraints, and Deferred Decisions
   - **PM Seed**: `~/.ouroboros/seeds/pm_seed_{id}.yaml` — structured YAML for downstream tooling

   The generate action is **idempotent** — calling it again with the same session_id produces the same result.

7. **Display the completion summary**:
   ```
   Your PM has been generated!

   Artifacts:
   - PM Document: .ouroboros/pm.md
   - PM Seed: ~/.ouroboros/seeds/pm_seed_{id}.yaml

   Deferred decisions: {N} items (to be resolved in development interview)

   📍 Next: `ooo interview` to start the development interview based on this PM
   ```

### Path B: MCP Server Not Available (Setup Required)

If the MCP tool is not found, the PM interview requires the Ouroboros MCP server. Guide the user to set it up:

```
The PM interview requires the Ouroboros MCP server, which is not currently available.

To set up:

1. Install the MCP server:
   uv tool install ouroboros-ai
   # or: pipx install ouroboros-ai

2. Add the MCP server to your Claude Code configuration:
   claude mcp add ouroboros -- ouroboros-ai serve

3. Restart Claude Code to load the MCP server.

4. Run `ooo pm` again.
```

Do NOT attempt to run the interview without the MCP tool. The question classification, reframing, and decide-later logic requires the server.

## Interviewer Behavior

The MCP tool's interviewer in PM mode:
- Focuses on BUSINESS and PRODUCT questions, not technical ones
- Targets PM-level ambiguity (goals, users, success criteria, scope)
- Automatically defers technical questions without bothering the PM
- Reframes necessary technical questions into PM-friendly language
- Always returns a question until ambiguity is resolved
- NEVER writes code, edits files, or runs commands

## Example Session

```
User: ooo pm

[MCP tool auto-detects brownfield: Python/FastAPI backend]

Q1: What do you want to build? Describe the product or feature in a few sentences.
> A notification system for our mobile app

Q2: Who are the primary users of this notification system?
> End users of our fitness tracking app

Q3: What events should trigger notifications?
> Workout reminders, achievement unlocks, friend activity

[DEV → deferred] "Should notifications use push (APNs/FCM), in-app, or email delivery?"
[DEV → decide-later] "What message queue system for async notification processing?"

Q4: How time-sensitive are these notifications?
> Workout reminders must be on time, others can be delayed up to an hour

ℹ️ This question was reframed from a technical question for PM clarity.
Q5 (reframed): When a user has many unread notifications, should we group them or show each one separately?
> Group similar ones, like "3 friends completed workouts"

...

[MCP tool returns meta.is_complete = true, completion_reason = "ambiguity_resolved"]
[Auto-transitioning to generate — no user prompt]

Your PM has been generated!

Artifacts:
- PM Document: .ouroboros/pm.md
- PM Seed: ~/.ouroboros/seeds/pm_seed_abc123.yaml

Deferred decisions: 3 items (to be resolved in development interview)

📍 Next: `ooo interview` to start the development interview based on this PM
```

## Next Steps

After PM completion, `ooo interview` will auto-detect the PM seed and offer to use it as initial context for a development-focused interview.
