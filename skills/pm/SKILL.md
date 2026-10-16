---
name: pm
description: "Use when the user wants product requirements rather than code: 'write a PRD', 'product requirements', 'spec this feature for the team', 'ooo pm'. Runs a PM-focused interview with automatic question classification and produces a PRD/PM document. Not for implementation requests."
---

# /ouroboros:pm

PM-focused Socratic interview that produces a Product Requirements Document (PRD).

## Required Skill Capabilities

- `ask_user`: ask the user through the active runtime's `ask_user` capability.
- `call_mcp`: `ouroboros_pm_interview` holds the interview state and writes the PRD.
- `orchestrate_subagents`: run the evidence lanes each question carries.
- `refine_answer`: confirm the structured answer before it is sent (Refine gate).
- `run_shell`: copy the finished PRD to the clipboard, when available.

## Instructions

Updates are checked in the background; if your context carries an Ouroboros update notice, mention `ooo update` once after the first question.

### Step 1: Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros pm_interview"` for `ouroboros_pm_interview`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

Fallback: fail closed. Do not inspect or edit `~/.claude/mcp.json` and do not add a direct Python fallback. Tell the user this, then stop:

```
The PM interview MCP tool is unavailable in this runtime.

Configure a supported CLI-backed host with:
  ouroboros setup --runtime <codex|opencode|kiro|copilot|hermes>

Then restart that host and retry ooo pm. Claude SDK profiles ([claude] and
[claude-sdk]) stay on MCP 1.x; the separate [mcp] server uses [claude-cli]. Do
not combine both MCP majors or add a direct Python fallback.
```

### Step 2: Start Interview

```
Tool: ouroboros_pm_interview
Arguments:
  initial_context: <user's topic or idea>
  cwd: <current working directory>
```

The response carries the first question, so Step 3 applies to it, evidence lanes included.

### Step 3: Loop

Apply this to every response that carries a question.

**Batched turns.** A response may carry one to three questions: `meta.question_batch` lists them, and `meta.question_advisories` holds one advisory envelope per question (its own `question_advisory_subagents`, `question_advisory_fanout_id`, and `question_advisory_result_correlation_key`). Treat each question as below, plus:
- Dispatch every envelope's payloads in one wave; no question shown loses its lanes.
- Submit results per envelope: one `ouroboros_submit_fanout_results` call each.
- Ask all of the turn's questions in one `ask_user` prompt, each with its own options from its own `meta.question_batch` entry.
- Relay the turn in one call, `answers: [{question, answer}, ...]`, one entry per question with its exact text. Collect every answer first and never auto-answer, auto-defer, or decide-later one on the user's behalf to complete the set. Whatever you leave out is abandoned, and a call without the turn's answers plans a new turn, so the user is asked again.
- Skip sentinels are per question, in that question's own entry.

**A. Alerts** (from `meta`):
- `meta.deferred_this_round` → print `[DEV → deferred] "question"`
- `meta.decide_later_this_round` → print `[DEV → decide-later] "question"`
- `meta.pending_reframe` → print `ℹ️ Reframed from technical question.`

**A2. Evidence lanes: required before you ask the user anything.**

You do not look at the repositories yourself, ever. This skill has no code-answer path: evidence that does not trace back to a lane is evidence the record does not contain. Everything you need is here and in the tool response.

When `meta.question_advisory_subagents` is present, process every payload: show the question first, then pass each payload's `prompt` unchanged, including compact stub prompts (the child fetches its own context through `ouroboros_fetch_artifact`). Obey `meta.question_advisory_host_action`: `spawn_subagents` means parallel support was declared; `dispatch_subagents_if_supported` means native parallel dispatch when available and sequential processing otherwise; `process_payloads_sequentially` requires ordered processing. The payloads are the work contract, while the host action selects the execution strategy.

The lanes are `code_context` and `data_context`, both required. After the question, behind a divider, say in the user's language how many perspectives are running, named in the user's terms rather than by lane id, and end at "grounds", never at "options" (this tool produces no answer choices):

```
---
While you answer this question, two perspectives are reviewing in parallel
(code context / data measurement). When they return I will put what they found
next to the question as grounds.
```

Do not go to step B while lanes are still in flight. A lane that came back empty, broke its contract, or could not be spawned has returned. The user may answer at any time; what waits is your formal prompt.

**Submitting.** Call `ouroboros_submit_fanout_results` with `meta.question_advisory_fanout_id`, an explicit `session_id`, and results correlated by `meta.question_advisory_result_correlation_key` (`context.lane_id`). Submit every lane you hold: a lane that found nothing submits its output; a lane you could not spawn, or whose child replied exactly `UNDISPATCHED`, is `{ "key": <lane id>, "undispatched": true }` (the literal `true`, no `content`). Never invent output for a lane you did not run.

**Reading the reply.** With a `contract_id`, synthesize from the outputs you hold. Without one, read `missing_required_keys` and `contract_violations` and resubmit once carrying every lane. Still without one, go to B with what survived, or with none, saying in one line that the investigation did not come back. In every case, leave out a lane named in `contract_violations`, and leave out a lane you submitted as
`undispatched`: a lane that did not run has an empty place, not a clear one. Where the block would carry that lane, write that it did not run.

**Evidence block** (`synthesis_contract.output_shape = "evidence_beside_question"`). Print it immediately above the question, then ask the question unchanged:

```
Evidence (examined: billing-api, storefront)

What the system does today
  · [billing-api] access continues to the end of the paid period
  · [storefront]  access is revoked immediately
    ! These two repositories implement this differently.

Measured: active subscriptions by plan, last 90 days
  · standard 12,480 / premium 3,120
```

The labels are placeholders for the shape, not text to copy. Rules:
- Render each code claim from the lane's `plain_statement` (already in the question's language), never from `policy_claim` and never paraphrased. No file paths, class names, or flag values on screen; the citations stay in the published fan-out.
- Each claim keeps its repository. Never merge two repositories' claims or present a disagreement as one policy with an exception; flag it, as above.
- Always print the examined scope. Each `examined` entry is a repository the lane read; an entry whose `policy_claims` is empty was read and had nothing (say so); a repository with no entry was never opened and is never printed as clean.
- Carry measurements as reported (`metric`, groups, numbers); do not re-scale, combine, or round.
- A no-op lane gets at most one line. `not_a_policy_question` / `not_a_measurement`: print nothing. `no_repository_in_roster` / `roster_repository_not_readable` / `store_described_but_not_callable`: yours to handle (nothing registered, a path did not open, a store did not answer); never relay any of them as "your system has no such policy/data".
- Drop a finding the user has already answered past; do not re-open a settled decision with it.
- Evidence from outside the roster (the brownfield repositories selected for this PM session) is rejected at submission. Do not relay it; offer to register the repository instead.
- No answer options, recommendation, ranking, or "therefore" sentence. A PRD asks what the system *should* do; the block says what it *does*.

**A3. Findings are evidence, never answers.** The published fan-out is a finding's whole record. Do not send `[from-code]` answers, do not ask the user to confirm a finding as its own turn, do not put findings in any answer payload, and never skip asking because a lane answered clearly. The user answers in their own words with the evidence in view.

**B. Ask the user** (once A2's lanes have returned). Print the MCP content text with the evidence block. Tell the user they need not invent speculative answers: a question that is unknown, stakeholder-dependent, too broad, or safer to decide later goes through the assumption, decide-later, or deferred mechanisms, not into confirmed requirements. Ask through `ask_user` with `meta.question` as the question text and 2-3 suggested answers, including `Not sure yet: record as an assumption or decide-later item` when appropriate. When `meta.skip_eligible == true`, add one skip option from that question's own `classification`:
- `decide_later` → `Decide later` (recorded as an open item in the PRD)
- `deferred` → `Defer to dev` (the technical decision is deferred to the development phase)

**C. Relay the answer.** `Decide later` → `answer="[decide_later]"`; `Defer to dev` → `answer="[deferred]"`; anything else goes through the Refine gate first. On a batched turn, refine each answer, then send them all in one `answers` call with each question's exact text; that call returns the next turn.

**Refine gate (pm).** Unlike the interview skill, always structure the answer, even when the user only picked an option: it is MCP's only context and what the PRD records as decided. Label sections in the user's own words `(user-stated)` and leave your reading unmarked. Omit any section you have nothing for rather than filling it with something plausible. No codebase-context section and no lane findings (A3).

```
[from-user][refined]
Decision: <what they decided, in their words>

Reasoning:
- <your reading of why, drawn from what they said in this session>

Constraints (user-stated):
- <constraints they stated>

Out of scope (user-stated):
- <what they put out of scope>
```

Then confirm through `ask_user`: "I structured your answer as follows before sending it: <payload>. Is anything missing or misrepresented?" Options: `Send as-is`, `Fix the reasoning` (the unmarked section is the one you wrote), `Add to Constraints`, `Let me rewrite it`. Append `[refined]` only after this confirmation.

```
Tool: ouroboros_pm_interview
Arguments:
  session_id: <meta.session_id>
  last_question: <meta.question>
  answer: <the refined answer, or "[decide_later]" / "[deferred]">
```

`last_question` is always required with `answer`: the server pairs every answer with its question and refuses an `answer` without `last_question`. Every answer is the user's own words.

**D. Check completion.** Completion is determined ONLY by `meta.is_complete`, never by the response text. If `meta.is_complete == true`:
- `meta.generation_failed == true` → retry generation:
  ```
  Tool: ouroboros_pm_interview
  Arguments:
    session_id: <session_id>
    action: "generate"
    cwd: <current working directory>
  ```
- Otherwise the PRD was generated: go to Step 4 (`meta.pm_path`, `meta.seed_path`).

Otherwise repeat Step 3.

### Step 4: Copy to Clipboard

If `run_shell` is available, copy the contents of `meta.pm_path` to the clipboard (for example `pbcopy < <meta.pm_path>` on macOS).

### Step 5: Show Result & Next Step

```
PM document saved: <meta.pm_path>
(copied to clipboard)

PM seed handoff artifact: <meta.pm_seed_path or meta.seed_path>
This is not the runnable Seed yet.

Next step:
  ooo interview <meta.pm_seed_path or meta.seed_path>
  ooo seed
```

Omit `(copied to clipboard)` when nothing was copied.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
