---
name: qa
description: "General-purpose QA verdict for any artifact type"
---

# /ouroboros:qa

Standalone quality assessment for any artifact. Unlike `ooo evaluate` (3-stage pipeline), `ooo qa` is a fast single-pass verdict.

## Usage

```
ooo qa [file_path | artifact_text]
ooo qa                                     # evaluate recent execution output
/ouroboros:qa [file_path | artifact_text]   # plugin mode
```

**Trigger keywords:** "ooo qa", "qa check", "quality check"

## Instructions

When the user invokes this skill:

### Step 0: Determine execution mode

- **MCP mode** — If `ToolSearch` is available, try loading the QA MCP tool:
  ```
  ToolSearch query: "+ouroboros qa"
  ```
  If found, proceed with **QA Steps** below.

- **Fallback mode** — If no matching tool, skip to **Fallback** section.

### QA Steps (MCP mode)

**Architecture**: Main is thin proxy. `ouroboros_qa` is a stateless record/normalize tool, and the subagent does the actual scoring.

```
MCP (record verdict) ←→ @qa-judge (read artifact, score, record)
You (main) = pass artifact path + quality bar, present compact verdict
```

1. **Determine the artifact:**
   - If user provides a file path: Note the path (do NOT read it yourself)
   - If user provides inline text: Pass it to the agent
   - If no artifact specified: Ask user

2. **Determine the quality bar:**
   - If a seed YAML is available in context: Note it
   - If user specifies: Use that
   - If neither: ask the user before spawning the agent

3. **Delegate to @qa-judge**:
   ```
   Tool: Agent
   Arguments:
     prompt: |
       artifact_path: <path if file-based>
       artifact: <inline text if user pasted content directly>
       quality_bar: <bar>
       seed_content: <yaml if available>
       qa_session_id: <optional previous qa session id>
       iteration_history: <optional previous iteration array>
     subagent_type: "ouroboros:qa-judge"
     description: "QA verdict"
   ```

   The agent reads the artifact if needed, computes a JSON verdict, calls `ouroboros_qa` once to normalize/record it, and returns only a compact verdict summary.
   Do NOT read artifact files yourself.

4. **Present the verdict** returned by the agent:
   - Show score and verdict
   - End with next step:
     - **PASS**: `Next: Artifact meets quality bar. Proceed with confidence.`
     - **REVISE**: `Next: Address suggestions, then ooo qa again.`
     - **FAIL**: `Next: Consider ooo interview or ooo unstuck.`

**Context minimization** (CRITICAL):
- Do NOT read artifact files from main session.
- When native subagents are available, do NOT call MCP tools from main session.
- The agent handles ALL file reading and MCP interaction.
- Do NOT expect a separate QA `state` action — this stage is stateless by design.

### Iterative QA Loop

For iterative usage, pass `qa_session_id` and `iteration_history` from the previous agent/MCP result into the next agent prompt.

If native subagents are unavailable or fail twice, call `ouroboros_qa` directly without `agent_verdict` and let MCP run the original internal LLM-in-MCP scoring flow using the same minimal inputs.

## Fallback (No MCP Server)

Spawn the `ouroboros:qa-judge` agent with artifact path and quality bar. The agent definition contains the complete scoring framework.
