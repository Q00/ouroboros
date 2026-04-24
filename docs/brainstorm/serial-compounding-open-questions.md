# Serial Compounding — Open Questions (Brainstorm)

> Status: **pre-decision**. Sibling doc: [`../guides/serial-compounding.md`](../guides/serial-compounding.md).
> Every claim here is tagged with a file:line reference from a codebase audit on merge commit `c1bc167` (upstream sync completed 2026-04-24).
> Purpose: pick a direction per question before the next milestone.

## Ground rules

- No "probably" or "might be" — every question includes what the code **actually does today**.
- Every option includes a **rough cost** (LOC, risk class, insertion point).
- Every question ends with a **lean + rationale**, not a final decision.
- Questions are ordered by urgency (blocks or enables next milestones).

---

## Q1 — Decomposition × compounding: what happens to sub-ACs?

### What the code does today

`SerialCompoundingExecutor` (`src/ouroboros/orchestrator/serial_executor.py:82`) extends `ParallelACExecutor` and inherits `enable_decomposition` (default `True`, `runner.py:423`). When a top-level AC has sub-ACs:

- `_try_decompose_ac` still fires (`parallel_executor.py:2169`), and sub-ACs are executed sequentially within the parent (`parallel_executor.py:2209-2282`).
- The parent `ACResult` carries `sub_postmortems: tuple[ACPostmortem, ...]` (`parallel_executor.py:519`).
- **But** `SerialCompoundingExecutor._build_postmortem_from_result` (`serial_executor.py:344-408`) **never reads `sub_postmortems`** — only the top-level outcome is written into the chain.

### What this means

Sub-AC knowledge is **silently discarded** from the compounding chain today. The original doc framed this as "decomposition turns one AC into sub-ACs; they already run sequentially" — technically true, but sub-AC gotchas, files touched, and invariants never reach AC-(N+1).

### Options

| Opt | Description | Cost | Side effects |
|---|---|---|---|
| **A** | Status quo: keep silent; document it. | ~0 LOC | Hidden regression risk when sub-ACs do important work. |
| **B** | Inline sub-postmortems into the parent's digest (concat `files_modified`, merge `gotchas`). | ~60 LOC in `serial_executor.py:344`. | Parent digest grows; deterministic. Loses sub-AC boundaries in serialized chain. |
| **B-prime** | Option B for prompt rendering, **plus** preserve `sub_postmortems` in the serialized chain state (unrendered but queryable). | ~80 LOC (B + keep `sub_postmortems` in checkpoint state, don't render). | Two-view chain: flat prompt, structured persistence. Unlocks sub-AC-boundary resume (see Q6). |
| **C** | Render sub-postmortems as nested chain entries (`AC 3.1 [pass]`, `AC 3.2 [fail]`). | ~150 LOC: chain accumulation + digest rendering change in `level_context.py:617`. | Changes chain shape; may break prompt-cache assumptions. |
| **D** | Add `--no-decomposition` alias that flips the default when `mode="compounding"`. | ~20 LOC at `run.py` / `runner.py:423`. | Simplest semantics, but loses decomposition's benefit. |

### Lean

**Option B-prime. DECIDED.** Sub-AC work is real work; losing it defeats compounding. Flattening (not nesting) keeps the prompt-cache story intact for phase 2. Preserving `sub_postmortems` in persistence unlocks Q6's sub-AC-boundary resume — deleting that information would force Q6 into restart-from-top-of-AC for every decomposed failure. Implementation note: `ACResult.sub_postmortems` already exists (`parallel_executor.py:519`); the change is "don't throw it away when serializing the chain," not "compute it anew."

**Coupling with Q6:** this option is chosen specifically to enable Q6's sub-AC-boundary resume model.

---

## Q2 — Diff capture: what backend, and when?

### What the code does today

- `ACPostmortem.diff_summary: str = ""` (`level_context.py:510`) — **no population site anywhere** (grep confirms).
- **No** `WorkspaceSnapshotBackend` interface exists (not stubbed, not imported).
- **No** `subprocess` calls invoking `git` anywhere in `orchestrator/`.
- Event-based file reconstruction **already works**: `ACContextSummary._extract_files_modified` (`level_context.py:368-371`) pulls `tool_input.file_path` from Write/Edit/NotebookEdit events. This is how `files_modified` in postmortems is populated today.

So we're not at zero — we have "which files" but not "what lines changed."

### Options

| Opt | Description | Cost | Notes |
|---|---|---|---|
| **A** | Defer entirely. Rely on `files_modified` list. | 0 | No line-level diff in chain. |
| **B** | Session-start ref + `git diff --stat <ref> HEAD` per AC. | ~100 LOC; one subprocess per AC. | Deterministic, user sees nothing in `git log`. |
| **C** | `git stash create` before each AC → stash SHA as snapshot. | ~120 LOC. | HEAD-free but undiscoverable; stashes accumulate. |
| **D** | Per-AC commit (`--commit-per-ac` opt-in). | ~150 LOC + commit message convention. | Pollutes worktree; users may not want it. |
| **E** | Introduce `WorkspaceSnapshotBackend` ABC now with null + git-stat backends. | ~250 LOC. | Unblocks future GitButler/jujutsu/etc. |

### Lean

**Option B, shipped as phase 1.5.** Deterministic, silent (no worktree pollution), backend-neutral (plain git). The ABC (Option E) is over-engineering until a second backend shows demand. **Blocks:** nothing critical, but phase-2 prompt-caching wins more when diff_summary is non-empty (more cache-friendly stable prefix).

---

## Q3 — `invariants_established` extraction: what interface?

### What the code does today

- `ACPostmortem.invariants_established: tuple[str, ...] = ()` (`level_context.py:514`) — always empty.
- Rendering is live and tested: `PostmortemChain.to_prompt_text()` (`level_context.py:617-683`) emits a "Cumulative invariants" section only when the tuple is non-empty (line 654-656). Dedupe-in-insertion-order works (verified by `tests/unit/orchestrator/test_level_context.py`).
- **No extractor exists anywhere**: no post-AC meta-prompt, no tag parser, no adapter-layer hook.
- Prior art for structured extraction: the assertion-extraction and ontology-analysis models already exist (`get_assertion_extraction_model`, `get_ontology_analysis_model` in `config/loader.py`) — pattern is there.

### Options

| Opt | Description | Cost | Cost/AC |
|---|---|---|---|
| **A** | Defer. Chain never has cumulative invariants. | 0 | — |
| **B** | Post-AC meta-prompt: "state 1-3 invariants future ACs can assume." | ~80 LOC hook at `serial_executor.py:268`. | +1 main-model call per AC. |
| **C** | Tag convention `[[INVARIANT: ...]]` parsed from `result.messages` / `result.final_message`. | ~40 LOC regex. | Free (agent emits inline). |
| **C-plus** | C + Haiku verifier + occurrence/reliability scoring. Only invariants above threshold render into future ACs' prompts. | ~140 LOC. | +1 Haiku call per invariant (~$0.0005). |
| **D** | Hybrid: tag parser first; if empty, meta-prompt fallback. | ~150 LOC. | 0-1 main-model calls per AC. |

### Lean

**Option C-plus. DECIDED.** Pure C is cheap but trusts the agent unconditionally; a wrong invariant silently corrupts every downstream AC. C-plus adds a Haiku sanity gate at negligible cost (~$0.01 per 20-AC run) and introduces a **reliability score** the chain can learn from — the same layered/scored retrieval pattern QCS uses for context chunks. **Blocks:** nothing, but this is "where compounding actually compounds."

### C-plus design

**Flow per AC:**
1. Agent emits `[[INVARIANT: <text>]]` tags inline during normal work (instructed once in compounding system prompt).
2. Regex parser extracts tags from `result.final_message` / `result.messages`.
3. For each tag: Haiku verify pass — prompt: "Given this AC's files changed, work trace, and output, is this invariant actually supported? Return a reliability score 0.0–1.0."
4. Occurrence tracker:
   - New invariant → record with Haiku's reliability score.
   - Re-declared invariant (semantic match to prior) → occurrence count bumps, reliability blended with new Haiku score.
   - Contradicted invariant (e.g., later AC says `[[INVARIANT: NOT X]]` where X was prior) → prior is demoted or dropped.
5. Render gate: only invariants above threshold (default 0.7) appear in downstream ACs' prompt chain.

**Tag format:**
- `[[INVARIANT: <text>]]` — single-line, max ~200 chars per tag.
- Multiple tags per AC allowed.
- Strict double-bracket to keep regex unambiguous (`\[\[INVARIANT:\s*([^\]]+)\]\]`).
- No nested brackets inside text (escape or reword).

**Schema change required:**

```python
# level_context.py:514 BEFORE:
invariants_established: tuple[str, ...] = ()

# AFTER:
invariants_established: tuple[Invariant, ...] = ()

# New type:
@dataclass(frozen=True)
class Invariant:
    text: str
    reliability: float  # 0.0 - 1.0, from Haiku verifier
    occurrences: int    # bumps on re-declaration
    first_seen_ac_id: str
```

**Verification: inline, not async.** The whole point of invariants is the next AC's prompt sees them. Async verify breaks the invariant — the score doesn't land in time to gate inclusion. Inline cost is ~1–2s per AC per invariant on Haiku, acceptable.

**Where to instruct the agent:** **Option (c)** — once in the compounding system prompt only. Keeps the stable prefix (phase-2 prompt caching compatible). Add per-AC reinforcement *only* if the dogfood run shows agents forget.

**Configuration:**
- `OUROBOROS_INVARIANT_MIN_RELIABILITY` — default `0.7`. Below-threshold invariants are captured and scored but not rendered into downstream prompts.
- `OUROBOROS_INVARIANT_VERIFIER_MODEL` — default `claude-haiku-4-5`. Override for cost/quality tuning.
- Existing `get_mechanical_detector_model` pattern (`config/loader.py:921`) gives us the routing template.

**Implementation insertion points:**
- Parser: new `extract_invariant_tags(messages: list[Message]) -> list[str]` in `level_context.py`, called at `serial_executor.py:268` after `_build_postmortem_from_result`.
- Haiku verifier: new `verify_invariants(tags, ac_trace, files_modified) -> list[tuple[str, float]]` in `serial_executor.py`.
- Occurrence logic: method on `PostmortemChain` — `chain.merge_invariants(new_invariants, source_ac_id)`. Handles match (semantic via cosine or simple normalization), contradiction detection, and score blending.
- Render gate: update `PostmortemChain.to_prompt_text()` at `level_context.py:654-656` to filter by reliability.

---

## Q4 — QA retries: share stall counter or add `--max-qa-retries`?

### What the code does today

- Stall retries cap: `MAX_STALL_RETRIES = 2` (`parallel_executor.py:164`) → 3 total attempts per AC. Timeout: `STALL_TIMEOUT_SECONDS = 300.0` (`parallel_executor.py:162`).
- Retries tracked per-AC: `ac_retry_attempts: dict[int, int]` (`parallel_executor.py:1516`).
- **QA is NOT wired inline.** `QAHandler` exists (`mcp/tools/qa.py:397`) but receives **zero calls** from `parallel_executor.py` or `serial_executor.py`. QA is an MCP tool the agent calls on itself if it decides to.
- `QAVerdict(score, verdict, dimensions, differences, suggestions, reasoning)` (`qa.py:85-94`) — production-ready structured output.

### What this means

The original question (separate counter vs share) is downstream of the real blocker: **inline QA isn't implemented**. You can't have a QA-retry counter without a QA call. So M7 has two parts:

1. Wire QA inline (the real cost).
2. Choose retry semantics (cheap).

### Options for part 2 (assuming part 1 is done)

| Opt | Description | Cost | Behavior |
|---|---|---|---|
| **A** | Share existing stall counter (max 2 retries regardless of cause). | ~20 LOC | Simple; confounds stall and quality failures. |
| **B** | Separate `--max-qa-retries` (default 1). | ~50 LOC + env var. | Independent tuning; better diagnostics. |
| **C** | Retry matrix: stall + QA fail → different retry prompts (stall = resume, QA = apply feedback). | ~150 LOC prompt-builder branch. | Best UX; most complex. |

### Lean

**Option B for the retry counter; but first settle part 1.** Wire `QAHandler` at `serial_executor.py:268-271` after postmortem construction, before chain append. Gate behind `--inline-qa` (default off — doubles model cost). Then add `--max-qa-retries` default 1. **Blocks:** M7 milestone in full.

---

## Q5 — Evolve × compounding: auto-trigger, end-of-run, or user-driven?

### What the code does today

- `ouroboros_evolve_step` is an MCP tool (`mcp/tools/evolution_handlers.py:105`, `StartEvolveStepHandler` at line 7). User-invoked only.
- **Zero calls** to evolve from any executor. No "stagnation detected" signal, no `should_evolve` logic anywhere.
- Grep for `stagnation` in `orchestrator/`: **zero hits**.

### What this means

Evolve is a heavy, user-driven process today. The question "does each AC trigger a micro-evolve" has a concrete answer: **no, not currently, and there's no wiring to do so.**

### Options

| Opt | Description | Cost | Risk |
|---|---|---|---|
| **A** | Status quo: user manually calls `ooo evolve` between runs. | 0 | None; probably correct. |
| **B** | End-of-run summary log: "5/20 ACs passed — consider `ooo evolve --from-lineage <id>`." | ~30 LOC in runner. | None; informational. |
| **C** | Emit `execution.seed.stagnant` event when success_count < threshold. MCP layer can subscribe. | ~60 LOC + threshold config. | Low; needs threshold UX. |
| **D** | Auto-trigger evolve on in-run failure after N retries. | ~200 LOC + recursion guards. | High — can blow up cost/time. |

### Lean

**Option B for phase 1.5; Option C only if users ask.** Auto-triggering evolve (D) is a trap — recursive cost, unclear semantics. A printed hint at run end is the right nudge. **Blocks:** nothing; this is purely enhancement.

---

## Q6 — Resume × compounding chain (NEW)

### What the code does today

- `ooo resume` exists (`cli/commands/resume.py`, added upstream): lists sessions. Actual resume wiring runs through `runner.py`.
- `CheckpointData.state: dict[str, Any]` (`persistence/checkpoint.py:42`) — schema-flexible, **no** `last_completed_ac_index` field, **no** postmortem chain field.
- `SerialCompoundingExecutor` has **no per-AC checkpoint writes** (grep: no `CheckpointStore.save` inside `serial_executor.py`).
- `serialize_postmortem_chain` / `deserialize_postmortem_chain` exist (`level_context.py:716+`) and round-trip is tested.

### What this means

A 20-AC compounding run that crashes at AC 15 loses everything today. The chain *can* be serialized, but nobody's calling it.

### Resume model (decided)

Sequential execution means any failure is localized to **one AC**. Prior ACs are already done — no reason to re-run them. The question is what to do about the *failing* AC's partial work.

**Decided semantics:**

| Failing AC shape | Resume behavior |
|---|---|
| **Decomposed (has sub-ACs)** | Resume at the last completed sub-AC boundary. Sub-AC boundaries *are* the natural checkpoints. Requires Q1 = B-prime so `sub_postmortems` survives serialization. |
| **Monolithic, little partial work** | Restart from top of the failing AC. Discard partial file changes if reversible, or let the agent overwrite. |
| **Monolithic, substantial partial work** | Resume from where it left off. Verify-before-continue — the agent gets the pre-crash trace + original prompt and adjudicates whether state is coherent enough to continue, or whether to restart. |

"Much vs not much" partial work is **delegated to the agent** on resume — consistent with Ouroboros's existing pattern of agent-as-judge. No heuristic threshold; the agent sees the trace and decides. Light agent self-check on resume is sufficient for phase 1.5; heavy Q4 inline QA can land later as independent verification.

### Split into two ACs for the dogfood run

| AC | Scope | Cost | What it delivers |
|---|---|---|---|
| **Q6.1 (B)** | Serialize chain at end of run, including failed runs. Write to `docs/brainstorm/chain-<session>.md`. | ~40 LOC | Inspection artifact; not resumable. |
| **Q6.2 (C)** | Per-completed-AC checkpoint. Resume logic: skip completed ACs, rehydrate chain from checkpoint, enter failing AC with resume semantics above. | ~200 LOC | Full resume model. |

### Options (for historical/alternate paths)

| Opt | Description | Cost | Completeness |
|---|---|---|---|
| **A** | Defer all of M6. | 0 | Useless for long runs. |
| **B** (adopted as Q6.1) | End-of-run chain serialization only. | ~40 LOC | Chain inspectable; not resumable. |
| **C** (adopted as Q6.2) | Per-AC checkpoint + resume with sub-AC-boundary / agent-adjudication semantics. | ~200 LOC (down from ~250 — sub-AC boundaries come free from Q1=B-prime). | Fully resumable per decided model. |
| **D** | C + Q4-style QA verdict replay on resume. | ~300 LOC. | Couples with Q4; defer. |

### Lean (confirmed)

**B then C, split as Q6.1 and Q6.2.** Q6.1 ships fast, delivers inspection value even before resume works. Q6.2 inherits sub-AC boundary survival from Q1=B-prime; the 50 LOC saving is real. Resume semantics are **agent-adjudicated**, not heuristic.

**Coupling:**
- Requires Q1 = B-prime (sub-postmortems preserved in serialized state).
- Does NOT require Q4 (agent self-check is sufficient for phase 1.5 resume; heavier QA is independent).

---

## Q7 — Token-budget overflow eventing (NEW)

### What the code does today

- Budget: `POSTMORTEM_DEFAULT_TOKEN_BUDGET = 8000` (`level_context.py:47`), 4 chars/token heuristic (line 49).
- Trimming in `PostmortemChain.to_prompt_text()` (`level_context.py:617-683`): oldest digests drop first (lines 669-673); full forms + cumulative invariants are preserved (line 682 loop condition).
- **Overflow already logs a warning**: `log.warning("postmortem_chain.over_budget", ...)` at `level_context.py:676`. **No event is emitted.**
- Event factory pattern for postmortem events already exists: `create_ac_postmortem_captured_event` (`events.py`).

### What this means

Silent truncation is a debugging hazard. Logging exists but log lines are grep-only, not structured for subscribers (TUI, event query tools).

### Options

| Opt | Description | Cost |
|---|---|---|
| **A** | Keep log-only. | 0 |
| **B** | Add `create_postmortem_chain_truncated_event` (mirror existing factory) + emit alongside log. Fields: `dropped_count`, `char_budget`, `rendered_chars`, `full_forms_preserved`. | ~50 LOC: 1 factory in `events.py`, 1 call at `level_context.py:676`. |
| **C** | Option B + TUI surface warning (yellow banner). | ~100 LOC. |

### Lean

**Option B.** Cheap, consistent with existing event patterns, unlocks `ouroboros_query_events` visibility. **Blocks:** nothing, but helps debug "why did AC-12 seem to forget AC-3's gotcha?" after the fact.

---

## Meta — should we dogfood compounding on these questions?

Before picking an option per question, a methodology question: **use `ooo run --compounding` on a seed listing Q1 + Q3 + Q6 + Q7 as ACs, and let Ouroboros build its own improvements.**

### Why it's a reasonable test

The whole point of compounding is: a multi-AC run where each AC builds on the last's postmortem. Q1/Q3/Q6/Q7 together are ~400 LOC across 6 files (`level_context.py`, `serial_executor.py`, `events.py`, `checkpoint.py`, `runner.py`, `run.py`) — exactly the shape compounding claims to handle. If compounding can't build its own improvements, that's a strong signal we're not done.

### Chicken-and-egg risks (real, code-grounded)

1. **Q3 gap is load-bearing.** Without `[[INVARIANT: ...]]` extraction shipped, the chain carries `files_modified` and `gotchas` but **not invariants** (`level_context.py:514` — empty today). So the compounding run would execute with a degraded chain until AC-N finishes shipping Q3. Mitigation: **run Q3 as AC-1** so AC-2+ benefit.
2. **Q6 gap is load-bearing.** A 4-AC run that crashes at AC-3 loses AC-1 and AC-2's chain today (`serial_executor.py` has no per-AC checkpoint). Mitigation: **commit after each AC manually**, or **run Q6 as AC-1** so the rest of the chain survives.
3. **Q1 gap is load-bearing.** If any AC's decomposition fires (`parallel_executor.py:2169` — default on), sub-AC work vanishes from the chain. Mitigation: **either run with `--no-decomposition` OR run Q1 as AC-0**.

### Ordering candidates

| Order | Rationale | Risk |
|---|---|---|
| **Q7 → Q3 → Q1 → Q6** | Smallest first; verify the loop runs at all. | If we crash at Q6, we've at least landed the easy wins. |
| **Q6 → Q1 → Q3 → Q7** | De-risk the rest with resume + no-silent-loss. | Q6 is the hardest AC-1 imaginable; if it fails, we've proven nothing. |
| **Q1 → Q3 → Q7 → Q6** | Guards against silent sub-AC loss first, then the core value (Q3), then observability, then resume. | Balanced. Q6 last is fine if we commit per-AC manually. |

### Success criteria

If we do this, we need explicit success criteria — otherwise the run is just vibes.

- **Behavioral**: each AC's final-message mentions at least one fact from a prior AC's postmortem (e.g., AC-3 references the event type added in AC-2). If AC-N's prompt could have been produced without the chain, compounding isn't doing anything.
- **Artifact**: the full postmortem chain is checkpointed to `docs/brainstorm/dogfood-run-<date>.md` as a public showcase.
- **Tests**: existing test suite stays green after each AC lands; new tests added per AC (matching current TDD discipline in `tests/unit/orchestrator/`).
- **Meta**: at least one bug found during the run becomes a new test case.

### What this would teach us (that brainstorming can't)

- **Q3 efficacy**: do agents actually emit `[[INVARIANT: ...]]` tags in practice, or do they forget? Cheapest empirical test available.
- **Q7 usefulness**: does the truncation event actually fire during a realistic 4-AC run, or is 8000 tokens always enough?
- **Prompt-cache fit**: does the stable system prefix (CLAUDE.md + ontology) actually deliver on phase-2's cache promise at realistic chain lengths?
- **UX friction**: where does `ooo run --compounding` actually hurt today — missing flags, unclear output, surprising defaults?

### What could go wrong

- **AC-1 (Q1) silently succeeds but is wrong.** Current QA isn't inline (Q4 gap). Mitigation: manual review after each AC before continuing. Or run with `--inline-qa` once Q4 lands — but that's circular.
- **Run crashes mid-AC.** No resume (Q6 not shipped). Mitigation: commit after each AC manually so at least the diff is recoverable even if the chain isn't.
- **Agent doesn't emit invariants tags.** Q3's `[[INVARIANT: ...]]` convention requires the agent to know about it. Mitigation: the compounding system prompt must be updated with the tag instruction as part of Q3's AC itself.
- **Model cost spike.** 4 ACs × prompt-with-chain × maybe retries. Order-of-magnitude: low-$10s. Acceptable.

### Decision prompt

1. **Do this?** Run compounding-on-compounding as the first post-brainstorm action, or build the improvements linearly by hand?
2. **If yes**: which ordering? (Q7→Q3→Q1→Q6 leans "lowest risk"; Q1→Q3→Q7→Q6 leans "most methodical.")
3. **Scope**: all four (Q1/Q3/Q6/Q7), or a minimal 2-AC warm-up (say Q7 + Q1) as a feasibility test first?
4. **Fallback**: if we crash at AC-K without Q6 shipped, do we commit the partial work, re-seed with AC-K as AC-1, and resume by restart?

---

## Priority / sequencing matrix

| Question | Blocks | Effort | Recommended phase |
|---|---|---|---|
| Q3 — invariants tag parser | Nothing but *core value* of compounding | Low (~40 LOC) | **Phase 1.5 — do first** |
| Q1 — sub-postmortem rendering | Nothing, but silent data loss today | Low-med (~60 LOC) | Phase 1.5 |
| Q6 — resume chain | Long-run reliability | Med (~250 LOC) | Phase 1.5 |
| Q7 — overflow event | Observability only | Low (~50 LOC) | Phase 1.5 — bundle with Q6 |
| Q2 — diff capture | Q4 (richer QA feedback) | Med (~100 LOC) | Phase 2 entry |
| Q4 — inline QA + retry | M7 in full | High (~250 LOC) | Phase 2 |
| Q5 — evolve integration | Nothing | Low hint, high auto | Defer (hint only) |

---

## Decisions log

| Q | Decision | Rationale |
|---|---|---|
| Q1 | **B-prime** — flatten in prompt, preserve `sub_postmortems` in serialized chain state. | Keeps chain shape stable for phase-2 prompt caching; preserves sub-AC boundaries for Q6.2 resume. |
| Q2 | Defer to phase 2 entry. | Event-based `files_modified` reconstruction covers 70% today; diff content is nice-to-have, not blocking. |
| Q3 | **C-plus** — tag convention `[[INVARIANT: ...]]` + Haiku verifier + reliability/occurrence score. | Cheap sanity gate; QCS-style scored retrieval pattern; Haiku cost negligible (~$0.01 per 20-AC run). |
| Q4 | Wire inline QA as M7 (phase 2), opt-in `--inline-qa` default off. | Inline QA isn't implemented at all yet; separate `--max-qa-retries` counter once that lands. |
| Q5 | End-of-run hint only (option B). Defer auto-trigger. | Evolve is heavy; auto-recursion is a trap. |
| Q6 | **B then C** split as Q6.1 + Q6.2. Resume semantics: sub-AC boundary first, agent-adjudicates monolithic. | Agent-as-judge matches existing Ouroboros pattern; saves ~50 LOC vs full-C by leveraging Q1=B-prime. |
| Q7 | **Bundle into Q6.2.** | Both touch `events.py`; same file seam; cheap to add alongside. |

## Execution plan — dogfood run

Run `ooo run --compounding` on a seed listing these ACs. **Ordering and rationale:**

| # | AC | Scope | LOC | Rationale for position |
|---|---|---|---|---|
| **1** | **Q6.1** — End-of-run chain serialization. Serialize `PostmortemChain` to `docs/brainstorm/chain-<session>.md` at run end (including failed runs). | ~40 | Smallest AC. Proves the compounding loop runs end-to-end before trusting harder ACs. Independent — no dependencies. Inspection artifact is immediately valuable. |
| **2** | **Q1** — Flatten sub-postmortems in prompt rendering; preserve `sub_postmortems` in serialized `PostmortemChain` state (B-prime). | ~80 | Must precede Q6.2 (which depends on sub-AC boundaries in serialized state). Fixes silent data loss. |
| **3** | **Q3** — `[[INVARIANT: ...]]` tag parser + Haiku verifier + `Invariant(text, reliability, occurrences)` schema + render gate. Update compounding system prompt with tag instruction. | ~140 | Core compounding value. Later ACs (Q6.2) benefit from Q3's invariants if the Haiku verifier + parser work. Acts as empirical test of Q3 itself — AC-4's chain should show invariants from AC-3. |
| **4** | **Q6.2 + Q7** — Per-completed-AC checkpoint; resume with sub-AC-boundary + agent-adjudicated semantics. **Bundled**: add `postmortem_chain.truncated` event at `level_context.py:676`. | ~200 + 50 | Heaviest AC. Depends on Q1 (B-prime). Benefits from Q3 invariants in its own prompt. Q7 shares `events.py` seam. |

**Total:** 4 ACs, ~460 LOC across 6 files.

### Success criteria for the dogfood run

| Criterion | How we check |
|---|---|
| Each AC's final message references ≥1 fact from a prior AC's postmortem. | Manual review of `result.final_message` per AC. |
| `invariants_established` populated from AC-3 onward. | Inspect serialized chain after AC-3. |
| Haiku verifier assigns non-trivial scores (not all 1.0, not all 0.0). | Log Haiku scores per tag in AC-3. |
| Full postmortem chain artifact written to `docs/brainstorm/chain-<session>.md`. | File exists after AC-1 ships + run end. |
| AC-4 (Q6.2) runs with Q1+Q3 benefits visible in its prompt. | Inspect the prompt sent for AC-4. |
| At least one bug found during the run becomes a new test case. | PR diff shows new test files. |
| Existing test suite stays green after each AC. | `uv run pytest` green after each commit. |

### Failure fallbacks (before Q6.2 ships, resume isn't available)

| Failure point | Fallback |
|---|---|
| Crash during AC-1 (Q6.1) | Re-run from scratch. Only ~40 LOC lost; cheap. |
| Crash during AC-2 (Q1) | Manually commit AC-1's work; re-seed with AC-2 as new AC-1. |
| Crash during AC-3 (Q3) | Same — commit AC-1 + AC-2, re-seed with Q3 as AC-1. |
| Crash during AC-4 (Q6.2 + Q7) | Commit prior ACs; re-seed. This is the last AC; loss is limited. |

### Ordering tradeoff notes

Alternate considered: **Q6.1 → Q3 → Q1 → Q6.2** — core value earlier. Rejected because:
- Q3's schema change (`tuple[str, ...]` → `tuple[Invariant, ...]`) is best landed after Q1's serialization change, not before; avoids two schema migrations.
- Q1 before Q3 means AC-3's own postmortem already benefits from Q1's B-prime (sub-postmortem preservation) if Q3 is decomposed.

## References used for this audit

All file:line references are against the `c1bc167` merge commit (post-upstream-sync). Reviewed files:

- `src/ouroboros/orchestrator/serial_executor.py` (lines 82-408)
- `src/ouroboros/orchestrator/parallel_executor.py` (lines 162-1516, 2169, 2209-2282, 2898-2946, 519)
- `src/ouroboros/orchestrator/level_context.py` (lines 47-49, 175-195, 368-371, 510-514, 608-683, 716+)
- `src/ouroboros/orchestrator/runner.py` (lines 423, 2181)
- `src/ouroboros/orchestrator/events.py`
- `src/ouroboros/mcp/tools/qa.py` (lines 85-94, 397-414)
- `src/ouroboros/mcp/tools/evolution_handlers.py` (lines 7, 105, 194)
- `src/ouroboros/persistence/checkpoint.py` (line 42)
- `src/ouroboros/cli/commands/resume.py`
- `src/ouroboros/cli/commands/run.py`
