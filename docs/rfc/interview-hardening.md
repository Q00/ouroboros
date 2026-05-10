# RFC: Interview Hardening — Prompt Budget, Refine/Restate Gates, Adapter MCP Isolation

## Status

**Proposed (2026-05-11).**

Three coupled changes harden the interview phase against silent answer
truncation, single-line answer compression, and self-spawn recursion of
the ouroboros MCP server. The changes are independent enough to ship as
three separate PRs but motivated by a single underlying observation:
**the interview LLM is a question generator with no tools, and the main
session is the only context channel into it.** Anything that drops, compresses,
or recursively re-spawns context degrades the interview's Socratic quality.

This RFC anchors the three PRs:

1. `feat/interview-prompt-cap-raise` — raise per-answer and total prompt
   budgets so structured answers reach the LLM intact.
2. `feat/interview-refine-restate` — encode the Refine and Restate gates
   into `skills/interview/SKILL.md` so the main session sends multi-section
   answers and confirms a one-line goal before seed generation.
3. `feat/interview-adapter-isolation` — force `strict_mcp_config=True` on
   the `ClaudeCodeAdapter` used by `InterviewEngine` so the spawned
   `claude` subprocess does not boot ouroboros-mcp recursively.

## Context: the interview is the main session's question, not the LLM's

`commands/interview.md` and `skills/interview/SKILL.md` already establish
that the interview MCP tool is **a pure question generator**. It does not
read code, browse the web, or execute tools. The main session does all
that work and feeds the results into the interview as answers.

That makes the interview unusually sensitive to two things that other
phases tolerate:

- **Answer fidelity.** A truncated or compressed answer becomes the
  ground truth for ambiguity scoring and the only signal driving the
  next question. Other phases (execute, evaluate) re-derive context from
  artifacts; the interview cannot.
- **Adapter recursion.** Other phases run for seconds and complete in a
  handful of LLM calls. The interview runs for many rounds back-to-back
  in the same Python process. Any per-call overhead — including the
  `claude` subprocess loading a project `.mcp.json` and booting plugin
  servers — accumulates round over round.

The three changes below address those sensitivities, each with empirical
evidence below.

## Change 1 — Raise prompt budgets

### Problem

`InterviewEngine` in `src/ouroboros/bigbang/interview.py` capped per-answer
content at **800 chars** and total prompt at **4,800 chars** (system
prompt minimum 1,200, leaving ~3,600 for history). The 800-char cap was
introduced to work around a Claude Agent SDK CLI quirk where overly
large prompts produced empty responses, but the value was applied
uniformly across all adapters and made it impossible to send anything
richer than a sentence to MCP.

The downstream effect: a structured answer like

```
[from-user][refined]
Decision: Stripe Billing.
Reasoning: ...
Constraints: ...
Codebase context: ...
```

was stored intact in `state.rounds` (the security validator allows up to
10 KB) but **silently truncated to 800 chars + `…`** when
`_build_conversation_history` constructed the next-question prompt. The
Constraints, Codebase context, and Out-of-scope sections never reached
the LLM. The next question was generated against the first sentence
alone, which short-circuited the dialectic into a label-confirmation
loop.

### Decision

Raise the two caps:

| Constant | Old | New |
|---|---|---|
| `_MAX_TOTAL_PROMPT_CHARS` | 4,800 | 16,000 |
| `_MAX_USER_RESPONSE_CHARS` | 800 | 4,000 |

`_trim_messages_to_budget` continues to enforce the total budget, so the
total cap remains the safety net.

### Evidence

A single LLM round trip with a 1,223-char structured answer through
`ClaudeCodeAdapter` (`claude-sonnet-4-5`):

```
[1/4] start_interview                       OK
[2/4] ask_next_question (Q1, 149 chars)     OK
[3/4] record_response                       full preserved: True
[4/4] ask_next_question (Q2, 358 chars)     OK, no empty response
        Q2 quoted: "Stripe Billing", "monthly/annual recurring plans"
        signals matched: ['stripe', 'billing']
```

Q2 directly references the answer's reasoning sections — content that
would have been past the 800-char cut under the old caps. Five-round
stress with growing answers preserves the most recent ~3.5 rounds at
full fidelity and trims older rounds via the existing budget guard.

### Risk

The 800-char cap protected against an Agent SDK CLI empty-response bug.
We did not encounter that bug at the new cap during testing across
single calls and 5-round sequences with `claude-sonnet-4-5`. If a
specific adapter (likely the older `gemini-cli`) regresses, the next
step is per-adapter capability instead of a global rollback — see Future
Work.

## Change 2 — Refine and Restate gates in SKILL.md

### Problem

The existing Step 3 of `skills/interview/SKILL.md` Path A demonstrated
answer payloads as one-line strings:

```
answer: "[from-code] JWT-based auth in src/auth/jwt.py"
        or "[from-user] Stripe Billing"
```

That format was readable but **collapses the user's reasoning,
constraints, and scope decisions into a label**. Combined with the
800-char cap, it ensured that even when the user articulated rich
context, the main session would compress it before forwarding. The
Socratic intent of the underlying skill set
(`wonder` / `reflect` / `refine` / `restate`) — particularly the
`refine` rule "여기 빠진 결은 없습니까?" — was not encoded anywhere in
the interview flow.

### Decision

Add two gates and a payload schema:

- **Step 3 — Multi-section payload by default.** Single-line answers
  are permitted only for PATH 1a auto-confirmed facts and short PATH 2
  answers (yes/no/single noun). All free-text answers are sent as a
  multi-section block (Decision / Reasoning / Constraints / Out of scope
  / Codebase context).

- **Step 4 — Refine before forwarding.** Free-text answers go through a
  single `AskUserQuestion` confirming the structured payload preserves
  every reasoning, constraint, and scope element. The user can accept,
  add to a section, or rewrite. Auto-confirmed facts and option-pick
  confirmations skip this gate.

- **Step 9 — Restate gate (post-Acceptance Guard).** After the
  Seed-ready Acceptance Guard passes, the main session restates the
  agreed goal as a single sentence and asks the user to confirm. This is
  the one place where one-line compression is the goal — the rest of
  the interview kept everything in multi-section form.

The Path B agent-mode flow inherits both gates by reference.

### Why "Refine" is reinterpreted, not copied

The standalone `refine` skill collapses meaning candidates into one
shared-meaning line. Applying that literally inside the interview would
re-introduce the same compression problem this RFC is solving. The
underlying principle of `refine` is "miss no nuance" — that principle is
preserved by Step 4, even though the mechanism is structure preservation
rather than line consolidation. The one-line restatement happens once,
at the Restate gate, where compression is the explicit goal.

### Evidence

Same round-trip as Change 1: with structured payloads in place, Q2
references the answer's specific reasoning rather than asking for a
restatement of the headline decision. The interview becomes a dialectic
about constraints and tradeoffs instead of a label-confirmation loop.

## Change 3 — Adapter MCP isolation for question generation

### Problem (the one the user flagged as critical)

`ClaudeCodeAdapter` accepts a `strict_mcp_config: bool` parameter whose
docstring states:

> "Used exclusively by callers that must avoid recursion into the
> ouroboros MCP server (notably the interview policy path); generic
> ``allowed_tools`` envelopes keep MCP-tool access intact."

The interview policy path **never set this flag**. As a result, every
LLM call from `InterviewEngine` spawned a `claude` subprocess that
discovered the project `.mcp.json` and booted ouroboros-mcp inside the
subprocess. For interviews running inside the ouroboros repository
itself this produced a self-spawn loop: ouroboros-mcp → interview tool
→ `claude` subprocess → ouroboros-mcp → ...

The boot cost accumulated per round. We measured the same 3-round
interview in the same cwd:

| | Q1 | Q2 | Q3 |
|---|---|---|---|
| Without isolation | 11.3 s | 38.4 s | **102.8 s** |
| With isolation | 22.0 s | 19.6 s | 32.6 s |

Without isolation Q3 exceeded the default 120 s timeout when run with a
shorter timeout, manifesting as "interview hangs at round 3" with no
useful error.

### Decision

`InterviewEngine.__post_init__` automatically wraps any adapter that
exposes a `with_strict_mcp_config()` method. The wrapper returns an
adapter clone with `strict_mcp_config=True` set; if the adapter is
already strict the wrapper returns `self` (idempotent). Adapters
without the method (`anthropic`, `litellm`, etc.) are untouched.

`ClaudeCodeAdapter` gains a `with_strict_mcp_config()` factory method
that copies its construction parameters and returns a new instance.
This avoids mutating an adapter shared with non-interview phases.

The implementation surface is small:

```python
# src/ouroboros/bigbang/interview.py — InterviewEngine.__post_init__
self._isolate_adapter_for_question_generation()

def _isolate_adapter_for_question_generation(self) -> None:
    wrap = getattr(self.llm_adapter, "with_strict_mcp_config", None)
    if callable(wrap):
        self.llm_adapter = wrap()
```

```python
# src/ouroboros/providers/claude_code_adapter.py — new method
def with_strict_mcp_config(self) -> "ClaudeCodeAdapter":
    if self._strict_mcp_config:
        return self
    return ClaudeCodeAdapter(... strict_mcp_config=True)
```

### Why force, not opt-in

The interview LLM has **no legitimate use** for plugin MCP servers — by
design it does not read code, browse the web, or call tools. Forcing
isolation removes a footgun that has been present since
`strict_mcp_config` was introduced and never actually used.

If a future use case needs interview-time access to an external MCP
server, that should be modeled as a new explicit option (a sibling of
`strict_mcp_config`), not as accidental inheritance from project state.

### Evidence

Unit:

- `InterviewEngine(llm_adapter=raw_adapter)` — `engine.llm_adapter` is a
  new instance with `_strict_mcp_config=True`.
- `InterviewEngine(llm_adapter=already_strict)` — `engine.llm_adapter is
  already_strict` (idempotent).
- `InterviewEngine(llm_adapter=non_claude_adapter)` — adapter unchanged
  (no `with_strict_mcp_config` method).

Real LLM (cwd intentionally pointing at ouroboros-gemini3, which has
`.mcp.json`):

```
[real] post-init adapter strict=True
[Q1] 22.0s (117 chars)
[Q2] 19.6s (122 chars)
[Q3] 32.6s (770 chars)
```

No latency accumulation, no timeout. Q3 produces a 770-char question —
when isolation removes the MCP overhead the model uses its budget on
question quality instead of subprocess churn.

## Out of scope

- **Per-adapter cap capability.** Change 1 keeps a single global cap. A
  follow-up RFC will replace the constants with per-adapter capability
  fields if Change 1 regresses on a specific adapter.
- **Reflect-lite (paired-meaning option in PATH 1b/4 confirmations).**
  The original sketch included a "your meaning vs. my meaning" option
  pair in confirmation questions. We deferred this to keep the SKILL
  patch surface small; the Refine gate captures the same intent for
  free-text answers where it matters most.
- **Subagent-path caps** (`mcp/tools/subagent.py:51-55`). The 300/220/600
  caps in the OpenCode subagent dispatch path are unchanged. They are a
  different code path (not used in the main MCP-mode interview) and
  warrant a dedicated audit.

## Migration / Rollout

All three PRs are backward compatible:

- **PR 1** changes only constant values; no API changes. Worst case is
  reverting two integers.
- **PR 2** is a documentation-only change to `skills/interview/SKILL.md`.
- **PR 3** adds a method to `ClaudeCodeAdapter` and one call in
  `InterviewEngine.__post_init__`. Adapters without the method are
  untouched. The change is observable to operators only as faster,
  more reliable interviews.

We recommend landing in this order:

1. **PR 3 (adapter isolation)** first — fixes a latent recursion bug
   that exists today, regardless of the other two changes.
2. **PR 1 (cap raise)** next — unlocks the dialectic but is benign on
   its own.
3. **PR 2 (Refine/Restate gates)** last — depends on PR 1 (without the
   raised caps, multi-section payloads would still be truncated) and
   benefits from PR 3 (faster rounds make the extra Refine
   `AskUserQuestion` feel cheap).

## Open Questions

- Should `with_strict_mcp_config` be promoted onto the base `LLMAdapter`
  protocol with a default `return self`? Current implementation uses
  duck typing via `getattr`, which is enough for now.
- Should `_MAX_TOTAL_PROMPT_CHARS` become an env-overridable config so
  power users can tune it without a code change? Likely yes if Change 1
  proves stable across adapters.
