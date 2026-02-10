# /ouroboros:ecomode

Token-efficient execution using haiku and sonnet models only.

## Usage

```
ooo ecomode "<your request>"
/ouroboros:ecomode "<your request>"
```

**Trigger keywords:** "ecomode", "eco", "budget", "cheap mode", "token efficient"

## How It Works

Ecomode reduces costs by 85% through smart model routing:

1. **Route** (model router)
   - Use haiku for simple tasks (1x cost)
   - Use sonnet for standard tasks (10x cost)
   - Never use opus (30x cost)

2. **Execute** (executor with ecomode routing)
   - All agents use haiku/sonnet only
   - Progressive escalation only if needed
   - Fail-fast on complexity

3. **Report** savings
   - Show tokens used vs standard mode
   - Estimate cost savings

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be done

2. **Initialize state**: Create `.omc/state/ecomode-state.json`:
   ```json
   {
     "mode": "ecomode",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "running",
     "tokens_used": 0,
     "estimated_cost": 0,
     "standard_cost_estimate": 0,
     "savings_percent": 0
   }
   ```

3. **Execute with cost-optimized routing**:

   For all agent delegations, use:
   ```
   model="haiku" for:
   - Quick lookups
   - Lightweight scans
   - Narrow checks

   model="sonnet" for:
   - Standard implementation
   - Debugging
   - Code review

   NEVER use model="opus"
   ```

4. **Progressive escalation** (only if necessary):
   - Start with haiku
   - If haiku fails/truncates, retry with sonnet
   - If sonnet fails, report complexity limit reached
   - Do NOT escalate to opus

5. **Track costs**:
   - Count tokens per model tier
   - Estimate cost using tier pricing:
     - haiku: $0.80 per million input tokens
     - sonnet: $3.00 per million input tokens
   - Compare to "what if we used opus" baseline

6. **Report savings**:
   ```
   [Ecomode Summary]
   Tokens Used:
   - Haiku: <tokens> (1x)
   - Sonnet: <tokens> (10x)

   Estimated Cost: $<cost>
   Standard Mode Cost: $<standard>
   Savings: <percent>%

   Note: Opus was not used (30x cost avoided)
   ```

## Model Selection Guide

| Task Complexity | Model | Cost Factor |
|----------------|-------|-------------|
| Simple lookup | haiku | 1x |
| Small refactor | haiku | 1x |
| Add feature | sonnet | 10x |
| Debug issue | sonnet | 10x |
| Architecture | BLOCKED | Use standard mode |
| Complex refactor | BLOCKED | Use standard mode |

## When Ecomode Fails

If the task is too complex for haiku/sonnet:

```
Ecomode LIMIT REACHED
=====================

This task requires opus-tier reasoning.
Ecomode avoids 30x cost opus calls.

Options:
1. Simplify the request
2. Use standard mode: /ouroboros:autopilot
3. Accept partial completion with current result
```

## Example

```
User: ooo ecomode add error handling to API

[Ecomode Planning]
Using haiku for planning...

[Ecomode Execution]
Using sonnet for implementation...

[Ecomode Verification]
Using haiku for verification...

Ecomode COMPLETE
================
Request: Add error handling to API
Duration: 2m 10s

Model Usage:
- Haiku: 15,000 tokens (2 calls)
- Sonnet: 45,000 tokens (3 calls)

Cost Analysis:
- Ecomode cost: $0.15
- Standard mode cost: $0.95
- Savings: 84%

Note: $0.80 saved by avoiding opus
```

## Routing Algorithm

```python
def ecomode_route(task):
    complexity = estimate_complexity(task)

    if complexity < 0.4:
        return "haiku"
    elif complexity < 0.8:
        return "sonnet"
    else:
        # Too complex for ecomode
        raise EcomodeLimitReached(
            "Task requires opus-tier reasoning. "
            "Use standard mode or simplify."
        )
```

## State Persistence

Track:
- Tokens used per tier
- Cost estimates
- Savings calculations
- Tasks blocked by complexity

## Cancellation

Cancel with `/ouroboros:cancel`.

Shows cost summary up to cancellation point.
