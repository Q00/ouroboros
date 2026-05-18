# RFC: Interview Milestone Lateral Contract

## Status

**Proposed.** This records the contract proposed in #817 for triggering
bounded `ooo lateral` passes from the interview flow. It is intentionally a
contract-only slice: no MCP handler, skill prompt, or runtime implementation is
changed here.

## Context

Issue #817 originally framed the problem as an interview stagnation hook: detect
when the interview has stopped making progress, then chain `ooo lateral` to help
recover. The open design question was the trigger. A traditional stagnation
heuristic would require tuning several values, for example ambiguity-score delta,
number of turns, repeated-question distance, or repeated "I don't know" answers.

The interview system already has a better boundary signal. `AmbiguityMilestone`
in `src/ouroboros/bigbang/ambiguity.py` divides the ambiguity score into four
semantic phases:

| Milestone | Score range | Meaning |
|---|---:|---|
| `INITIAL` | `1.0` → `> 0.4` | Core requirements are still being discovered. |
| `PROGRESS` | `<= 0.4` → `> 0.3` | Most requirements are captured; details remain. |
| `REFINED` | `<= 0.3` → `> 0.2` | Success criteria and edge cases are being sharpened. |
| `READY` | `<= 0.2` | Criteria are concrete enough for Seed generation. |

Those phase boundaries are already part of the interview model and are surfaced
as structured milestone metadata. Crossing one is a natural time to ask: before
we leave this phase, what hidden assumptions, contradictions, or missing edge
cases did we miss?

## Decision

Trigger bounded lateral review on **first forward milestone transitions**, not on
a free-form stagnation heuristic.

A transition is eligible when all of the following are true:

1. The current interview score maps to a later `AmbiguityMilestone` than the
   previous recorded score.
2. The transition is forward-only in this order:
   `INITIAL -> PROGRESS -> REFINED -> READY`.
3. The destination milestone has not already triggered a lateral pass for this
   interview session.
4. The interview is still being routed by the main session. The MCP
   `ouroboros_interview` tool remains a single-question generator.

Backward movement is not eligible. If the score regresses from `REFINED` to
`PROGRESS`, no new pass is triggered. If it later returns to `REFINED`, the
`REFINED` pass is not repeated if it already fired once.

## Runtime boundary

The lateral fan-out belongs at the skill/main-session layer, not inside the
interview MCP tool.

Current role split stays intact:

```text
MCP interview tool: question generation, state, ambiguity scoring
Main session: routing, code inspection, user questions, optional lateral calls
User: human judgment only
```

That boundary matters because `ouroboros_interview` should remain deterministic
and single-purpose. It produces the next question and metadata. The main session
can then decide whether a milestone transition warrants a sibling `ooo lateral`
call before it sends the next answer back into the interview.

## Lateral prompt shape

When a milestone transition fires, the main session should call `ooo lateral`
with a prompt grounded in the closing phase, for example:

```text
Closing INITIAL and entering PROGRESS for interview <session_id>.

Review the interview transcript and ambiguity snapshot. What hidden assumptions,
contradictions, missing constraints, or edge cases should be considered before
the next interview question moves deeper into PROGRESS?

Return concise findings that can be folded into the next ouroboros_interview
answer/context. Do not generate the next interview question directly.
```

Recommended default personas for automatic milestone passes are intentionally
small: `contrarian` plus one grounding persona such as `researcher` or
`simplifier`. A full persona panel remains available for explicit `ooo lateral`
invocations, but automatic milestone passes should keep latency bounded.

## Data needed by the main session

An implementation can be minimal. The main session needs only:

- previous milestone for the interview session;
- current milestone from the latest `ouroboros_interview` result metadata;
- set of destination milestones that already triggered lateral review;
- compact transcript or ambiguity snapshot to ground the lateral prompt;
- the lateral verdict text to fold into the next interview answer/context.

This RFC does not require a new persistent schema. A later implementation may
persist these fields in session metadata if needed, but the first implementation
can keep them in the main-session interview ledger.

## Acceptance criteria for the future implementation

A follow-up implementation should prove:

1. `INITIAL -> PROGRESS`, `PROGRESS -> REFINED`, and `REFINED -> READY` can each
   trigger at most one lateral pass per interview.
2. Backward or repeated transitions do not trigger duplicate passes.
3. The `ouroboros_interview` MCP contract remains a single-question generator;
   it does not call lateral personas itself.
4. The main session folds the lateral verdict into the next interview context or
   answer without showing raw persona chatter directly to the user.
5. Existing interview flows still work when no milestone transition occurs.

## Non-goals

- No score-delta, Levenshtein, embedding, or repeated-answer stagnation heuristic
  is introduced by this contract.
- No hard block or pause is added at milestone boundaries.
- No change is made to Seed readiness thresholds.
- No change is made to the existing resilience stagnation detector, which remains
  scoped to execution-loop style stagnation patterns.
- No plugin, MCP transport, or background job behavior is changed by this RFC.

## Why this is safer than a stagnation heuristic

The milestone trigger is deterministic, bounded, and already aligned with the
interview's semantic model. It avoids a new calibration surface while still
adding review exactly where the interview is about to move from one kind of
questioning to the next. That makes it a small contract the project can review
before deciding whether to implement the hook in code.

Refs #817.
