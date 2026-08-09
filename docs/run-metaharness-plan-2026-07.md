# Run Meta-Harness Plan 2026-07 — Verify-by-Default → Context Pack → Cross-Harness Supremacy

> Status: PROPOSED PLAN (2026-07-02). Baseline: `main` @ `7bc011dc`.
> Origin: 6-agent fan-out audit (run failure modes, injection points, brownfield gap,
> dashboard overlap, meta-harness brainstorm, external harness-landscape research).
> Companion to the [historical master roadmap](./history/master-roadmap-2026-07.md)
> snapshot — this plan BUILDS ON PR-E/F/G/H
> (AC success contract) and PR-D (run→eval chain); it does not replace them.

---

## 0. Diagnosis — why `ooo run` underperforms on hard domains & brownfield

Structural root cause: **the default run path is `parallel=True`
(`cli/commands/run.py:555` → `ParallelACExecutor.execute_parallel`), and the entire
recovery/verification stack lives on branches that path never takes** (sequential
runner, auto pipeline, MCP evaluate handlers).

Ranked failure modes (file:line evidence from direct audit):

| # | Defect | Evidence |
|---|--------|----------|
| 1 | AC completion = worker's own word. `success = not message.is_error`; real verification gated behind `fat_harness_mode` (default OFF); `HeadlessRunProbe` imported only by `auto/` | `parallel_executor.py:5749`, `:6015`, `:6052-6060`; `run.py:418` |
| 2 | Workers start blind — no repo map, no build/test commands, no conventions. Only brownfield signal is interview-text-derived strings, and `""` when `project_type != "brownfield"` (default greenfield) | `runner.py:316-322`, `seed_contract_prompt.py:31-54`, `seed.py:122` |
| 3 | Failed AC is never retried; `ac_retry_attempts` is a dead counter (never incremented); failure blocks the whole dependent subtree | `parallel_executor.py:3722`, `:4061-4074`, `:4124-4127`, `:3956-3968` |
| 4 | Stagnation detection + lateral recovery absent from parallel path — `RecoveryPlanner` fenced to sequential ("Same-session recovery is limited to the sequential runner") | `runner.py:2549-2558`; zero grep hits in `parallel_executor.py` |
| 5 | ACs are bare prose strings with no machine-checkable oracle | `core/seed.py:256` — **fixed by roadmap PR-F/G** |
| 6 | Effort routing dormant by default; global tier; decomposed (harder) children get LESS effort | `config/loader.py:786-808`, `effort_routing.py:104-107` |
| 7 | Dependency edges LLM-guessed from prose; no LLM adapter → zero edges → everything parallel on a shared workspace | `dependency_analyzer.py:34-48`, `:536`; `runner.py:878,905` |
| 8 | Predecessor→dependent context is a lossy 200-char tail + file NAMES only | `level_context.py:212-240` |
| 9 | Transient retry shallow (3×, ~7s total), terminal on exhaust → permanent AC death; zero retry on goose/hermes | `adapter.py:1430-1594`, `providers/goose_cli_adapter.py`, `hermes_cli_adapter.py` |
| 10 | Trust leaks: `_complete_sibling_acs_from_evidence` flips FAILED→success from a sibling's self-report; `--skip-completed` green-lights without verification | `parallel_executor.py:2189-2231`, `:3915-3948` |

Brownfield-specific: context is lost at **seed generation**, long before run —
`_build_interview_context` omits `codebase_context` (`seed_generator.py:455-464`);
extraction template hardcodes `PROJECT_TYPE: greenfield` and never requests
brownfield keys (`seed_generator.py:495-503`); `ooo auto` never passes
`brownfield_context` (`auto/ledger_seed.py:106-165`); `.ouroboros/mechanical.toml`
verify commands are read ONLY by evaluation Stage 1, never by worker prompts.
The `BrownfieldContext` schema + renderer already exist and are complete
(`core/seed.py:107-135`, `seed_contract_prompt.py:31`) — the hole is the FILLING side.

External-landscape verdicts that shape this plan (sources in §6):
- **"Verify is the bottleneck, not generation"** — universal conclusion across
  2025-26 orchestrator literature. Phase V is therefore first.
- **Cross-vendor N-version verification is unclaimed territory** — parallel
  execution across vendors is mature (Vibe Kanban, Conductor, Claude Squad), but
  no product does cross-vendor verification of the same artifact. Ouroboros's
  runtime-factory (12 backends) makes it uniquely cheap. Phase X is the 위엄 play.
- **LLM-generated context files are counterproductive** (ETH Zurich 2026-03:
  −3% success, +20-23% cost). The context pack must be DETERMINISTIC FACTS ONLY
  (commands, file map, versions) — never speculative LLM-written advice.

Answer to the "session start hook?" question: **not a host-side hook.** The right
mechanism already exists as a single choke point — `runner.build_system_prompt`
(`runner.py:292-322`) reaches every runtime (folded into the prompt for CLI
runtimes, passed as native developer-instructions for the Tier-A worker pool via
`--append-system-prompt` / `developer-instructions`). A hook would be
Claude-host-only; the system-prompt fragment is provider-agnostic.

---

## Dependency map

```
(roadmap PR-E, PR-F)  ──→ V1 (orchestrator-run verify needs verify_command)
V1 (evidence gate)    ──→ X1/X2/X3 (cross-harness moves are only trustworthy
V2 (AC retry)          │   when acceptance is orchestrator-verified)
V3 (stagnation)        │
V4 (trust-leak fixes)  │
C1 (scanner) ─→ C2 (pack injection) ─→ C3 (seed pipeline fixes) — parallel with V
X0 (runtime picker) ─→ X1 (alt-harness redispatch) ─→ X2 (cross-review) ─→ X3 (tournament)
D1 (merge web kanban + dead TUI cut) — independent, anytime
```

Safe order: **V (V1‖C1 first) → C → X → D continuous.** Phase V and C are
independent tracks; X depends on V1.

---

## Phase V — Verify by Default (close the loop)

The single highest-impact change. Today a hard-domain worker "believes" it
finished; nothing catches the false positive.

### V1: Orchestrator-run evidence gate (default ON)
- When an AC carries `verify_command` (roadmap PR-F spec), **ouroboros executes
  it itself** after the worker finishes and grades from its own exit code/stdout —
  worker self-report becomes irrelevant. Reuse `HeadlessRunProbe`
  (`orchestrator/runtime_evidence.py`, today auto-only) inside the parallel path.
- Contract-less ACs: keep today's behavior but flip evidence extraction from
  `observe_only` to enforcing when `fat_harness_mode` semantics allow; emit
  `missing_success_contract` findings (PR-F Step 4) so the repair loop fills them.
- Failure feeds the existing failure taxonomy — no new codes.
- Note: this is PR-G's transcript-verification extended one level further:
  PR-G checks the worker's CLAIMED evidence against the transcript; V1 re-runs
  the command independently. Ship as a follow-up PR to PR-G.

### V2: Wire per-AC retry into the parallel path
- Make `ac_retry_attempts` real: on non-stall failure, re-dispatch up to N (2)
  attempts before marking FAILED; include the failure taxonomy class + prior
  attempt's error tail in the retry prompt.
- Kill criteria (external prior art): same failure class 3× → stop retrying same
  approach, escalate (V3 lateral, or X1 alt-harness once built).
- Blocked-subtree rescue: when a failed AC later succeeds on retry, unblock
  dependents instead of leaving them skipped.

### V3: Stagnation detection + lateral injection in parallel path
- Port the sequential runner's `RecoveryPlanner` loop (`runner.py:2558`) into
  `ParallelACExecutor`: per-AC stall/oscillation detection
  (`resilience/stagnation.py` already exists) → inject a lateral directive into
  the worker's next attempt (V2 retry vehicle). Bound: 1 lateral injection per AC.

### V4: Close the trust leaks
- `_complete_sibling_acs_from_evidence`: require the V1 orchestrator-run verify
  to pass before flipping a FAILED sibling.
- `--skip-completed`: run the AC's `verify_command` before treating tree state
  as satisfied; no command → keep today's behavior but stamp
  `verification_status="assumed"`.

### V5 (small, opportunistic): retry parity + effort defaults
- Add transient-retry to goose/hermes adapters (reuse `providers/retry.py`).
- Effort: stop lowering decomposed children one notch; raise effort one notch on
  V2's second retry attempt (hard ACs get MORE reasoning, not less).

**Done means (phase):** a seed with verify_commands cannot report success unless
ouroboros itself observed the commands pass; a first-attempt AC failure no longer
kills its subtree; a stuck worker gets one lateral-informed retry.

---

## Phase C — Session-Start Context Pack (brownfield priming)

### C1: Deterministic repo scanner (`ooo brownfield` upgrade)
Produce a **facts-only** context pack per repo (ETH warning: no LLM-generated
advice):
- Tech stack + versions (from lockfiles/pyproject/package.json — parsing, not LLM)
- Verify commands: test/lint/build/format (source: `.ouroboros/mechanical.toml`
  detection already exists in `evaluation/detector.py` — reuse, don't rebuild)
- Compact repo map: top-level layout + key entry points; aider-style tree-sitter
  symbol ranking is the north star, but v1 = directory tree + exported symbols
  of the most-referenced modules, hard token budget (~1.5k tokens)
- Storage: extend `brownfield_repos` (`persistence/brownfield.py`) or a sidecar
  `.ouroboros/context_pack.json`; regenerate on staleness (git HEAD change).

### C2: Inject the pack at the single choke point
- New fragment in `runner.build_system_prompt` (`runner.py:292-322`): if the run
  cwd (or seed `context_references`) resolves to a scanned repo, append the pack.
  Reaches ALL runtimes: Tier-B CLIs fold it into `<system-directive>`/`## System
  Instructions`; Tier-A worker pool passes it natively
  (`claude_worker_runtime.py:218`, `codex_mcp_runtime.py:194-196`).
- Also feed it to `context_governor.compose_context`'s reserved `parent_summary`
  slot (`context_governor.py:28`) for governed leaf dispatches.
- Inject verify commands into the worker's VERIFY section
  (`build_execute_subagent`, `subagent.py:1604-1607`) — closes brownfield gap #7.

### C3: Stop dropping brownfield context in seed generation
- `_build_interview_context`: include `state.codebase_context` + `codebase_paths`
  (`seed_generator.py:455-464`).
- Extraction template: remove the `PROJECT_TYPE: greenfield` hardcode; request
  `CONTEXT_REFERENCES`/`EXISTING_PATTERNS`/`EXISTING_DEPENDENCIES` when brownfield
  was detected (`seed_generator.py:495-503` — parser prefixes already exist).
- `ooo auto`: pass `brownfield_context` through `synthesize_seed_from_ledger`
  (`auto/ledger_seed.py:106-165`).
- Delete or wire the dead `_resolve_brownfield_target_dir` lookup
  (`cli/commands/run.py:199-205` — key exists nowhere).

### C4 (follow-ups, per-runtime native channels)
Lowest-friction first: copilot `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` append
(`copilot/cli_policy.py:176-179`); claude worker `--add-dir` for
`context_references`; opencode/gemini/kiro per-run instruction files (path
helpers already in `runtime_instruction_artifacts.py`). These are optimizations —
C2 alone reaches every runtime.

**Done means (phase):** a brownfield run's worker prompt contains repo facts +
the project's real test/lint commands, on every runtime, without any interview
having mentioned them.

---

## Phase X — Cross-Harness Supremacy (the meta-harness 위엄)

Prereq: V1. Routing/racing between unverifiable claims is routing between guesses.

### X0: Shared runtime picker
One helper: "give me a healthy (runtime × backend) that is NOT the one that just
failed", consulting capability gates (`backends/capabilities.py`) and
availability. Built once, consumed by X1/X2/X3.

### X1: `REDISPATCH_ALT_HARNESS` recovery action
- New action in `orchestrator/failure_taxonomy.py`'s RecoveryPolicy table
  (today: RETRY / ESCALATE_MODEL / REDISPATCH / ESCALATE_HUMAN — none switch
  harness). On `FABRICATION_SUSPECTED`, `STALL` (post-V3 lateral), or sustained
  429/529 (rate-limit failover), redispatch the SAME AC to a different harness
  via X0. A stall is often runtime-specific, not task-specific.
- Cheapest 위엄 win: one enum value + policy edits + picker.

### X2: Cross-harness adversarial review (no self-grading)
- Bind each AC's reviewer to a DIFFERENT (runtime × backend) than its
  implementer. `evaluation/consensus.py` already requires ≥2 distinct voters —
  the missing piece is enforcing executor≠reviewer at dispatch.
- Scope: start with evaluate-stage reviewer binding (cheap), then optional
  post-AC diff review for `critical`-flagged ACs.
- Prior art check: no product does this today (Star Chamber = a Claude Code
  skill; SentinelOne = custom PoC). First-class support is genuinely novel.

### X3: N-version tournament for hard ACs (gated)
- AC flagged hard (or 2× V2-retry-failed) → dispatch to 2-3 diverse harnesses in
  isolated worktrees; winner = whichever passes the V1 orchestrator-run verify
  first. Selection by evidence, not vote. Strictly cost-gated (opt-in flag +
  hard-AC heuristic).
- Worktree-per-task isolation is the 2026 orchestrator standard — required here.

### X4: Benchmarking flywheel (later)
- Aggregate live EventStore outcomes per (runtime × backend × failure class):
  accepted-first-try, recovered-within-N. Metric shapes already exist
  (`orchestrator/baseline_metrics.py` — fixture-fed today). Feed X0/X1 routing
  weights. This is the moat: only a meta-harness ever sees comparative
  cross-harness outcomes.

**Done means (phase):** a run can survive one harness's bad day (X1), no AC is
graded by its own author's vendor (X2), and the hardest ACs get evidence-decided
diversity (X3).

---

## Phase D — Dashboard Consolidation (continuous track)

Facts (verified): web kanban is NOT in main — it lives on unmerged branch
`feat/dashboard-web` (`fbe4832a`, ~1.3k LOC, already wired to auto-launch from
execute_seed/start_auto with opt-out `OUROBOROS_DASHBOARD=0`). TUI is 11.5k LOC
of which ~2.5-3k is confirmed-dead (screens/dashboard.py, dashboard_v2.py,
hud_dashboard.py + widgets fed only by no-op "removed" handlers). The web surface
is the ONLY one showing per-worker provider identity — the meta-harness view.

**Decision: Option B now, Option C as the target.**
- **D1 (now):** merge `feat/dashboard-web` (rebase onto current main — it
  predates v0.44.0); delete the dead TUI layers (verify non-reference from
  execution.py/logs.py first). Keep dashboard_v3 (pause/resume, debug inspector,
  logs — no web equivalent). Keep `ac_tree_hud` as the headless default.
- **D2 (target):** promote `dashboard_web/kanban.py`'s pure reducer to a shared
  `events → board` projection consumed by BOTH web page and a slimmed TUI screen —
  kills the dual-reducer drift risk and gives TUI provider identity for free.
- X3/X1 make this urgent rather than cosmetic: cross-harness runs need the
  provider-tagged board to be legible.

---

## Full idea list from the fan-out (ranked, impact-per-effort)

Kept for reference; ✅ = adopted into a phase above.

1. ✅ Evidence-gated AC completion (V1) — H × S/M
2. ✅ Cross-harness adversarial review (X2) — H × S/M
3. ✅ Failure-taxonomy alt-harness redispatch (X1) — H × S — best ratio in list
4. ✅ Rate-limit/overload failover across harnesses (X1) — M/H × S/M
5. ✅ Session-start context pack (C1-C2) — H(brownfield) × M
6. Parallel-safety-aware scheduling from capability graph (CapabilityMutationClass
   etc. exist, nothing consumes them) — M × S — good V-phase follow-up
7. Universal deliver-gate claim contract across harnesses
   (`harness/claim_term_guard.py` applied uniformly) — M × S
8. Cross-harness simplifier pass (independent harness trims the diff, re-verify;
   the +12,970 vs +26 LOC lesson) — M × S — natural X2 extension
9. ✅ Harness-capability-aware AC routing (X0 substrate; full domain routing
   deferred until X4 data exists) — H × M/L
10. ✅ Benchmarking flywheel (X4) — H × M
11. ✅ N-version tournament (X3) — H × M
12. Worker peer-review chains (A implements, B reviews, C confirms) — M × M —
    deferred; X2 first
13. Checkpoint/resume across harnesses (EventStore-backed, resume a Claude run
    on Codex) — M × M/L — deferred
14. Per-harness frugal effort routing (extend effort plumbing beyond
    anthropic/litellm + codex; ties to frugality-proof seed) — M × M
15. ✅ Provider-tagged live board (D1 — merge, not rebuild) — M × S

Explicitly rejected: **auto-generating AGENTS.md/context advice via LLM**
(ETH evidence: negative value). The context pack stays deterministic.

---

## 6. External evidence (for the PR descriptions)

- Verify-is-the-bottleneck + orchestration patterns: addyosmani.com/blog/code-agent-orchestra
- Devin self-verification "come back with proof": cognition.ai/blog/testing-development
- Harness survey (verification layer 𝒱, Rel_k): arxiv.org/html/2606.20683v1
- AGENTS.md counter-evidence (ETH Zurich): arxiv.org/html/2602.11988v1 +
  infoq.com/news/2026/03/agents-context-file-value-review
- Cross-vendor consensus prior art (all sub-product maturity):
  blog.mozilla.ai/the-star-chamber-multi-llm-consensus-for-code-quality,
  mindstudio.ai/blog/cross-vendor-ai-agent-review-claude-codex
- Repo-map priming: aider.chat/docs/repomap.html
- Orchestrator landscape (11 tools, all worktree-based, none cross-verify):
  augmentcode.com/tools/open-source-agent-orchestrators
- Context compaction convergence: arize.com/blog/context-management-in-agent-harnesses,
  anthropic.com/engineering/effective-context-engineering-for-ai-agents

---

## Phase A (backlog, post-4-PR) — Artifact-First Harness Loop

Owner proposal (2026-07-02, Discord discussion): make the interview→seed→evaluate→
evolve loop itself the meta-harness — "queryable traces of failures and judgments,
then let Fable/Codex/Claude read those artifacts and fix the harness."
NOT in the current 4 PRs (they cover the execution layer). Ordered by dependency:

1. **A1 — Provenance gate (do first; known bug class).** Tag every seed-ledger
   decision with `source=` (user_confirmed / model_inferred / timeout_default /
   lateral_consensus / maintainer_policy). `timeout_default` and `model_inferred`
   entries must pass a low-ambiguity gate before the seed is executable.
   This is exactly the #1485 failure (interview_phase_deadline → degraded seed
   with question-text pollution) made structurally impossible. Substrate: auto
   ledger `committed_decisions` anchoring already exists — extend, don't rebuild.
2. **A2 — Interview trace artifact.** Per interview run, a grep-able export:
   questions, answers, ambiguity-score trajectory, lateral results,
   timeout/fallback flags, promoted vs rejected decisions, evaluate outcome.
   Implementation: projection over the EXISTING EventStore + auto ledger
   (they already record most of this) → `.ouroboros/traces/<run_id>/` files.
   No new store.
3. **A3 — Experience-store CLI.** `ouroboros harness list / show <id> /
   diff <a> <b> / trace <id> --grep <pat> / frontier --metric ...` — thin CLI over
   A2's projections (query_events/query_projection MCP tools already exist as
   the read path).
4. **A4 — Strategy variants as candidate harnesses (LAST; needs A2 metrics).**
   baseline_interview / with_contrarian / with_simplifier / with_3way_lateral /
   with_provenance_gate as named strategies; compare on ambiguity reduction,
   seed correctness, token cost, user friction (Pareto via A3 frontier).
   Proposer/validator/evaluator/archive/selector separation applies here —
   the evaluator must sit outside the proposing agent (same no-self-grading
   principle as PR-X X2). CAUTION: do not start A4 before A2 has real data —
   unmeasured strategy proliferation is the Wonder-ontology divergence failure
   mode again.

## Open decisions (owner)

1. **V1 default**: orchestrator-run verify ON by default (recommended), or
   opt-in first release? Recommendation: ON for contract-carrying ACs, observe
   for contract-less.
2. **X3 cost gate**: which heuristic flags an AC "hard" (2× retry-failed is the
   safe default; seed-authored `hard: true` flag is the explicit one)?
3. **D1 timing**: merge `feat/dashboard-web` before or after Phase V lands
   (independent — can go first for morale/visibility).
4. **Phase ordering vs master roadmap**: PR-E/F/G are prerequisites for V1;
   recommend interleaving — roadmap PR-A/B/E/F → V1‖C1 → PR-G → V2-V4 → C2-C3 → X.
