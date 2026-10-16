---
name: idk
description: "Calibrate interview explanations from the user's topic-specific understanding"
aliases: [dont-know, calibrate]
mcp_tool: ouroboros_interview
mcp_args:
  calibration_input: "$1"
---

# /ouroboros:idk

Calibrate the language used by `ooo interview` without weakening the interview itself.

## Usage

```text
ooo idk <terms I do not know, or what I understand about them>
/ouroboros:idk <terms I do not know, or what I understand about them>
```

Examples:

```text
ooo idk I do not know idempotency or event sourcing. I have built REST APIs.
ooo idk Kubernetes: deployed a tutorial once; networking and operators are unfamiliar.
ooo idk OAuth is familiar enough to implement, but I cannot explain PKCE.
```

## Instructions

When the user invokes this skill, treat their text as evidence for a
**topic-specific interview calibration**, not as a test and not as a permanent
rating of the person.

### 1. Extract evidence without quizzing by default

Identify:

- terms or concepts the user explicitly does not know;
- concepts they recognize but cannot explain or apply;
- concepts they can explain, use, debug, or compare;
- concrete experience they report, such as tutorials, production use, or design work.

Use only what the user actually said. Do not infer low general ability from one
unknown term, unfamiliar English vocabulary, a typo, or a short answer. Ask one
short follow-up only when the supplied text is too ambiguous to affect interview
wording safely. Otherwise continue immediately.

### 2. Estimate a local level and confidence

Assign one overall interview explanation level based on the user's evidence:

- **Foundational** — use plain language, define necessary terms before using
  them, and give one concrete example.
- **Working** — use standard terminology with a short definition when a term is
  new or overloaded; connect questions to practical trade-offs.
- **Fluent** — use precise domain terminology and concise trade-off language;
  skip definitions unless requested.

When the user mentions multiple domains with mixed familiarity, select the level
that ensures the least-familiar domain remains accessible (conservative default:
choose the lower level). The session carries one active level and applies it to
all subsequent interview questions.

Also record confidence as `low`, `medium`, or `high` based on how direct and
specific the evidence is. Prefer the less aggressive inference when evidence is
mixed.

### 3. Produce the active calibration

Respond with this compact, visible block so the current conversation can use it:

```text
Interview calibration
- <domain/concept>: <Foundational|Working|Fluent> (confidence: <level>)
  Evidence: <brief user-stated evidence>
- Unknown terms to define before use: <terms, or none>
- Adaptation: <how wording/examples will change>
```

Then say that the calibration applies to subsequent `ooo interview` turns in
this conversation and can be revised at any time with another `ooo idk`.
Do not write a profile to disk or claim that it persists across conversations.
If the runtime returns `meta.interview_calibration`, relay that value into the
next `ooo interview` call as the `interview_calibration` argument.

### 4. Apply it during an active interview

If `ooo idk` is invoked while an interview question is pending:

1. Do not treat the calibration text as the answer to the pending MCP question.
2. Update the active calibration and explain or rephrase the pending question at
   the calibrated level.
3. Preserve the question's decision, constraints, and acceptance meaning.
4. Ask the rephrased question again; advance the interview only after the user
   answers it.
5. On subsequent interview calls, pass the latest `meta.interview_calibration`
   value as the `interview_calibration` argument.

## Guardrails

- Adjust vocabulary, sentence structure, context, and example depth — never the
  rigor of requirement discovery, ambiguity checks, or closure gates.
- Define jargon before using it at Foundational level. At Working level, add
  brief parenthetical definitions only where useful. At Fluent level, stay
  concise and technical.
- Examples explain a question; they must not steer the user toward a particular
  product or architecture decision.
- Treat the estimate as provisional. Update it when later answers show stronger
  or weaker topic knowledge, and mention material changes briefly.
- If the user says a term is unknown during `ooo interview`, explain it and
  rephrase before asking for a decision. Do not count “I don't know that term”
  as an answer to the underlying interview question.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status`
when that MCP projection is available; otherwise derive it from this skill's
actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an
evolutionary loop. When the next action is genuinely a choice, list 2-3 honest
options in the `next:` clause. The breadcrumb line must be the last line of the
response.
