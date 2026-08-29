# Review Conventions

`ouroboros-agent[bot]` ("ourobot") records review verdicts against specific
commits. Before acting on a verdict, verify that its associated commit is the
PR's current head.

The bot is strict, and it is strict in a predictable, repeatable way. Most
review rounds are lost to objections that could have been preempted before the
first push. This page exists so you can preempt them.

It is tempting to read a page like this as a checklist to satisfy. Do not. The
recurring blockers catalogued below are all the same question wearing different
clothes — *is this change structurally in the right direction?* — and a
contributor who memorizes a list of rules will lose time to the next case
nobody catalogued. Start with the question. The rules are what it looks like
when the answer was no.

Everything here was extracted from the bot's actual review bodies on this
repository's PRs, not from a style guide. Some PRs clear review quickly; PR
#1926 accumulated 64 `CHANGES_REQUESTED` reviews across 60 commits and was
closed unmerged.

## What the review actually contains

The exact headings evolve with the review contract, but every review reports
the same stable concepts:

1. **Verdict and exact HEAD** — `APPROVE` or `REQUEST_CHANGES`, tied to the
   commit the bot checked. If you pushed after it started, the verdict describes
   the older commit.
2. **Progress against the issue** — what improved, how each linked-issue
   requirement grades (*Met*, *Partially met*, or *Not met*), and the status of
   findings carried from earlier rounds.
3. **Findings by disposition** — merge-blocking findings, follow-up warnings,
   and non-blocking suggestions are separated rather than blended together.
4. **Verification and design assessment** — test coverage, architectural and
   directional notes, and the design/roadmap gate explain the evidence and the
   shape of the change.
5. **Merge recommendation** — the final action follows from those findings.

Treat these as concepts, not an exhaustive heading template; the authoritative
contract may add or rename sections without changing how contributors should
interpret the review.

Two consequences worth internalizing:

- **Your PR is graded against its declared boundary and every baseline contract
  that boundary cannot waive.** The issue supplies the requested outcome; the
  PR description declares supported conditions and non-goals; public contracts,
  approved RFCs and maintainer decisions remain binding. This is why
  [issue quality](./issue-quality-policy.md) and the
  [Review Boundary Contract](../../CONTRIBUTING.md#review-boundary-contract)
  both matter.
- **The bot reproduces things.** Findings routinely cite a probe it ran —
  *"a focused probe classified such a companion as `decide_later` and made it
  skip-eligible"*, *"A focused runtime probe with `process.wait()` returning
  immediately and both streams remaining open exceeded an outer 0.2-second
  guard"* (#2224, #2239). You are not arguing with a linter. If you claim a
  path is unreachable, expect it to be tested.

## Is this change structurally in the right direction?

Ask this before your first push, and again every time a round lands. It is the
only question on this page that matters, and the rest of the page exists
because it is the one people skip.

This project's whole premise is that most failure is upstream of the code —
you solve the root problem or you spend forever on symptoms. Review applies
that premise to your diff. A round is not an obstacle between you and merge.
It is evidence about your direction.

So when a round arrives, there are two possible readings:

- **This specific thing is wrong.** Fix it, push, done. Most rounds.
- **The reviewer found this because the shape makes it findable.** Fix the
  specific thing and another one appears, because you are defending a position
  that cannot be defended. No number of rounds closes this.

Telling those apart early is the whole skill. What it costs to get wrong:

| PR | Direction taken | `CHANGES_REQUESTED` reviews | Outcome |
|---|---|---|---|
| #1926 | "**bind** regex evidence to acceptance input" — make the check correct enough to be trusted | 64 across 60 commits | **closed, never merged** |
| #2065 | "**refuse** unbound regex evidence as grounds to overturn an agent FAIL" — stop trusting it | 3 | merged |

The same defect. Sixty-four change-request reviews versus three. #2065 did not out-argue the
reviewer and did not write a better check — it asked whether that component
should have been allowed to overturn a verdict at all, and the answer was no.

Nobody in #1926 was careless. The repeated findings were answered. The question that
would have ended it in week one was never asked.

### The signal that you should be asking it

You are not obliged to introspect after every comment. These are the specific
signs that the next round will not be the last one:

- The same finding **reappears at a different line number**. You are moving the
  failure, not removing it.
- Each fix **adds surface** — another branch, another special case, another
  entry in a table.
- The blocker count is **not trending down** after three rounds.
- You are arguing about **which inputs are legitimate** rather than about what
  the code does.
- You find yourself thinking *"the reviewer keeps finding edge cases"*. Edge
  cases that keep existing are not edge cases. They are the shape.

### What asking it actually looks like

Not "which of the known traps is this?" — that is pattern-matching, and
pattern-matching is how you end up with a fourth trap nobody catalogued.
Re-derive from the problem instead:

1. State, in one sentence, the property the reviewer keeps attacking.
2. Ask what would have to be true for that property to be **unnecessary**.
   Usually one of: the component should not hold this authority; the default
   should be refusal rather than acceptance; the truth belongs to something
   else that should be asked directly; the two things that must agree should be
   one thing.
3. Ask whether the original problem is still solved by that shape. If it is,
   you have the change you should have opened.
4. Say it in the PR: the current shape cannot close, here is the one that can.
   A stated structural argument gets engaged with — the reviewer's own
   non-blocking notes are frequently pointing at it already. In #2212 a round-5
   suggestion proposed one authoritative parser instead of parallel option
   tables; the merged repair instead bounded the affected parser and rejected
   unknown or malformed option forms.
5. Be willing to close the PR and open a differently-shaped one. #2065 is not a
   failure story. It is the correct ending to #1926.

### Three ways review pressure changed the design

Read these as worked examples of a boundary becoming explicit, not as a
taxonomy to match your situation against.

**Conservatively parsing a surface you do not own** (#2212, 10 change-request
reviews). The verification path needed to recognize `uv run` without accepting
non-executing or malformed commands as test evidence. Repeated reviews found
missing value-taking options and unsafe command forms. The merged repair kept a
bounded option table for that path, rejected unknown or malformed forms, and
covered compound and non-executing modes. The current tree still has a separate
`uv run` parser in `evaluation/detector.py`; this example is about fail-closed
evidence validation, not eliminating every option table.

**Keeping compatibility paths under one contract** (#2193, 7 change-request
reviews). The atomic PM path and legacy two-call fallback both remained. The
repair aligned decision counting and Seed eligibility across them, then made
the fallback boundary explicit through the atomic-turn capability flag. The
lesson is not that one path must disappear; it is that capability differences
must not change shared interview semantics.

**Removing authority from an unreliable component** (#1926 → #2065, above).

In all three, specific line findings exposed an authority, parsing, or
compatibility boundary. The durable repair stated that boundary in code and
tests instead of patching each observed example.

### Not an escape hatch

This is not permission to dismiss a finding you do not want to fix. "The
structure is wrong" is a claim that has to survive step 3 above — a different
shape that still solves the original problem. Without that, it is just a
disagreement, and the reviewer is probably right.

## Recurring defect classes

These are common evidence sources, not unconditional scope expansion. Apply the
Review Boundary Contract before deciding whether a reproduced defect blocks the
current PR or becomes an owned follow-up.

### 1. Validate untrusted input — never coerce it

`bool(...)` on an external payload is a blocker, not a shortcut:

> "Companion routing fields are coerced with `bool(...)` instead of validated
> as booleans. An untrusted planner payload such as `"decide_later": "false"`
> is interpreted as `True`" — #2224

Anything crossing a trust boundary — a planner payload, an MCP argument, a
config file, `./.env` — gets parsed and validated against a closed set of
accepted values.

### 2. Honor the public schema and never report silent success

A code path that accepts a request, does nothing, and reports success is
always a blocker:

> "The public tool schema accepts `answers`, but plugin mode only examines the
> singular `answer` variable. A request such as `{"session_id": id, "answers":
> [{"question": "Which workflow?", "answer": "Review"}]}` enters the plugin
> branch, records no rounds, and still returns a successful delegation receipt."
> — #2224

Corollaries the bot enforces: do not truncate silently, do not drop items from
a batch without saying so, and do not swallow an exception into a default.

### 3. Keep the same semantics across every runtime

This project drives many backends. Behavior that changes meaning depending on
which one is active is a blocker:

> "Plugin mode records every normalized pair through
> `InterviewState.record_answer`, bypassing `record_turn_answers`, so the
> documented per-question `[deferred]` and `[decide_later]` controls change
> meaning by runtime." — #2224

When you add a behavior to one adapter, check the others.

### 4. Preserve crash-safety and idempotency on persistence paths

Commit ordering and retry behavior are reviewed explicitly:

> "Batch answer persistence is not replay-safe because interview state is
> committed before the authoritative `pending_batch` metadata is updated." — #2224

> "The new answer path is not idempotent after a successful write.
> `record_turn_answers` unconditionally appends every supplied pair, while the
> handler lock only serializes calls and persists no turn/request identity
> that could recognize a transport retry." — #2224

A lock serializes; it does not make an operation idempotent.

### 5. Bound every wait

Any subprocess wait, network call, or stream drain needs a finite ceiling, and
the timeout must surface through the normal error contract rather than as a
bare exception:

> "Timeout from the changed legacy `communicate()` path is not translated into
> the adapter’s `Result.err(ProviderError)` contract or followed by child
> cleanup." — #2239

Bounding the primary wait is not enough if a stream drain sits outside the
deadline (#2239).

## Preempting a round

Before your first push, walk your own diff and answer:

- **Is the shape right?** If this is defending a position rather than removing
  a failure, stop here — see [the question](#is-this-change-structurally-in-the-right-direction).
- Does this remove the cause, or relocate the symptom?
- Every new input: validated, or coerced?
- Every new branch: can it succeed while doing nothing?
- Every new wait: bounded, and does the timeout surface as a typed error?
- Every new behavior: does it mean the same thing on the other runtimes?
- Every new conditional: is there a test that reaches it?
- Does the linked issue list a requirement this PR does not meet? Say so
  explicitly in the PR body, with the reason.

Writing that reasoning into the PR description is not ceremony. The bot reads
it, and a stated, justified scope boundary is treated differently from a
requirement that is simply unmet.

## Verification honesty

State what was run, what could not run, and what residual risk remains. A green
focused suite is not a full-suite claim, and an unavailable runtime is not a
successful smoke test. Review can classify an explicit gap; it cannot recover a
check that the PR silently implies was completed.

## Mechanics

- **Re-review is triggered by a comment.** Push your fix, then post a comment
  explaining what changed. A push alone is unreliable; empty commits and
  re-requesting a reviewer do not work. Turnaround is typically 7–15 minutes.
- **The review pins a commit.** Compare the commit associated with the submitted
  review in the PR review metadata with the PR's current head; a verdict may
  predate your latest push.
- **Prior findings carry forward.** Respond to each one, in the PR
  conversation, even if the answer is "not doing this, because …".
- **Pushing back is legitimate.** The bot is wrong sometimes. Say concretely
  why — with the code path or a reproduction — and it can reverse itself; a
  round of explanation is cheaper than a bad change. What does not work is
  silently ignoring a finding.

## Related

- [CI Gates and Branch Protection](./ci-gates.md) — the automated gates
- [Issue Quality Policy](./issue-quality-policy.md) — why the issue text is graded
- [Verifier Evidence Policy](./verifier-evidence-policy.md) — evidence rules in the verifier
