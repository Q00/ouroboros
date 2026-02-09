# /ouroboros:evaluate

Evaluate an execution session using the three-stage verification pipeline.

## Usage

```
/ouroboros:evaluate <session_id> [artifact]
```

**Trigger keywords:** "evaluate this", "3-stage check"

## How It Works

The evaluation pipeline runs three progressive stages:

1. **Stage 1: Mechanical Verification** ($0 cost)
   - Lint checks, build validation, test execution
   - Static analysis, coverage measurement
   - Fails fast if mechanical checks don't pass

2. **Stage 2: Semantic Evaluation** (Standard tier)
   - AC compliance assessment
   - Goal alignment scoring
   - Drift measurement
   - Reasoning explanation

3. **Stage 3: Multi-Model Consensus** (Frontier tier, optional)
   - Multiple models vote on approval
   - Only triggered by uncertainty or manual request
   - Majority ratio determines outcome

## Instructions

When the user invokes this skill:

1. Determine what to evaluate:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for recent execution session IDs

2. Gather the artifact to evaluate:
   - If user specifies a file: Read it with Read tool
   - If recent execution output exists in conversation: Use that
   - Ask user if unclear what to evaluate

3. Call the `ouroboros_evaluate` MCP tool:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     session_id: <session ID>
     artifact: <the code/output to evaluate>
     seed_content: <original seed YAML, if available>
     acceptance_criterion: <specific AC to check, optional>
     artifact_type: "code"  (or "docs", "config")
     trigger_consensus: false  (true if user requests Stage 3)
   ```

4. Present results clearly:
   - Show each stage's pass/fail status
   - Highlight the final approval decision
   - If rejected, explain the failure reason
   - Suggest fixes if evaluation fails

## Fallback (No MCP Server)

If the MCP server is not available, use the `ouroboros:evaluator` agent to perform a prompt-based evaluation:

1. Delegate to `ouroboros:evaluator` agent
2. The agent performs qualitative evaluation based on the seed spec
3. Results are advisory (no numerical scoring without Python core)

## Example

```
User: /ouroboros:evaluate sess-abc-123

Evaluation Results
============================================================
Final Approval: APPROVED
Highest Stage Completed: 2

Stage 1: Mechanical Verification
  [PASS] lint: No issues found
  [PASS] build: Build successful
  [PASS] test: 12/12 tests passing

Stage 2: Semantic Evaluation
  Score: 0.85
  AC Compliance: YES
  Goal Alignment: 0.90
  Drift Score: 0.08
```
