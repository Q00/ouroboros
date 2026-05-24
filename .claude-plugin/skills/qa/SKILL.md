---
name: qa
description: "General-purpose QA verdict for any artifact type"
---

# /ouroboros:qa

Standalone quality assessment for any artifact — code, documents, API responses, test output, or custom content. Unlike `ooo evaluate` (3-stage formal verification pipeline), `ooo qa` is a fast single-pass verdict with actionable suggestions.

## Usage

```
ooo qa [file_path | artifact_text]
ooo qa                                     # evaluate recent execution output
/ouroboros:qa [file_path | artifact_text]   # plugin mode
```

**Trigger keywords:** "ooo qa", "qa check", "quality check"

## How It Works

The QA Judge evaluates an artifact against a quality bar and returns a structured verdict:

1. **Parse the Quality Bar** — What EXACTLY must be true to pass?
2. **Assess Dimensions** — Correctness, Completeness, Quality, Intent Alignment, Domain-Specific
3. **Render Verdict** — Score (0.0-1.0) with PASS / REVISE / FAIL
4. **Determine Loop Action** — `done` (pass), `continue` (revise), `escalate` (fail)

### Verdict Thresholds

| Score Range  | Verdict | Loop Action |
|--------------|---------|-------------|
| >= 0.80      | PASS    | done        |
| 0.40 - 0.79  | REVISE  | continue    |
| < 0.40       | FAIL    | escalate    |

## Instructions

When the user invokes this skill:

### Step 0: Determine execution mode

This skill works in two modes. Determine which one **before** attempting any tool calls:

- **MCP mode** — If `ToolSearch` is available, try loading the QA MCP tool:
  ```
  ToolSearch query: "+ouroboros qa"
  ```
  If found (typically named `mcp__plugin_ouroboros_ouroboros__ouroboros_qa`), proceed with **QA Steps** below.

- **Fallback mode** — If `ToolSearch` is not available, or it finds no matching tool, skip directly to the **Fallback** section. This skill is designed to work without MCP setup.

### QA Steps (MCP mode)

1. **Determine the artifact to evaluate:**
   - If user provides a file path: Read the file with Read tool
   - If user provides inline text: Use that directly
   - If no artifact specified: Look for the most recent execution output in conversation context
   - Ask user if unclear what to evaluate

2. **Determine the quality bar:**
   - If a seed YAML is available in context: Extract acceptance criteria from it
   - If user specifies a quality bar: Use that
   - If neither: Ask the user "What does 'good' mean for this artifact?"

3. **Determine artifact type:**
   - `code` — source code files
   - `test_output` — test results, CI output
   - `document` — specs, docs, READMEs
   - `api_response` — API responses, JSON payloads
   - `screenshot` — visual artifacts
   - `custom` — anything else

4. **Call the `ouroboros_qa` MCP tool:**
   ```
   Tool: ouroboros_qa
   Arguments:
     artifact: <the content to evaluate>
     quality_bar: <what 'pass' means>
     artifact_type: "code"  (or other type)
     reference: <optional reference for comparison>
     pass_threshold: 0.80  (adjustable)
     seed_content: <seed YAML if available>
   ```

5. **Present results clearly:**
   - Show the score and verdict prominently
   - List dimension scores
   - Highlight specific differences found
   - Show actionable suggestions
   - End with next step guidance based on verdict:
     - **PASS (done)**: `Next: Your artifact meets the quality bar. Proceed with confidence.`
     - **REVISE (continue)**: `Next: Address the suggestions above, then run ooo qa again to re-check.`
     - **FAIL (escalate)**: `Next: Fundamental issues detected. Consider ooo interview to re-examine requirements, or ooo unstuck to challenge assumptions.`

### Iterative QA Loop

For iterative usage, track the `qa_session_id` and `iteration_history` from the response meta:

1. First call returns `qa_session_id` and `iteration_entry` in meta
2. On subsequent calls, pass `qa_session_id` and accumulated `iteration_history`
3. Continue until verdict is `pass` or `fail`

In fallback mode, generate a `qa-<uuid4_short>` session ID on the first run and maintain iteration count in conversation context to preserve the same iterative contract.

## Fallback (No MCP Server)

If the MCP server is not available, adopt the `ouroboros:qa-judge` agent role directly:

1. Read the canonical agent definition: `<project-root>/src/ouroboros/agents/qa-judge.md`
   (This is the same prompt used by the MCP QA tool, ensuring consistent verdicts.)
2. Follow the QA Judge framework to evaluate the artifact
3. Output the verdict in the standard format (must match MCP output shape):

```
QA Verdict [Iteration N]
========================
Session: qa-<id>
Score: X.XX / 1.00 [PASS/REVISE/FAIL]
Verdict: pass/revise/fail
Threshold: 0.80

Dimensions:
  Correctness:      X.XX
  Completeness:     X.XX
  Quality:          X.XX
  Intent Alignment: X.XX
  Domain-Specific:  X.XX

Differences:
  - <specific difference>

Suggestions:
  - <actionable fix>

Reasoning: <1-3 sentence summary>

Loop Action: done/continue/escalate
```

## Example

```
User: ooo qa src/main.py

QA Verdict [Iteration 1]
============================================================
Session: qa-a1b2c3d4
Score: 0.72 / 1.00 [REVISE]
Verdict: revise
Threshold: 0.80

Dimensions:
  Correctness:           0.85
  Completeness:          0.60
  Quality:               0.75
  Intent Alignment:      0.80
  Domain-Specific:       0.60

Differences:
  - Missing error handling for network timeout in fetch_data()
  - No input validation on user_id parameter
  - Type hints missing on 3 public functions

Suggestions:
  - Add try/except with TimeoutError in fetch_data() (line 42)
  - Add isinstance check for user_id at function entry
  - Add return type annotations to get_user(), fetch_data(), process_result()

Reasoning: Core logic is correct but lacks defensive programming
patterns expected for production code.

Loop Action: continue

Next: Address the suggestions above, then run `ooo qa` again to re-check.
```
