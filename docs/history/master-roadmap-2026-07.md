> [!WARNING]
> **Immutable historical snapshot — non-normative and superseded.**
>
> - Snapshot date: 2026-07-02
> - Source commit: [`715a0c63556d330c529085dfb83d066890703a72`](https://github.com/Q00/ouroboros/commit/715a0c63556d330c529085dfb83d066890703a72)
> - Repository baseline captured by the plan: `aa4aa20b` (v0.44.0)
> - Current sequencing SSOT: [GitHub issue #961](https://github.com/Q00/ouroboros/issues/961)
>
> The original work orders below are retained verbatim for traceability. They
> must not be executed or treated as current repository guidance.

## Final work-order dispositions

| Work order | Final disposition | Evidence |
| --- | --- | --- |
| PR-A | Landed | [#1540](https://github.com/Q00/ouroboros/pull/1540) deleted the confirmed dead code. |
| PR-B | Superseded / partially landed | [#1571](https://github.com/Q00/ouroboros/pull/1571) landed the valid JSONL and child-environment deduplication; the prompt-composition target was rejected because the implementations were not behaviorally identical. |
| PR-C | Landed | [#1543](https://github.com/Q00/ouroboros/pull/1543) emitted provider-neutral sequential subagent contracts. |
| PR-D | Landed | [#1547](https://github.com/Q00/ouroboros/pull/1547) added bounded automatic evaluation chaining after successful runs. |
| PR-E | Landed | [#1549](https://github.com/Q00/ouroboros/pull/1549) extracted the evidence helper library. |
| PR-F | Landed | [#1551](https://github.com/Q00/ouroboros/pull/1551) added structured acceptance criteria. |
| PR-G | Landed | [#1548](https://github.com/Q00/ouroboros/pull/1548) made the executor consume AC success contracts through verify-by-default gates and retries. |
| PR-H | Landed | [#1572](https://github.com/Q00/ouroboros/pull/1572) made evaluation judge declared AC contracts and enforce the reward-hacking veto. |
| PR-I | Landed | [#1574](https://github.com/Q00/ouroboros/pull/1574) split the interview authoring handler by action. |
| PR-J | Landed | [#1578](https://github.com/Q00/ouroboros/pull/1578) added the generic fan-out core and correlated result re-entry. |
| PR-K | Landed | [#1580](https://github.com/Q00/ouroboros/pull/1580) added the bounded interview fan-out injection points. |

# Master Roadmap 2026-07 — Dead Code → Provider Parity → Verifiable Loop → Interview Fan-out

> Status: APPROVED PLAN (2026-07-02). Baseline: `main` @ `aa4aa20b` (v0.44.0).
> Origin: 7-agent codebase audit (dead code sweep, refactor hotspots, interview anatomy,
> subagent infra, provider parity, AC verifiability, run→eval chaining).

---

## 0. How to execute this document (READ FIRST)

This document is a set of **independent work orders** (PR-A … PR-K plus a Phase 5 backlog).
Each work order is self-contained. Rules for the executor:

1. **One work order = one branch = one PR.** Branch naming: `roadmap/pr-a-dead-code`,
   `roadmap/pr-b-dup-kill`, etc. Never combine two work orders in one PR.
2. **This repo merges squash-only.** Do NOT stack branches on each other. Every branch
   starts from fresh `origin/main`.
3. **Line numbers in this document are anchors, not gospel.** They were captured at
   `aa4aa20b`. Before editing, ALWAYS locate the code by the symbol name given
   (grep for it). If a symbol cannot be found, STOP and report — do not guess.
4. **Preconditions are blocking.** Every work order lists preconditions. If one fails,
   STOP and report. Do not "fix it while you're there".
5. **Scope is a hard wall.** Only touch the files listed in the work order. No drive-by
   refactoring, no renaming, no formatting of untouched code, no comment sprinkling.
6. **Validation gate for every PR** (run from repo root):
   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run mypy src/
   uv run pytest tests/ --ignore=tests/e2e -q
   ```
   Known pre-existing failures that must NOT block you (do not attempt to fix them):
   opencode tests (`test_opencode_*`), e2e `test_run_workflow_verbose`,
   codex_cli_runtime profile tests. See `MEMORY`/test catalog. Everything else must pass.
7. **Every work order ends with "Done means".** All bullets must be true before opening
   the PR. Put the checklist verbatim into the PR description.

### Dependency map

```
PR-A (dead code)      ──┐
PR-B (dup kill)       ──┤  independent of each other, do first
                        │
PR-C (parity, Phase 1) ─┼─→ required before PR-I/J/K (Phase 4)
PR-D (run→eval, Phase 2)│   independent — can run parallel with PR-C
                        │
PR-E (evidence extract) ─→ required before PR-G
PR-F (AC schema)        ─→ required before PR-G, PR-H
PR-G (executor wiring)  ─→ requires E + F
PR-H (evaluator wiring) ─→ requires F (G recommended first)
                        │
PR-I (handler split)    ─→ requires C; required before PR-J/K
PR-J (fan-out core)     ─→ requires C + I
PR-K (fan-out inject)   ─→ requires J
```

Safe parallel tracks: (A, B) → then (C ∥ D ∥ E) → then (F ∥ I) → (G, H ∥ J) → K.

---

## Phase 0 — Ground clearing

### PR-A: Delete confirmed dead code

**Goal**: remove code proven dead by reverse-reference grep, nothing else.

**Context**: a dead-code sweep confirmed these have zero live importers. Live
functionality is elsewhere in each case.

**Preconditions** (verify each; if any fails, STOP):
```bash
# 1. These directories contain ONLY __pycache__ leftovers (deleted from git already):
git -C . ls-files src/ouroboros/execution/ src/ouroboros/openclaw/ src/ouroboros/secondary/
# → must output NOTHING

# 2. ac_tree_hud_render is imported only by its own tests:
grep -rn "ac_tree_hud_render" src/ --include='*.py'
# → must show ONLY the file itself (its own module line), no importers

# 3. workflow_display is not imported anywhere in src/:
grep -rn "workflow_display\|WorkflowDisplay\|render_workflow_state" src/ --include='*.py' | grep -v "cli/formatters/workflow_display.py"
# → must output NOTHING
```

**Steps**:
1. Delete stale bytecode dirs (not tracked by git, filesystem cleanup):
   `rm -rf src/ouroboros/execution src/ouroboros/openclaw src/ouroboros/secondary`
2. `git rm src/ouroboros/mcp/tools/ac_tree_hud_render.py`
3. `git rm tests/unit/mcp/test_ac_tree_hud_render_depth3.py tests/unit/mcp/test_ac_tree_hud_status_icons.py`
   (locate exact paths with `grep -rln "ac_tree_hud_render" tests/`)
4. `git rm src/ouroboros/cli/formatters/workflow_display.py`
5. Check `src/ouroboros/cli/formatters/__init__.py` — it exports only
   `console`/`OUROBOROS_THEME`; confirm it does NOT re-export anything from
   `workflow_display`. If it does, remove only that re-export line.

**Do NOT touch** (they look dead but are kept on purpose):
- `src/ouroboros/mcp/tools/subagent.py` — `synthesize_code_investigation_when_complete`,
  `synthesize_lateral_persona_panel_when_complete`,
  `continue_interview_after_lateral_persona_synthesis` and their tests
  → these are the planned re-entry layer for PR-J.
- `src/ouroboros/orchestrator/frugality_proof.py` → tied to an active Seed.
- `src/ouroboros/auto/auto_fill.py` → RFC #1256 substrate.
- `src/ouroboros/core/runtime_transition.py`, `src/ouroboros/core/hitl_resume.py`,
  `src/ouroboros/orchestrator/traceguard_benchmark_capture.py` → owner decision pending.

**Done means**:
- [ ] All three precondition greps returned clean before deletion
- [ ] Only the 4 tracked files above were deleted; `git status` shows nothing else
- [ ] Validation gate passes (Section 0, rule 6)

---

### PR-B: Kill byte-identical duplication (3 targets only)

**Goal**: consolidate three proven copy-paste sites into existing/new shared helpers.
No behavior change; pure deduplication.

**Target 1 — `_compose_prompt` triplication.**
Files: `src/ouroboros/providers/codex_cli_runtime.py` (~:337-358),
`src/ouroboros/providers/hermes_runtime.py` (~:314-335),
`src/ouroboros/providers/opencode_runtime.py` (~:414-450).
The `## System Instructions` fence + tool-list assembly is identical except docstrings.
1. Create `src/ouroboros/providers/prompt_compose.py` with one function
   `compose_cli_prompt(...)` — copy the body from `codex_cli_runtime.py` verbatim
   (it carries the `<system-directive>` fencing fix; preserve it exactly).
2. Replace all three `_compose_prompt` bodies with a delegation to it. Keep the
   method signatures on each runtime class unchanged.
3. Compare the three original bodies first. If any body differs by more than
   docstring/whitespace, STOP and report the diff instead of merging.

**Target 2 — JSONL event parsing quadruplication.**
Files: `codex_cli_runtime.py` (~:1185-1192), `opencode_runtime.py` (~:683-697),
`pi_runtime.py` (~:234-245), `gjc_runtime.py` (~:216-233).
The `json.loads → JSONDecodeError → isinstance(dict) → None` body is identical;
`_malformed_event_message` (240-char truncate) is byte-identical in gjc/pi.
1. Add `parse_json_event(line: str) -> dict | None` and
   `malformed_event_message(line: str) -> str` to the EXISTING module
   `src/ouroboros/providers/codex_cli_stream.py`.
2. Delegate all four sites. gjc raises where others return None — keep gjc's raise
   by wrapping (`if parsed is None: raise ...` at the call site), do NOT change
   gjc's error semantics.
3. Leave `gemini_cli_runtime.py` alone (it goes through a normalizer — out of scope).

**Target 3 — `_CHILD_ENV_STRIP_KEYS` sextuplication.**
Files: `opencode_adapter.py:72`, `gemini_cli_adapter.py:85`, `hermes_cli_adapter.py:37`,
`hermes_runtime.py:61`, `opencode_runtime.py:73`, `gemini_cli_runtime.py:65`
(all under `src/ouroboros/providers/`).
The tuple `("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND")` re-declares a value
that already exists as `DEFAULT_OUROBOROS_STRIP_KEYS` in the `child_env` module.
1. Verify equality first:
   `grep -rn "DEFAULT_OUROBOROS_STRIP_KEYS" src/ouroboros/ | head` and read the definition.
2. Replace each local constant with an import of the shared constant. If any local
   tuple contains EXTRA keys beyond the shared two, STOP and report — do not merge
   differing sets.

**Done means**:
- [ ] Zero behavior change (no test expectations edited)
- [ ] The three original `_compose_prompt` bodies were diffed and found identical (or STOP was reported)
- [ ] gjc still raises on malformed events (its tests prove it)
- [ ] Validation gate passes

---

## Phase 1 — Provider parity (ubiquitous language)

### PR-C: Every dispatch mode gets a first-class, in-band contract

**Goal**: today only HOST_DRIVEN (claude/codex) responses carry machine-readable fan-out
cues; SEQUENTIAL hosts (copilot/gemini/hermes/kiro/goose/pi/gjc/opencode-subprocess) get
payloads with no processing contract. After this PR, all three dispatch modes speak the
same vocabulary, from one source of truth.

**Vocabulary** (do not invent new terms — reuse these):
- Enums: `SubagentDispatchMode` {`host_driven`, `plugin_passive`, `sequential`} in
  `src/ouroboros/backends/capabilities.py` (~:66).
- Wire keys: `host_action`, `dispatch_mode`, `result_correlation_key`.
- New wire value introduced by this PR: `host_action="process_payloads_sequentially"`
  (sequential counterpart of `host_action="spawn_subagents"`).

**Step 1 — Symmetric stamping in the interview advisory path.**
File: `src/ouroboros/mcp/tools/authoring_handlers.py`, function
`_attach_question_assist_requests` (~:693). Today (~:738-743) it sets
`question_advisory_dispatch_mode="host_driven"`, `question_advisory_host_action="spawn_subagents"`,
`question_advisory_result_correlation_key="context.lane_id"` ONLY when the resolved
dispatch mode is HOST_DRIVEN.
Add the SEQUENTIAL branch: same three keys, values
`"sequential"` / `"process_payloads_sequentially"` / `"context.lane_id"` (same key!).
PLUGIN_PASSIVE branch stays as-is.

**Step 2 — Symmetric stamping in the lateral_think path.**
File: `src/ouroboros/mcp/tools/evaluation_handlers.py` (~:1596-1620). The SEQUENTIAL
branch currently emits `dispatch_mode="inline_fallback"` with payloads but no
`host_action` and no `result_correlation_key`. Change it to emit
`dispatch_mode="sequential"`, `host_action="process_payloads_sequentially"`,
`result_correlation_key="context.persona"`.
CAUTION: grep tests and skills for the literal `inline_fallback` first:
`grep -rn "inline_fallback" src/ tests/ skills/`. Update every consumer/assertion you
find in the same PR. If a consumer exists OUTSIDE this repo's control (skill markdown
shipped to plugins), keep emitting `inline_fallback` as an ADDITIONAL legacy alias key
rather than breaking it — decide based on what the grep shows and note it in the PR.

**Step 3 — Wire the dead contract as SSOT.**
`build_runtime_subagent_orchestration_contract` and
`RuntimeSubagentOrchestrationContract` (`backends/capabilities.py` ~:120 and ~:580)
are fully implemented but have ZERO consumers (verify:
`grep -rn "build_runtime_subagent_orchestration_contract" src/ | grep -v capabilities.py`).
In both stamping sites (Steps 1-2), fetch the contract for the active backend and emit
its `runtime_instruction_handling` text into meta (key:
`subagent_orchestration_instruction`) instead of hard-coding instruction prose.
The contract already contains correct per-mode text including sequential.

**Step 4 — Close the capability-guide gaps.**
File: `src/ouroboros/backends/capabilities.py`.
- `_GENERIC_SKILL_EXECUTION_CAPABILITIES` (~:262) has no `orchestrate_subagents`
  entry → copilot/gemini/hermes/kiro/pi/gjc render no orchestration guidance. Add one,
  sequential-first wording: "Process each payload in order. Correlate results by the
  key named in `result_correlation_key`. Synthesize after the last payload."
- goose has an EMPTY capability tuple (~:438-448) → assign it the generic set.

**Step 5 — De-Claude-ify skill prose.**
- `skills/interview/SKILL.md` ~:175 names Claude's Task/Agent tool as "that mechanism".
  Reword to lead with the abstract concept ("your runtime's native subagent mechanism;
  with none, process payloads sequentially per the `sequential` dispatch mode") keeping
  Claude/Codex as examples. The existing :200-208 sequential_fallback text stays.
- `skills/unstuck/SKILL.md` ~:142 header covers Claude+Codex only — add the explicit
  sequential branch mirroring interview's wording.

**Do NOT**: change `resolve_subagent_dispatch` logic, registry flags, or which backend
gets which mode. This PR changes what is EMITTED, never how modes are RESOLVED.

**Done means**:
- [ ] A SEQUENTIAL backend response for interview advisory carries all three
      `question_advisory_*` keys (add/extend a unit test asserting this)
- [ ] lateral_think sequential response carries `host_action` + `result_correlation_key`
      (test), and the `inline_fallback` grep decision is documented in the PR
- [ ] `build_runtime_subagent_orchestration_contract` has ≥1 non-test consumer
- [ ] goose renders a non-empty capability guide (test)
- [ ] Validation gate passes

---

## Phase 2 — run → evaluate guarantee

### PR-D: Formal evaluation auto-chains after `ooo run`

**Goal**: `ooo run` currently ends with `evaluated: false` and a prose pointer
(`next_step: "ooo evaluate <session_id>"`) that users ignore. After this PR, a
successful run automatically enqueues the formal 3-stage evaluation as a separate,
bounded background job — on every provider, without host involvement.

**Key facts** (verify before coding):
- Run completion paths stamp run-only metadata via `_run_only_verification_meta` /
  `_run_only_verification_text` in `src/ouroboros/mcp/tools/execution_handlers.py`
  (~:236-263, applied ~:842, ~:873, ~:1447-1459).
- The formal evaluator is `EvaluateHandler` / `StartEvaluateHandler` in
  `src/ouroboros/mcp/tools/evaluation_handlers.py` (~:430 / ~:1786). It requires
  `session_id` + `artifact`; optional `seed_content`, `working_dir`.
- At run completion inside `_run_in_background` (execution_handlers.py ~:684-779) all
  of those inputs already exist — the post-execution QA block (~:723-757) consumes
  the same data.
- `EvaluateHandler.TIMEOUT_SECONDS = 0` (evaluation_handlers.py ~:349) — the formal
  pipeline currently has NO server-side bound. Auto-chaining an unbounded job is
  forbidden; Step 3 fixes this.
- The auto pipeline's "EVALUATE" phase uses a qa-judge wrapper, NOT this handler.
  Do not touch `auto/` in this PR.

**Step 1 — Config flag.**
File: `src/ouroboros/config/models.py` (near `tui_autolaunch`, ~:203-208).
Add `auto_evaluate: bool = True` with a docstring: "When true, a successful
`execute_seed` run automatically enqueues formal evaluation as a background job."
Also add a per-call override parameter `auto_evaluate: bool | None = None` to the
execute-seed tool input schema (same place `skip_qa` is declared — grep `skip_qa`
in `execution_handlers.py` and mirror its plumbing exactly).

**Step 2 — Enqueue on terminal success.**
File: `src/ouroboros/mcp/tools/execution_handlers.py`.
Insertion point: in `StartExecuteSeedHandler`, after the run job's runner returns
successfully (`_runner`, ~:1400-1409) — NOT inside `_run_in_background` (a separate
job keeps the run job's terminal state clean and gives evaluate its own
cancel_key/process_id/terminal events).
Logic:
```
if run_succeeded and resolve_auto_evaluate(config_flag, per_call_override):
    enqueue evaluate via the same path StartEvaluateHandler uses
    (reuse its job-startup helper; pass session_id, artifact, seed_content,
     working_dir gathered from the run result)
    attach to the run receipt meta:
      chained_evaluate_job_id, verification_status="evaluation_enqueued"
else:
    keep today's meta exactly (evaluated:false, next_step prose)
```
Reuse `start_background_tool_job` (`src/ouroboros/mcp/tools/background.py` ~:74-166) —
do not hand-roll job management.

**Step 3 — Bound the chained evaluation.**
In `evaluation_handlers.py`, add a deadline for the auto-chained path (constructor
or call parameter, e.g. `deadline_seconds=1800` default, config-overridable). On
timeout: write a terminal event for the job (see the events table terminal-event
pattern used elsewhere — grep `terminal` in `background.py`/`job` modules) and
surface `evaluation_status="timed_out"`. NEVER let a hung evaluation strand the
session (this codebase has zombie-job history).

**Step 4 — Failure isolation.**
An evaluate job that fails or times out must NOT change the run's success status.
The receipt/meta reads: run succeeded, evaluation incomplete, retry with
`ooo evaluate <session_id>`.

**Step 5 — Skill surfacing.**
File: `skills/run/SKILL.md` step 10 (~:267-282) and breadcrumb footer (~:333-341).
When the run receipt contains `chained_evaluate_job_id`: instruct the host to poll
that job (`ouroboros_job_wait`/`job_status`) and render the evaluation verdict
(APPROVED / not approved + failed ACs) instead of printing "Next: ooo evaluate".
When the key is absent (flag off, older server): keep today's prose path verbatim.

**Do NOT**: touch `auto/pipeline.py`, the QA block semantics, or `EvaluateHandler`'s
3-stage internals beyond adding the deadline parameter.

**Done means**:
- [ ] With flag on (default): run success → evaluate job enqueued; receipt carries
      `chained_evaluate_job_id` (integration test with a stubbed evaluator)
- [ ] With flag off or per-call override false: byte-identical legacy meta (test)
- [ ] Evaluate timeout → terminal event written, run status unchanged (test)
- [ ] Evaluate failure → run status unchanged (test)
- [ ] SKILL.md renders verdict when the key exists, legacy prose when not
- [ ] Validation gate passes

---

## Phase 3 — Per-AC success contract (the spine)

### PR-E: Extract the evidence library out of parallel_executor

**Goal**: `src/ouroboros/orchestrator/parallel_executor.py` is ~6.3k lines; roughly
2,000 of them (~:245-2258 and ~:5972-6276) are PURE functions (no `self`) doing shell
parsing, test-success detection, file-claim matching, and typed-evidence validation.
Extract them into a package so PR-G can modify evidence logic without touching the
async orchestrator. **Pure move — zero behavior change.**

**Steps**:
1. Create `src/ouroboros/orchestrator/evidence/` package.
2. Identify every module-level function and constant in the two line ranges that does
   not reference `self`. Move them, grouped by theme (suggested modules:
   `shell_parsing.py`, `test_detection.py`, `claims.py`, `typed_evidence.py` — final
   split at executor's discretion, but no module >600 lines).
3. In `parallel_executor.py`, replace the moved code with imports. Keep the ORIGINAL
   names importable from `parallel_executor` via re-export if any test imports them
   from there (check: `grep -rn "from ouroboros.orchestrator.parallel_executor import" tests/ | head -50`).
4. No signature changes, no logic changes, no renames.

**Done means**:
- [ ] `wc -l src/ouroboros/orchestrator/parallel_executor.py` dropped by ≥1500
- [ ] Every moved function has identical body (`git diff` shows only moves/imports)
- [ ] All evidence-related tests pass unmodified (~80 tests reference evidence)
- [ ] Validation gate passes

---

### PR-F: Structured AC schema + authoring

**Goal**: today an acceptance criterion is a bare string
(`Seed.acceptance_criteria: tuple[str, ...]`, `src/ouroboros/core/seed.py` ~:256-259).
Introduce a structured per-AC spec that can declare what output counts as success,
while every existing Seed keeps loading unchanged.

**Design constraints (non-negotiable)**:
- The contract fields map onto the executor's EXISTING evidence vocabulary
  (`commands_run`, `files_touched`, `tests_passed` — see
  `_verify_atomic_evidence_against_runtime_messages`). Do NOT invent a 4th evidence
  category.
- Every field except `description` is OPTIONAL. A bare-string AC must remain valid
  forever (deterministic repair loops fill contracts; hard-requiring them would stall
  seed generation).

**Step 1 — Model.** In `src/ouroboros/core/seed.py`:
```python
class AcceptanceCriterionSpec(BaseModel):
    description: str
    verify_command: str | None = None        # command whose success proves the AC (→ commands_run / tests_passed)
    expected_artifacts: tuple[str, ...] = () # files/paths that must exist or change (→ files_touched)
    output_assertion: str | None = None      # substring/predicate expected in verify_command output
```
Change `acceptance_criteria` to `tuple[AcceptanceCriterionSpec, ...]` with a
`@field_validator(..., mode="before")` that coerces `str` items →
`{"description": <str>}`. Add a serializer decision: persisted format for a
spec-only-description AC should serialize back to the bare string (keeps existing
seed files diff-stable) — implement via `model_serializer` or a custom dump helper;
verify round-trip with a test: load old seed JSON → dump → identical.

**Step 2 — Ripple check.** Find every consumer of `acceptance_criteria`:
`grep -rn "acceptance_criteria" src/ --include='*.py' | grep -v test`.
Each consumer that treats items as `str` needs `.description` (or a small
`ac_text(item)` helper in `core/seed.py`). Do the mechanical update everywhere in
this PR; behavior stays identical because only `description` is populated so far.
`orchestrator/workflow_state.py` `AcceptanceCriterion` (~:329-410): add
`spec: AcceptanceCriterionSpec | None = None` field, populated where workflow state
is built from the Seed (grep for where `AcceptanceCriterion(` is constructed).
Do NOT use it yet — PR-G does.

**Step 3 — Authoring prompt.** `src/ouroboros/agents/seed-architect.md` (~:20-30,
:54-71): extend the AC output format so each criterion emits its contract:
```
AC: <description> | verify: <command or NONE> | artifacts: <comma-list or NONE> | expect: <output assertion or NONE>
```
and add 2 few-shot examples (one code AC with pytest command, one docs AC with
artifacts-only). Update the seed parser that consumes the architect's output
(grep for where architect output is parsed into `acceptance_criteria`) to fill the
spec fields; unparseable lines fall back to description-only. Never fail parsing
because contract fields are missing.

**Step 4 — Grade gate upgrade.** `src/ouroboros/auto/grading.py`:
`_is_observable` (~:479-498) currently checks whether AC PROSE mentions an observable.
New logic: an AC with a populated `verify_command` OR non-empty `expected_artifacts`
is observable, full stop; otherwise fall back to the existing prose heuristic and
additionally emit finding code `missing_success_contract` at MEDIUM severity (new
code — HIGH would hard-block legacy seeds; MEDIUM feeds `seed_repairer.py` (~:146)
so the repair loop starts filling contracts without blocking).

**Done means**:
- [ ] Old seed JSON files load and round-trip byte-identically (test with a real
      fixture from `tests/`)
- [ ] String AC and spec AC both validate; spec fields optional (tests)
- [ ] `grep -rn "acceptance_criteria" src/` shows no remaining consumer assuming `str`
- [ ] grading: AC with verify_command → observable; without → prose fallback +
      `missing_success_contract` MEDIUM finding (tests)
- [ ] Validation gate passes

---

### PR-G: Executor consumes the AC contract

**Goal**: the executor's evidence gate currently applies one per-profile schema to
every AC and infers everything at runtime. Make it READ the per-AC contract when
present. Requires PR-E and PR-F merged.

**Steps** (all in `src/ouroboros/orchestrator/`, mostly `parallel_executor.py` +
the new `evidence/` package):
1. `_effective_evidence_schema_for_ac` (~:395-413): today it only TRIMS the global
   schema via regex classifiers (`_is_documentation_only_ac`, `_is_validation_only_ac`).
   New behavior when `ac.spec` carries a contract: ADD to the required set —
   `verify_command` present → require `commands_run` (and `tests_passed` if the command
   is test-like; reuse the existing test-detection helper from `evidence/`);
   `expected_artifacts` non-empty → require `files_touched`.
   The regex-trim path remains the fallback for contract-less ACs. Additive only:
   a contract must never REMOVE a profile-required field.
2. Leaf prompt (`_build_atomic_dispatch_context`, ~:4898/4917): when a contract
   exists, append an explicit block:
   ```
   SUCCESS CONTRACT for this AC:
   - Run: <verify_command> and report it in commands_run
   - Expected artifacts: <list> — report them in files_touched
   - Expected output: <output_assertion>
   ```
3. Transcript verifier (`_verify_atomic_evidence_against_runtime_messages`,
   ~:6098-6200, now partly in `evidence/`): it already cross-checks the three field
   names against runtime messages. Extend: if the contract names a `verify_command`,
   the claimed `commands_run` must include it (substring match on the command);
   claimed-but-absent → existing `EVIDENCE_FORM_MISMATCH` path. If `output_assertion`
   is set and the command's transcript output does not contain it → evidence invalid
   with the same failure code. Reuse the existing failure taxonomy
   (`orchestrator/failure_taxonomy.py`) — no new codes.
4. Wire `AcceptanceCriterion.spec` (added in PR-F Step 2) through to wherever steps
   1-3 read the AC.

**Do NOT**: change behavior for contract-less ACs (the entire existing test suite is
the regression harness for that claim); touch retry/stall/heartbeat logic.

**Done means**:
- [ ] Contract-less AC: evidence schema and prompts byte-identical to before
      (assert via existing tests passing unmodified)
- [ ] AC with verify_command → `commands_run` required + command must appear in
      transcript-verified evidence (new tests, both pass and FABRICATION paths)
- [ ] AC with expected_artifacts → `files_touched` required (test)
- [ ] output_assertion mismatch → rejected with existing taxonomy code (test)
- [ ] Validation gate passes

---

### PR-H: Evaluator judges against the declared contract

**Goal**: Stage 2 (semantic) evaluation sees only the bare AC text today. Feed it the
contract, and stop letting `reward_hacking_risk` be write-only. Requires PR-F.

**Steps** (in `src/ouroboros/evaluation/`):
1. `models.py` `EvaluationContext` (~:302-325): add
   `current_ac_spec: AcceptanceCriterionSpec | None = None`. Populate it wherever
   `current_ac` is populated (grep `current_ac=`).
2. `semantic.py` prompt build (~:143-165): when a spec exists, render a
   "DECLARED SUCCESS CONTRACT" block (verify_command / expected_artifacts /
   output_assertion) and instruct the judge: "The AC passes ONLY if the artifact
   demonstrates the declared contract was met. Cite the evidence line."
3. Gate wiring, `pipeline.py` (~:242-244). Today:
   `final_approved = ac_compliance and score >= 0.8`. New:
   ```python
   final_approved = ac_compliance and score >= 0.8 and reward_hacking_risk < 0.7
   ```
   Threshold 0.7 as a module-level constant `REWARD_HACKING_VETO_THRESHOLD` with a
   comment that it only vetoes high-confidence gaming signals. When it vetoes, the
   result's failure reason must say so explicitly (surface in `ACCheckItem.failure_reason`).
4. `evaluation_handlers.py`: thread the seed's AC specs into the per-AC evaluation
   loop (grep how `acceptance_criteria` flows into `EvaluationContext` there).

**Done means**:
- [ ] Spec-carrying AC renders the contract block in the Stage 2 prompt (test on
      prompt construction, no live LLM)
- [ ] `reward_hacking_risk=0.9` with passing score → NOT approved, reason explains
      the veto (test)
- [ ] `reward_hacking_risk=0.0` → behavior identical to today (tests unmodified)
- [ ] Validation gate passes

---

## Phase 4 — Interview fan-out ("use subagents like crazy")

### PR-I: Split AuthoringHandlers.handle (prep)

**Goal**: `AuthoringHandlers.handle` is one 1,277-line method
(`src/ouroboros/mcp/tools/authoring_handlers.py` ~:1955-3231) mixing arg parsing,
action inference, plugin-vs-in-process branching, LLM calls, persistence, and
formatting. PR-J/K need to modify per-action logic; split first. **Pure structural
move — zero behavior change.**

**Steps**:
1. Extract per-action private methods: `_handle_start`, `_handle_answer`,
   `_handle_resume` (match the action names the dispatcher actually infers — read the
   method top to get the exact action set; do not guess).
2. `handle` keeps: arg parsing, action inference, session-id validation, client gate —
   then dispatches.
3. The plugin-passive vs in-process branch inside each action: extract into a small
   strategy call if trivial, otherwise keep inline per-action. Judgment call, but
   `handle` itself must end ≤150 lines.
4. No signature changes on `handle`; all 28 authoring tests pass unmodified.

**Done means**:
- [ ] `handle` ≤150 lines; per-action methods exist
- [ ] Zero test modifications; validation gate passes

---

### PR-J: Generic fan-out core + result re-entry tool

**Goal**: give ANY interview step a one-liner to declare "fan these N prompts out and
give me correlated results back". Two halves: a generic request builder (replacing two
bespoke copy-pasted producers) and a result re-entry MCP tool (reviving the dead
synthesis layer). Requires PR-C (vocabulary) and PR-I (handler shape).

**Step 1 — Generic builder.** In `src/ouroboros/mcp/tools/subagent.py`:
```python
def build_fanout_subagents(requests, correlation_key: str) -> list[SubagentPayload]
def stamp_fanout_meta(meta: dict, *, prefix: str, dispatch_mode, payloads, correlation_key) -> None
```
`SubagentPayload` (~:587) is already generic — reuse it as-is. `stamp_fanout_meta`
implements the three-mode stamping EXACTLY as PR-C standardized it (host_driven →
`spawn_subagents`; sequential → `process_payloads_sequentially`; plugin_passive →
`_subagents` envelope via `build_multi_subagent_result`). Then refactor BOTH existing
producers onto it: `_attach_question_assist_requests` (authoring_handlers.py ~:693)
and the lateral_think multi-persona emission (evaluation_handlers.py ~:1465-1633).
Their emitted meta keys must remain byte-identical (existing tests + PR-C tests prove it).

**Step 2 — Re-entry tool.** New MCP tool `ouroboros_submit_fanout_results`.
- Input: `session_id`, `correlation_key`, `results: [{key, content}, ...]`,
  `fanout_id` (stamped into meta by Step 1 so submissions match requests).
- Server side: validate every expected key is present (partial → return
  `status="partial"` listing missing keys; the host may resubmit); then dispatch to
  the REVIVED synthesizers in `subagent.py` —
  `synthesize_lateral_persona_panel_when_complete` (~:389),
  `continue_interview_after_lateral_persona_synthesis` (~:442),
  `synthesize_code_investigation_when_complete` (~:474). Read their existing tests
  first (`grep -rln "synthesize_lateral_persona_panel" tests/`) — the tests document
  the intended contract; adapt the tool to the functions, not vice versa.
- Register the tool wherever the other `ouroboros_*` tools register (grep
  `ouroboros_lateral_think` registration in `mcp/server/`).
- Update `skills/interview/SKILL.md`: after spawning advisory subagents, the host
  calls `ouroboros_submit_fanout_results` with correlated outputs, then continues with
  the tool's returned synthesis. Sequential hosts submit after processing payloads
  one-by-one — same tool, same contract.

**Done means**:
- [ ] Both legacy producers emit byte-identical meta through the shared helpers (tests)
- [ ] Submit tool: complete set → synthesis returned; partial set → `partial` +
      missing keys; unknown fanout_id → clean error (tests)
- [ ] Dead synthesizers now have non-test consumers
- [ ] Validation gate passes

---

### PR-K: Fan-out injection points

**Goal**: parallelize the interview's serial spine. Requires PR-J. Ship as one PR with
three independent commits (or split into K1/K2/K3 if any proves risky).

**K1 — Ambiguity scoring panel.** `src/ouroboros/bigbang/ambiguity.py`:
`AmbiguityScorer.score` (~:297) makes ONE LLM call (~:382) scoring all 3-4 dimensions
(scope/constraints/outputs[/brownfield]) in one prompt. Split: one scoring request per
dimension (per-dimension rubric extracted from the current combined prompt), fan out
via `build_fanout_subagents(correlation_key="context.dimension")` on the MCP path;
in-process concurrency (`asyncio.gather`, mirroring `_refine_answer`'s pattern in
`auto/interview_driver.py` ~:1560) on the auto path. Deterministic aggregation:
weighted average with the SAME weights as today (find them in the combined prompt or
scorer code — they must not silently change). The deterministic floor
(`auto/grading.py::deterministic_floor` clamp) is untouched.

**K2 — Question candidate panel.** `src/ouroboros/bigbang/interview.py`:
`ask_next_question` single call (~:507) → three persona candidates (contrarian /
researcher / architect — reuse persona prompts from the lateral system,
`orchestrator/capabilities.py` persona metadata) + deterministic selection: pick the
candidate targeting the dimension with the WORST current ambiguity score (from K1's
per-dimension output); tie → priority order contrarian > architect > researcher.
No LLM judge for selection — selection must be deterministic and testable.

**K3 — Seed-closer tri-panel.** The closer acceptance gate
(`skills/interview/SKILL.md` step 8 + `_load_seed_closer_summary`, subagent.py ~:892)
is single-pass. Make it a 3-lane fan-out (closer + contrarian + gap-hunter lanes,
correlation_key `context.lane_id`) through PR-J's builder; synthesis: closer verdict
gates, contrarian/gap-hunter findings append as blocking questions when severity is
high. This is mostly skill-markdown + one payload builder — smallest of the three.

**Explicitly deferred** (do NOT attempt in this PR): auto-path advisory fan-out —
requires the `_run_inner` split (refactor #7, Phase 5) first.

**Done means**:
- [ ] K1: per-dimension scores aggregate to the same weighted formula (unit test with
      stubbed per-dimension scores); interview converges in the auto e2e smoke test
- [ ] K2: selection is deterministic (test: fixed candidate set + fixed scores →
      fixed pick); ambiguity does not regress on the interview fixture
- [ ] K3: closer gate fires with three lanes; a HIGH gap-hunter finding blocks (test)
- [ ] Validation gate passes

---

## Phase 5 — Structural refactor backlog (continuous track)

Lower urgency; each is an independent PR, executable between phases. Order by
(impact × safety). All are PURE refactors — zero behavior change, tests unmodified.

| # | Target | Cut | Caution |
|---|--------|-----|---------|
| R1 | Twin backend factories: `providers/factory.py` ~:168-289 + `orchestrator/runtime_factory.py` ~:60-182 — parallel if/elif over the same ~11 backend strings | Single registry table `{backend: (adapter_factory, runtime_factory)}`; both factories read it | New-backend tests (61 factory-referencing tests) |
| R2 | `orchestrator/capabilities.py` (4,066 lines): tool specs + lateral personas + interview JSON schemas mixed | Split into `capabilities/{tool_specs,lateral_personas,interview_schemas}.py`, re-export from package `__init__` | Name-collides with `backends/capabilities.py` — never merge the two |
| R3 | `AutoPipeline.run` (auto/pipeline.py ~:480-1683, 1,204 lines) | Per-phase handlers following the existing `_run_evaluate`/`_run_lateral` pattern | 84 pipeline tests are the harness; watchdog/resume semantics must not shift |
| R4 | `_execute_atomic_ac` (parallel_executor.py ~:5232-5971, 740 lines) | Extract `AtomicPromptBuilder` (~:5260-5435) + `LeafDispatcher.stream()` (~:5563-5749) | stall/heartbeat timing is subtle; do AFTER PR-G settles |
| R5 | Runtime-handle lifecycle (parallel_executor.py ~:2615-3557, 15 interleaved methods) | `ACRuntimeHandleManager` collaborator owning `_ac_runtime_handles` | retry/resume tests |
| R6 | Event emission scatter (~12 methods + inline `BaseEvent` at ~:5540/5700/5716/5732) | `ExecutionEventEmitter` with typed `emit_*` | TUI depends on event FIELD NAMES — contract tests first |
| R7 | `AutoInterviewDriver._run_inner` (auto/interview_driver.py ~:609-1260, 652 lines) | Extract `_run_one_round()` + separate termination gate | Unblocks auto-path advisory fan-out (deferred from PR-K) |
| R8 | CLI stream-reader 5× duplication (`_iter_stream_lines` across codex/opencode/hermes/pi/gjc runtimes) | `RuntimeStreamMixin` in `providers/codex_cli_stream.py` | gjc's is a real inline reimplementation, not a delegator — diff carefully |
| R9 | Transient-error patterns copy-paste (codex/copilot/gemini/opencode adapters + claude_code/orchestrator variants) | `core/retry.py`: `BASE_TRANSIENT_PATTERNS` + `is_transient_error(msg, extra=())`; adapters pass only their extras | Per-adapter EXTRA patterns must be preserved exactly — no flat merge |

---

## Open decisions (owner, not executor)

1. **Scaffold trio**: `core/runtime_transition.py`, `core/hitl_resume.py`,
   `orchestrator/traceguard_benchmark_capture.py` — delete or schedule wiring?
   Default: keep until decided.
2. **`auto_evaluate` default**: plan says ON. Flip to OFF only if chained-evaluate
   token cost proves unacceptable in practice.
3. **`REWARD_HACKING_VETO_THRESHOLD`** (PR-H): starts at 0.7; revisit after observing
   real distributions.
4. **`inline_fallback` legacy alias** (PR-C Step 2): decision depends on what the
   grep reveals about external consumers.
