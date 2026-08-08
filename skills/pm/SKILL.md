---
name: pm
description: "Generate a PM through guided PM-focused interview with automatic question classification. Use when the user says 'ooo pm', 'prd', 'product requirements', or wants to create a PRD/PM document."
---

# /ouroboros:pm

PM-focused Socratic interview that produces a Product Requirements Document.

## Instructions

### Step 0: Version Check (runs before the PM interview)

Before starting the PM interview, check if a newer version is available:

```bash
# Fetch latest release tag from GitHub (timeout 3s to avoid blocking)
curl -s --max-time 3 https://api.github.com/repos/Q00/ouroboros/releases/latest | grep -o '"tag_name": "[^"]*"' | head -1
```

Compare the result with the current version in the active runtime's local plugin metadata (for Claude installs this is `.claude-plugin/plugin.json`).
- If a newer version exists, ask the user through the active runtime's `ask_user` capability:
  ```json
  {
    "questions": [{
      "question": "Ouroboros <latest> is available (current: <local>). Update before starting?",
      "header": "Update",
      "options": [
        {"label": "Update now", "description": "Update plugin to latest version (restart required to apply)"},
        {"label": "Skip, start PM interview", "description": "Continue with current version"}
      ],
      "multiSelect": false
    }]
  }
  ```
  - If "Update now":
    - On Claude-plugin installs only:
      1. Run `claude plugin marketplace update ouroboros` via the active runtime's `run_shell` capability (refresh marketplace index). If this fails, tell the user "⚠️ Marketplace refresh failed, continuing…" and proceed.
      2. Run `claude plugin update ouroboros@ouroboros` via the active runtime's `run_shell` capability (update plugin/skills). If this fails, inform the user and stop — do NOT proceed to the package-manager step.
    - On non-Claude runtimes, skip Claude plugin commands and proceed directly to the package-manager step for `ouroboros-ai`; do not require Claude-only commands or tools.
    3. Detect the user's Python package manager and upgrade the MCP server:
       - Check which tool installed `ouroboros-ai` by running these in order:
         - `uv tool list 2>/dev/null | grep "^ouroboros-ai "` → if found, use `uv tool upgrade ouroboros-ai`
         - `pipx list 2>/dev/null | grep "^  ouroboros-ai "` → if found, use `pipx upgrade ouroboros-ai`
         - Otherwise, print: "Also upgrade the MCP server: `pip install --upgrade ouroboros-ai`" (do NOT run pip automatically)
    4. Tell the user: "Updated! Restart your session to apply, then run `ooo pm` again."
  - If "Skip": proceed immediately.
- If versions match, the check fails (network error, timeout, rate limit 403/429), or parsing fails/returns empty: **silently skip** and proceed.

### Step 1: Load MCP Tool

```
tool discovery query: "+ouroboros pm_interview"
```

**CRITICAL — deferred-schema guard (prevents "Invalid tool parameters"):**
This is a multi-turn loop and each turn runs in a fresh tool context. A deferred
tool's schema loaded on one turn is NOT guaranteed to still be loaded on the next.
Calling `ouroboros_pm_interview` while its schema is unloaded in the **current**
turn makes the runtime reject it with **"Invalid tool parameters"** every message.
Therefore **re-run `tool discovery query: "+ouroboros pm_interview"` immediately before
EVERY `ouroboros_pm_interview` call** below (idempotent — a no-op if already
loaded). If the load ever returns no matching tool (and the tool is not already callable — an empty load for an already-exposed tool is an expected no-op, not absence), follow the not-found diagnosis
below instead of retrying the failing call.

If not found → fail closed without inspecting or mutating
`~/.claude/mcp.json`. Standalone Claude SDK setup requires MCP 1.x and cannot
activate the Ouroboros MCP 2 server with its configured backend. Explain:

```
The PM interview MCP tool is unavailable in this runtime.

Configure a supported CLI-backed host with:
  ouroboros setup --runtime <codex|opencode|kiro|copilot|hermes>

Then restart that host and retry ooo pm. Do not combine the [claude] and [mcp]
extras or add a direct Python MCP fallback.
```

Stop.

### Step 2: Start Interview

```
Tool: ouroboros_pm_interview
Arguments:
  initial_context: <user's topic or idea>
  cwd: <current working directory>
```

**This response carries the first question, so Step 3 applies to it** — including
the fan-out in 3-A2. The first question is the one most likely to be answered
from memory, so it is the last one to skip evidence on.

### Step 3: Loop

Apply this to **every** MCP response that carries a question, including the one
Step 2 returned and any question re-shown on resume.

**Except one:** a response with `meta.evidence_recorded=true` acknowledges a
recorded observation, not a question turn. Do not show it, do not ask
anything, and do not fan out — its `meta.pending_question` is the question
already on screen, repeated so you can see nothing was consumed. Continue from
where you were.

**A. Show alerts** (if present in `meta`):
- `meta.deferred_this_round` → print `[DEV → deferred] "question"`
- `meta.decide_later_this_round` → print `[DEV → decide-later] "question"`
- `meta.pending_reframe` → print `ℹ️ Reframed from technical question.`

**A2. Fan out the evidence lanes — required before you ask the user anything.**

**You do not look at the repositories yourself. Ever.** This skill has no
code-answer path: there is no step where you run Read/Glob/Grep or a docs MCP
to answer a PM question, and finding the answer quickly on your own is the
failure, not a shortcut past it. Evidence the PM cannot trace back to a lane is
evidence the record does not contain — it is not bound to the question, not
bounded by the roster, and not checked against the answer contract. This skill
is self-contained: everything you need is here and in the tool response, so do
not go looking for exploration rules in another skill's file.

**When `meta.question_advisory_subagents` is present you MUST fan out.** Show
the question text to the user first, then treat each entry as a spawn-ready
payload with `title`, `agent`, `prompt`, and `context`, and dispatch every
payload through your host's native subagent mechanism — Claude Code → **one
Task/Agent call per payload in one parallel batch**; Codex → one native Codex
subagent per payload; a runtime with no parallel primitive → process them
sequentially. Pass each payload's `prompt` unchanged rather than rewriting it.

This holds regardless of dispatch mode: **the payloads themselves are the spawn
signal.** `meta.question_advisory_host_action=spawn_subagents` is a reinforcing
cue, not a prerequisite. The only time you skip spawning is when the host has no
subagent primitive at all.

**Say what is running.** Same shape the regular interview uses: after the
question, set off by a divider, one line naming how many perspectives are running
and what they are — then what arrives when they finish.

```
---
While you answer this question, two perspectives are reviewing in parallel
(code context / data measurement). When they return I will put what they found
next to the question as grounds.
```

Two things differ from the interview's line, and both follow from this tool
having two lanes instead of six:

- Name the perspectives in the user's terms, not by lane id. `code_context` is
  an identifier for the fan-out, not something the reader needs.
- **End at "grounds", never at "options".** The interview can promise to
  organise the results into answer choices because it runs a lane that produces
  them. This tool does not, and a promise the synthesis cannot keep trains the
  user to expect the one thing the lanes must never hand them.

Write the line in the language the user is speaking.

**Do not go to step B until the lanes have returned.** Step B is where you ask
the user, and asking before the evidence arrives is the exact failure this
mechanism exists to prevent: the PM decides without the two things they could
not have looked up themselves.

**Submitting results back.** Correlate by
`meta.question_advisory_result_correlation_key` (`context.lane_id`) and call
`ouroboros_submit_fanout_results` with `meta.question_advisory_fanout_id`,
passing `session_id` explicitly. Submit **every lane you hold**, not only the
new ones: a lane that ran and found nothing still submits its output, and a lane
you could not spawn at all is submitted as
`{ "key": <lane id>, "undispatched": true }` — the literal `true`, with no
`content` beside it. Never invent output for a lane you did not run; a
fabricated finding is worse than a missing one.

There are two lanes and both are required: `code_context` and `data_context`.
Neither can become the answer — there is no prefix to forward and nothing to
confirm. Do not skip asking the user because a lane answered clearly.

**Synthesize into the evidence block.** This is what
`synthesis_contract.output_shape = "evidence_beside_question"` means, and it is a
fixed shape so the same session twice looks the same twice. Print it immediately
above the question, then ask the question unchanged:

```
Evidence (examined: billing-api, storefront)

What the code does today
  · [billing-api] src/billing/lapse.py  — access continues to period end
  · [storefront]  src/checkout.ts       — access is revoked immediately
    ! These two repositories implement this differently.

Measured — active subscriptions by plan, last 90 days
  · standard 12,480 / premium 3,120
```

Write the block in the language the user is speaking. The labels above are
placeholders for its shape, not text to copy.

**What this block is not.** It carries no answer options, no recommendation, no
ranking, and no "therefore …" sentence. The moment it proposes an answer it has
stopped being evidence — that is the whole difference between this tool and the
regular interview, which does synthesize options. A PRD asks what the system
*should* do, and everything above says what it *does*.

Rules for building it:

- **Each claim keeps its repository.** Never merge two repositories' claims into
  one line, and never present a disagreement as one policy with an exception —
  flag it, as above. That contradiction is the most useful thing the PM can be
  shown, and it is the first thing a tidy summary destroys.
- **Always print the examined scope.** `examined_repository_ids` is what every
  other statement is relative to; "found nothing" across two of five repositories
  means something different from across all five.
- **Carry measurements as reported** — the lane's `metric`, its groups, its
  numbers. Do not re-scale, combine, or round; you did not run the read.
- **A no-op lane gets at most one line, and often none.** Read the reason as a
  statement about the lane rather than about the user's system:
  `not_a_policy_question` / `not_a_measurement` → print nothing, this question
  simply is not that kind. `no_repository_in_roster` /
  `roster_repository_not_readable` / `store_described_but_not_callable` → yours
  to handle (nothing registered, a path did not open, a store did not answer);
  do not relay any of them as "your system has no such policy/data".
- **Drop evidence the user has already answered past.** If they answered while
  the lanes were still running, do not re-open a settled decision with it.
- **Evidence from outside the roster is a suggestion, not evidence.** It is
  rejected at submission, so relaying it as a finding would show the user
  something the record does not contain. Offer to register the repository
  instead, so the next question can be answered against it.

**B. Show content + get user input** (once A2's lanes have returned):

The question text was already shown in A2 and the user may answer it at any
point; what waits here is your formal prompt, not the person.

Print the MCP content text to the user first, with the lane findings beside it.

Tell users they do not need to invent speculative answers. If a question is
unknown, stakeholder-dependent, too broad, or safer to decide later, route it
through the existing assumptions / decide-later / deferred mechanisms instead
of presenting it as a confirmed requirement.

Then check: does `meta.ask_user_question` exist?

- **YES** → Pass it directly to `AskUserQuestion`:
  ```
  AskUserQuestion(questions=[meta.ask_user_question])
  ```
  Do NOT modify it. Do NOT add options. Do NOT rephrase the question.

- **NO** → This is an interview question. Use `AskUserQuestion` with `meta.question`.
  - If `meta.skip_eligible == true`: add a skip option based on `meta.classification`:
    - `classification == "decide_later"` → add option `{"label": "Decide later", "description": "Skip — will be recorded as an open item in the PRD"}`
    - `classification == "deferred"` → add option `{"label": "Defer to dev", "description": "Skip — this technical decision will be deferred to the development phase"}`
  - Generate 2-3 suggested answers as the other options. Include a non-speculative
    uncertainty option when appropriate, such as `Not sure yet — record as an
    assumption or decide-later item`.

**C. Relay answer back:**

If the user chose "Decide later" → send `answer="[decide_later]"`.
If the user chose "Defer to dev" → send `answer="[deferred]"`.
Otherwise → send the user's answer through the Refine gate below.

**Refine gate — structure it, mark whose it is, then have the user confirm.**

Always structure the answer, including when the user only picked an option. The
text you send is MCP's only context for the next question and for what the PRD
records as decided, so a bare label loses everything around the decision. What
makes structuring safe is not restraint — it is the two things below.

**Mark whose each section is.** A section carrying the user's own words is
labelled `(user-stated)`. A section that is your reading of their answer is left
unmarked, and a reader can tell them apart at a glance:

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

Omit any section you have nothing for. An empty `Constraints (user-stated)` is
better absent than filled with something plausible, and `Reasoning` drawn from
nothing they said is the failure this labelling exists to make visible.

**No codebase-context section.** The regular interview adds one, because there
the main session inspects code itself. Here it does not: the lanes did, and
their findings ride the same call in the `evidence` field, where they are
recorded as an adopted fact. Putting a lane's finding in this payload instead
would record it as part of the user's decision.

**Then confirm — this is the gate, and it is what licenses the structuring
above.** One `AskUserQuestion` before sending:

```json
{
  "questions": [{
    "question": "I structured your answer as follows before sending it:\n\n<payload>\n\nIs anything missing or misrepresented?",
    "header": "Refine — preserve your answer",
    "options": [
      {"label": "Send as-is", "description": "The structure captures my answer faithfully"},
      {"label": "Fix the reasoning", "description": "That is not why I decided it"},
      {"label": "Add to Constraints", "description": "I want to add a constraint I forgot"},
      {"label": "Let me rewrite it", "description": "I will restate the answer myself"}
    ],
    "multiSelect": false
  }]
}
```

`Fix the reasoning` is there because the unmarked section is the one you wrote.
Append `[refined]` only after this confirmation: an unconfirmed structure carries
your reading of the answer under the user's name, and the PRD cannot tell the
difference later.

**Send the answer and what it was weighed against in one call.** `evidence`
carries the lanes' findings, prefixed `[from-code]` / `[from-data]`:

```
Tool: ouroboros_pm_interview
Arguments:
  session_id: <meta.session_id>
  <meta.response_param>: <the refined answer, or "[decide_later]" / "[deferred]">
  evidence: |
    [from-code] billing-api src/billing/lapse.py: access continues to period end.
    [from-code] storefront src/checkout.ts: access is revoked immediately.
    [from-data] active subscriptions by plan, last 90 days: standard 12,480 / premium 3,120.
```

**One call, not two.** Evidence recorded after the answer call has missed the
question it exists to inform: that call already wrote the next one. Sending it
later is not a slower version of this — it is a different thing, recorded
against a decision it was not weighed for.

**What to put there.** The lanes' findings as they reported them: no conclusion
of your own, and nothing from a no-op lane — there was nothing weighed. If both
lanes were no-ops, omit `evidence` entirely. The server records it as an adopted
fact, so requirement extraction withholds it and it never becomes a PRD
requirement, and it answers no question.

**D. Check completion:**

Completion is determined ONLY by `meta.is_complete` — NEVER by the response text.
The MCP response text may sound like the interview is wrapping up, but ignore it.

If `meta.is_complete == true`:
- If `meta.generation_failed == true` → retry generation:
  ```
  Tool: ouroboros_pm_interview
  Arguments:
    session_id: <session_id>
    action: "generate"
    cwd: <current working directory>
  ```
- Otherwise → go to Step 4. The MCP auto-generated the PM document.
  `meta.pm_path` and `meta.seed_path` contain the file paths.

Otherwise → repeat Step 3, regardless of what the response text says.

### Step 4: Copy to Clipboard

Read the pm.md file from `meta.pm_path` and copy its contents to the clipboard:

```bash
cat <meta.pm_path> | pbcopy
```

### Step 5: Show Result & Next Step

Show the following to the user:

```
PM document saved: <meta.pm_path>
(copied to clipboard)

PM seed handoff artifact: <meta.pm_seed_path or meta.seed_path>
This is not the runnable Seed yet.

Next step:
  ooo interview <meta.pm_seed_path or meta.seed_path>
  ooo seed
```

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
