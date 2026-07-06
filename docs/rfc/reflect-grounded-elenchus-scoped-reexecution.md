# RFC: Grounded Elenchus & Satisficing Delta — AC-Scoped Re-execution for the Reflect Layer

Status: Draft
Author: Ouroboros core
Scope: `src/ouroboros/evolution/wonder.py`, `src/ouroboros/evolution/reflect.py`,
`src/ouroboros/evolution/loop.py`, `src/ouroboros/mcp/server/adapter.py`

## 1. Problem

The Wonder → Reflect → Seed → Execute → Evaluate cycle re-derives and re-executes
**everything** every generation:

1. **Wonder questions are free-floating.** `WonderOutput.questions` are bare strings
   (`wonder.py:49`) with no link to the acceptance criteria they challenge. "What did
   we miss?" never says *where* we missed it.
2. **Reflect rewrites the whole seed.** `ReflectOutput.refined_acs` is a full
   replacement AC list (`reflect.py:64`), fed verbatim into
   `SeedGenerator.generate_from_reflect` (`seed_generator.py:254`). Even when 9/10 ACs
   passed, all 10 get re-derived by an LLM at temperature 0.5 — which also perturbs
   their wording and breaks the positional AC identity that `RegressionDetector`
   (`regression.py:67-72`) and the per-AC convergence gate rely on.
3. **Execution re-runs every AC.** `_evolution_executor` (`adapter.py:1456`) calls
   `OrchestratorRunner.execute_seed` without any scoping, so the parallel executor
   spawns workers for all ACs each generation, including ones that already passed and
   were not challenged by anything.

Consequences observed in the field: heavy generations dominated by re-work of settled
ACs, wording-drift-induced oscillation, and regression-gate noise.

## 2. What already exists (borrow, don't port)

The execution substrate for scoping is **already built and battle-tested**:

- `OrchestratorRunner.execute_seed(..., externally_satisfied_acs: dict[int, dict[str, Any]])`
  (`runner.py:2089-2132`) skips listed AC indices.
- `ParallelACExecutor` handles them per-node (`parallel_executor.py:3899-3953`):
  dependency validation still runs (a "satisfied" AC downstream of a failed dependency
  is blocked, not skipped), and skipped ACs get
  `ACExecutionOutcome.SATISFIED_EXTERNALLY` with `success=True`.
- Reports count them as COMPLETED (`parallel_executor.py:2403-2409`), so the
  mechanical evaluator (`adapter.py:1492-1500`) sees them as passing.
- `ooo run --skip-completed` (`cli/commands/run.py:601-619`) is the existing consumer.

The evolution loop simply never plumbs into it. That is the entire gap.

## 3. Philosophy

### Socrates — grounded elenchus
An elenchus refutes a *specific* claim held by a *specific* interlocutor. A question
that cannot name what it challenges is not yet a question — it is a mood. Every Wonder
question must therefore be **grounded**: either it *challenges* named AC(s), or it
names a *gap* (something the goal requires that no AC covers). A grounded challenge is
the only licensed way to reopen a settled claim.

### Herbert Simon — satisficing over nearly decomposable systems
- **Satisficing:** an AC that passed evaluation is a satisficed commitment. Rational
  agents with bounded resources do not re-derive satisficed commitments without
  evidence (a regression or a grounded challenge).
- **Near-decomposability** (*The Architecture of Complexity*): ACs are the subsystem
  boundaries of the spec; intra-AC interactions dominate inter-AC ones. Local repair
  of the failing subsystems beats global re-derivation.
- **Means-ends analysis:** the agenda for generation N+1 is exactly the *difference*
  between current state and goal state: failed ACs ∪ regressed ACs ∪ grounded
  challenges ∪ gaps (as new ACs). Nothing else.

Design rule that falls out: **the LLM proposes, deterministic code disposes.** LLM
outputs are advisory; a deterministic backstop enforces the satisficing invariant
(same pattern as the interview refiner's committed-decisions anchor that fixed
oscillation in v0.42.4).

## 4. Design

### 4.1 `wonder.py` — GroundedQuestion

```python
class GroundedQuestion(BaseModel, frozen=True):
    question: str
    kind: Literal["challenge", "gap"] = "gap"
    ac_indices: tuple[int, ...] = ()   # 0-based; required for kind="challenge"

class WonderOutput(BaseModel, frozen=True):
    questions: tuple[str, ...] = ...            # UNCHANGED (events/lineage compat)
    grounded_questions: tuple[GroundedQuestion, ...] = ()   # NEW
    ontology_tensions: tuple[str, ...] = ...
    should_continue: bool = True
    reasoning: str = ""
```

- System prompt (`_system_prompt`): each question must either cite the AC number(s) it
  challenges (`"ac_refs": [2]`, 1-based in the JSON, converted to 0-based on parse) or
  declare `"kind": "gap"`. Passing ACs may only be questioned with evidence from the
  evaluation/execution output.
- Parser: tolerant. If the LLM returns plain strings (legacy shape), fall back to a
  deterministic regex over the question text (`\bAC\s*#?(\d+)\b`, case-insensitive):
  any matches → `challenge` with those indices (out-of-range matches dropped; if none
  remain in range → `gap`); no matches → `gap`. `questions` is always populated
  (derived from grounded questions when the new shape parses) so existing events,
  lineage records, and the repetitive-feedback convergence check are untouched.
- The same regex fallback re-grounds questions restored from interrupted-generation
  `partial_state` (which only persists strings, `loop.py:1403-1407`).

### 4.2 `reflect.py` — ACPatch + deterministic satisficing backstop

```python
class ACPatch(BaseModel, frozen=True):
    op: Literal["keep", "revise", "add"]
    index: int | None = None      # required for keep/revise; None for add
    content: str | None = None    # required for revise/add
    reason: str = ""

class ReflectOutput(BaseModel, frozen=True):
    refined_goal: str
    refined_constraints: tuple[str, ...] = ...
    refined_acs: tuple[str, ...] = ...          # UNCHANGED — composed from patches
    ac_patches: tuple[ACPatch, ...] = ()        # NEW
    settled_ac_indices: tuple[int, ...] = ()    # NEW — indices in the NEW AC list
    ontology_mutations: tuple[OntologyMutation, ...] = ...
    reasoning: str = ""
```

- System prompt: Reflect now **patches** the AC list instead of rewriting it. Explicit
  contract: an AC that passed and is not named by any grounded challenge and not
  regressed MUST be `keep` (verbatim). Failed/challenged/regressed ACs may be
  `revise`d. `gap` questions may become `add` patches. `remove` is NOT offered in v1
  (see 4.5).
- `reflect()` gains an optional `regression_report: RegressionReport | None = None`
  parameter so the loop can pass the already-computed report (today Reflect recomputes
  it internally for the prompt, `reflect.py:337-348`; reuse that when not supplied).
- **Deterministic backstop** (`_apply_satisficing_backstop`), applied after parsing,
  before composing `refined_acs`:
  - Let `protected = {i : AC i passed in eval AND i not in challenged_indices AND
    i not in regressed_indices}` (challenged from `wonder_output.grounded_questions`,
    regressed from the regression report).
  - Any LLM patch that revises a protected index is overridden to
    `keep` (verbatim parent text), logged as `reflect.backstop.forced_keep`.
  - Any index missing from the LLM's patches gets an implicit `keep`.
  - Malformed/duplicate/out-of-range patches are dropped with a warning; the composed
    list must contain every parent index exactly once (keeps/revises in place, adds
    appended in order). Order stability preserves positional AC identity for
    `RegressionDetector`.
- **Legacy fallback:** if the LLM returns no `ac_patches` (old JSON shape), derive
  patches by verbatim positional diff of `refined_acs` vs the parent ACs: identical
  text at the same index → `keep`; different text → `revise`; extra tail entries →
  `add`; a *shorter* list falls back to full-rewrite semantics (no settled indices)
  rather than guessing at deletions. The backstop then applies on top. This means
  scoping works even before any model has seen the new prompt.
- `settled_ac_indices` = indices whose final op is `keep` AND whose AC passed in the
  previous evaluation (kept-but-failing ACs must re-execute).

### 4.3 `loop.py` — scoped execution plumbing

In `_run_generation_phases`, Gen 2+ path:

1. Compute `regression_report = RegressionDetector().detect(lineage)` once; pass it to
   `reflect_engine.reflect(...)`.
2. After seeding, when `self.config.scoped_reexecution` and `reflect_output` exists:

```python
settled = {
    i: {"reason": "satisficed: passed in previous generation; not challenged or regressed"}
    for i in reflect_output.settled_ac_indices
}
```

3. `_call_executor` (`loop.py:1217-1231`) gains an `externally_satisfied_acs`
   parameter, forwarded only when the wired executor accepts it (existing
   `_callable_accepts_keyword` pattern) and non-empty. Resume paths that skip the
   reflect phase but re-execute simply pass nothing (conservative full run).
4. Config: `EvolutionaryLoopConfig.scoped_reexecution: bool = True`, overridable to
   off via env `OUROBOROS_SCOPED_REEXECUTION=0` (read where the config is built in
   `adapter.py:1756-1765`).

### 4.4 `adapter.py` — executor forwarding

`_evolution_executor` (`adapter.py:1456`) accepts
`externally_satisfied_acs: dict[int, dict[str, Any]] | None = None` and forwards it to
`evolution_runner.execute_seed(...)`. Four lines.

### 4.5 Invariants & non-goals

Invariants:
- **A regressed AC is never settled.** Regression indices are subtracted before
  settling.
- **Trust but verify:** `_verify_spec_compliance` (`adapter.py:1529`) still runs
  `SpecVerifier` over ALL assertions against the working tree every generation. A
  stale "satisfied" claim flips to FAIL → regression gate (`convergence.py:139-154`)
  blocks convergence → the AC is excluded from settling next generation. Skipping
  execution never skips verification.
- **Workspace continuity holds:** all generations execute in the same
  `evolutionary_loop.get_project_dir()`, so previously-built artifacts persist and
  skipping is sound.
- **Positional identity is preserved:** keeps/revises stay at their index; adds
  append. This *improves* on today, where full rewrites silently shuffle identity.

Non-goals (v1):
- `remove` op (index shifts would break positional AC history; deferred until stable
  AC IDs / `AcceptanceCriterionSpec` land). An LLM-proposed `remove` is coerced to
  `keep` with a warning.
- Evaluator scoping (evaluation is cheap relative to execution and doubles as the
  safety net).
- Stable AC identity keys (separate workstream).

### 4.6 Convergence interaction

- All-pass tail generations become cheap: every AC settles, execution degenerates to a
  near-noop verification report, and the loop spends its budget on ontology
  convergence instead of redundant rebuilds.
- The per-AC gate, eval gate, stagnation/oscillation detection are all unchanged —
  they consume the same `EvaluationSummary`/ontology signals as before.

## 5. Test plan

New: `tests/unit/evolution/test_wonder_grounding.py`,
`tests/unit/evolution/test_reflect_delta.py`, `tests/unit/evolution/test_scoped_reexecution.py`.

1. **Wonder grounding:** new-shape parse (challenge + gap, 1-based→0-based); legacy
   strings → regex fallback grounding; out-of-range refs dropped (all-dropped
   challenge → gap); `questions` always populated.
2. **Reflect delta:** patch parse; composition order (keep/revise in place, add
   appended); backstop forces keep on passed+unchallenged+unregressed; challenged or
   failed or regressed ACs may revise; kept-but-failed AC not settled; legacy
   full-list fallback diff (identical→keep, changed→revise, longer→add,
   shorter→full-rewrite semantics); malformed patches dropped, every parent index
   present exactly once.
3. **Loop scoping:** settled dict built and forwarded; executor without the kwarg →
   not forwarded (signature guard); `scoped_reexecution=False` → not forwarded;
   empty settled → not forwarded; regression exclusion end-to-end (AC passed gen N-1,
   regressed gen N → executes in gen N+1).
4. **Adapter:** `_evolution_executor` forwards the kwarg (extend
   `tests/unit/mcp/server/test_adapter.py`).
5. **Compat:** existing suites (`test_evolve_step.py`, `test_graceful_shutdown.py`,
   `test_convergence.py`, `test_backend_drift.py`, `test_wonder_scope.py`) pass
   unmodified — new fields all default-valued, `refined_acs`/`questions` semantics
   unchanged.

## 6. Rollout

Single PR, default-on with `OUROBOROS_SCOPED_REEXECUTION=0` escape hatch. No event
schema changes (new fields ride inside existing `reflect_output`/`wonder` payloads via
pydantic defaults; old events replay fine because all new fields are optional).
