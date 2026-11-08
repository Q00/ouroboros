> [!WARNING]
> **Immutable historical snapshot — non-normative and superseded.**
>
> - Snapshot date: 2026-07 (from the file name)
> - Source commit: [`715a0c63556d330c529085dfb83d066890703a72`](https://github.com/Q00/ouroboros/commit/715a0c63556d330c529085dfb83d066890703a72)
> - Repository baseline captured by the handoff: `a752b70e` (after #1590 and #1591)
>
> The original handoff below is retained verbatim for traceability. Its work
> orders must not be executed or treated as current repository guidance.

# HANDOFF — Finish grounding contract-AC evaluation so `ooo auto` completes on codex

> Audience: a fresh implementer (Codex or any agent). Self-contained. Read fully before coding.
> Baseline: `main` @ `a752b70e` (after PR #1590 + #1591 merged). Repo: `ouroboros_evolve`.
> Global CLI note: `which ouroboros` may point at an OLD worktree. To test current code use
> `uv run --project /Users/jaegyu.lee/Project/ouroboros_evolve ouroboros ...` and make sure the
> working tree is pulled to the latest `main` first (`git -C <repo> pull origin main`).

---

## 0. Why this exists (context you must not skip)

`ooo run` (hand-written seed with a `verify_command`) now works 1/1 on merged `main` — verified live
(session `orch_94faa6787990`). But a live `ooo auto` on the **codex** backend still FAILS to build a
verified product. Root causes are TWO, both narrow, both a natural completion of the architecture
pivot PR #1591 already started.

**The pivot (already done in #1591):** for a *contract-carrying AC* (an AC whose spec declares a
`verify_command`), the fat-harness stopped reconstructing `tests_passed` from the worker transcript
and instead **delegates to the orchestrator's own execution** — `_run_ac_verify_gate` runs the
declared `verify_command` itself (real subprocess, real exit code + `output_assertion`). Authoritative
observation beats transcript reconstruction. Your job is to finish that idea for the two remaining
evidence surfaces that still block `auto`.

**KEY PRINCIPLE (apply it to both work orders):** for a contract AC, grade it from what the
orchestrator *observes*, never from what the worker *claims* in the transcript. The orchestrator
already has the authoritative checks — you are wiring them in, not inventing them.

---

## 1. Diagnosis (grounded in code — verify each anchor by symbol, line numbers are stale)

Live failure evidence from `ooo auto` (codex, execution `exec_9695144e0246`):
```
execution.session.failed :: Fat-harness verifier failed (unsupported evidence claims:
    files_touched: hello.py; commands_run: python -c "...")
execution.session.failed :: unsupported evidence claims: files_touched: ./hello.py;
    files_touched: ./test_hello.py; commands_run: 0; commands_run: ./
execution.verify.failed :: output_assertion 'exit code 0' not found in verify_command output
```
The produced `hello.py`/`test_hello.py` were CORRECT (`greet('World') == 'Hello, World'`). So both
failures are false negatives from evidence handling, not real defects in the worker's output.

**Confirmed facts (already checked in the codebase):**
- `src/ouroboros/orchestrator/parallel_executor.py`
  - `_missing_expected_artifacts(spec.expected_artifacts, cwd)` — the verify-gate ALREADY checks the
    real filesystem for the AC's `expected_artifacts` (anchor: search `_missing_expected_artifacts`,
    it is called inside the verify gate near the `verify_command` run).
  - `if spec.output_assertion and spec.output_assertion not in combined:` — the `output_assertion`
    check is a pure substring match against the command's stdout+stderr (anchor: search
    `not found in verify_command output`).
- `src/ouroboros/orchestrator/evidence/ac_classification.py`
  - `_effective_evidence_schema_for_ac(..., has_success_contract=...)` drops `tests_passed` from the
    required-evidence set when `has_success_contract` (anchor: search `has_success_contract and
    "tests_passed" in schema.required`). It does **NOT** drop `files_touched` for contract ACs — only
    for `_is_validation_only_ac`. THIS is why codex's command-text file writes get rejected.

---

## 2. WORK ORDER 1 — delegate `files_touched` to the filesystem for contract ACs

**Goal:** when a contract AC declares `expected_artifacts`, stop requiring `files_touched` from the
worker transcript. The verify-gate's `_missing_expected_artifacts` (a real filesystem existence check)
is the authoritative oracle — so it no longer matters whether codex wrote the file via a structured
Edit event or a bash heredoc / `apply_patch`. This is the exact same move #1591 made for `tests_passed`,
and it is **strictly stronger** than transcript reconstruction.

**Branch:** `fix/auto-contract-files-touched-delegation` off fresh `origin/main`.

**Steps:**
1. In `src/ouroboros/orchestrator/evidence/ac_classification.py`,
   `_effective_evidence_schema_for_ac`: add a parameter carrying whether the AC's spec declares
   non-empty `expected_artifacts` (e.g. `has_expected_artifacts: bool = False`). When
   `has_success_contract AND has_expected_artifacts` and `"files_touched" in schema.required`, drop
   `files_touched` from required evidence (mirror the existing `tests_passed` drop right below it).
   - Do NOT drop `files_touched` when there are no `expected_artifacts` — without a declared artifact
     there is no filesystem oracle to delegate to; keep today's behavior for that sub-case.
2. Thread `has_expected_artifacts` from the caller(s). PR #1591 already threads
   `has_success_contract = bool(ac_spec.verify_command)` through
   `_observe_atomic_typed_evidence` / `_run_atomic_verifier_pass` /
   `_emit_atomic_typed_evidence_event` and the module verifier in
   `src/ouroboros/orchestrator/evidence/` + `parallel_executor.py`. Follow that exact thread and add
   `has_expected_artifacts = bool(ac_spec.expected_artifacts)` alongside it. Grep
   `has_success_contract` across `src/ouroboros/orchestrator/` and mirror every call site.
3. Confirm the ordering guarantee still holds: dropping `files_touched` from required evidence must let
   a correct contract AC reach `result.success = True` so `_apply_verify_gate` runs
   `_missing_expected_artifacts` — and that gate must still FAIL the AC when a declared artifact is
   genuinely absent. Verify in code; do not assume.

**Do NOT:**
- Change behavior for legacy ACs (no `verify_command`) or contract ACs without `expected_artifacts`.
- Touch the `files_touched` path-normalization / workspace-scoping logic in `claims.py` (that stays —
  it's correct and still used by legacy ACs and the `expected_artifacts`-less contract sub-case).
- Weaken `_missing_expected_artifacts` — it is the oracle now; it must still reject missing artifacts.

**Tests (add; do not modify existing):**
- contract AC with `expected_artifacts` present on disk + worker emits NO `files_touched` (or malformed
  like `commands_run: 0`) → AC PASSES (evidence not required; gate's filesystem check is authoritative).
- contract AC with `expected_artifacts` but the artifact is MISSING on disk → AC FAILS via the gate
  (proves the oracle still enforces).
- legacy AC (no verify_command) → `files_touched` still required (unchanged).

**Done means:** `files_touched` dropped for `has_success_contract AND has_expected_artifacts`; the
filesystem oracle enforces artifact existence; legacy + no-artifact behavior byte-identical; validation
gate green.

---

## 3. WORK ORDER 2 — make `output_assertion` mean "literal stdout", and stop the "exit code 0" trap

**Goal:** `output_assertion` is defined as a literal substring expected in the command's stdout, but
models (the seed-architect) naturally write a *condition description* like `"exit code 0"`, which never
appears in stdout → the verify-gate can never satisfy it. Exit-code-0 success is ALREADY the primary
gate (`verify_command` must exit 0), so an exit-code `output_assertion` is both redundant and
unsatisfiable. Fix the semantics at the source (authoring) plus a deterministic parser safety net.

**Branch:** `fix/output-assertion-literal-semantics` off fresh `origin/main`.

**Steps:**
1. **Authoring prompt** — `src/ouroboros/agents/seed-architect.md`: in the AC output-format section
   (search for `output_assertion` / `expect:`), state explicitly that `output_assertion` is ONLY a
   literal string that will appear VERBATIM in the command's stdout (e.g. `OK`, `5 passed`). It must
   NEVER be a condition/exit-code/status description (`exit code 0`, `exit 0`, `returns 0`, `success`,
   `no errors`, `passes`). If there is no distinctive stdout to assert, emit NONE — exit-code-0 is
   already verified separately. Update the few-shot examples accordingly (keep one code AC that uses a
   real literal like `OK`, one docs AC with NONE).
2. **Parser normalization (the safety net)** — find where the architect's output is parsed into an
   `AcceptanceCriterionSpec.output_assertion` (grep where `output_assertion` is assigned from parsed
   text; and check the seed loader / `core/seed.py` coercion path). Add a deterministic normalizer that
   treats exit-code / bare-success phrases as NO assertion (set to `None`): case-insensitive match of
   patterns like `^exit\s*(code|status)?\s*0$`, `^returns?\s*0$`, `^exit\s*0$`, and bare
   `^(success|succeeds|passed|passes|ok exit|no errors?)$`. Anything with genuine distinctive text
   (`OK`, `5 passed`, `Hello, x`) is preserved. This makes existing/legacy bad assertions non-fatal
   without a re-author.
   - Be conservative: only strip clear exit-code/success-condition phrases. Do NOT strip a real literal
     just because it contains the word "pass" as part of larger text (e.g. `5 passed` must survive).
3. Leave the verify-gate substring check (`parallel_executor.py`) AS-IS — it is correct once the
   assertion is genuinely a literal. Do not make the gate "interpret" intent.

**Do NOT:**
- Make `output_assertion` required. It stays optional (PR-F contract: every field except `description`
  is optional).
- Change how `verify_command` exit-code success is judged (that's the real gate; untouched).

**Tests (add):**
- `output_assertion: "exit code 0"` → normalized to None (no assertion enforced); a passing
  verify_command (exit 0) → AC passes.
- `output_assertion: "OK"` and stdout contains `OK` → enforced and passes; stdout lacks `OK` → fails.
- `output_assertion: "5 passed"` survives normalization (not stripped).
- seed round-trip with the normalized/omitted assertion stays diff-stable (existing seed round-trip
  tests must still pass).

**Done means:** exit-code/success-phrase assertions no longer strand a correct run; genuine literal
assertions still enforce; seed-architect prompt emits literal-only assertions; validation gate green.

---

## 4. Validation gate (run from repo root, BOTH PRs)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src/
uv run pytest tests/ --ignore=tests/e2e --ignore=tests/unit/mcp --ignore=tests/integration/mcp -q
```
Run the specific touched-area test files explicitly and report counts:
- WO-1: `tests/unit/orchestrator/` (evidence/verify/ac_classification files).
- WO-2: `tests/unit/` seed + evaluation + seed-architect parsing tests.

**SAFETY (hard rule):** NEVER run the full `tests/unit/mcp` or `tests/integration/mcp` directories — a
prior full run leaked a live server and deleted sibling worktrees. Run specific FILES only.

**Known pre-existing failures — do NOT try to fix, they don't block you:** opencode tests
(`test_opencode_*`), `codex_cli_runtime` profile tests, e2e `test_run_workflow_verbose`,
`test_surface` / `test_auto_runtime_dispatch` codex-config-leak (6 failures; confirm identical on clean
`origin/main` via `git stash`). Also `test_runner.py::test_execute_seed_success` hangs in this sandbox
on unmodified code (spawns a real runtime) — skip it, don't diagnose.

---

## 5. MANDATORY live proof (this is the acceptance gate, not optional)

Both work orders exist to make `ooo auto` complete on codex. After BOTH are merged, prove it end-to-end
with the real codex backend (do NOT touch `~/.ouroboros` config):

```bash
D=$(mktemp -d) && cd "$D"
uv run --project /Users/jaegyu.lee/Project/ouroboros_evolve ooo auto \
  "Create hello.py with greet(name) returning 'Hello, <name>', plus a pytest test that passes." \
  --max-interview-rounds 15 --timeout 1200
```
Acceptance: the final status is a genuine COMPLETE product (the AC passes via the orchestrator's
verify-gate), NOT `failed` with `unsupported evidence claims` or `output_assertion ... not found`.
Inspect the execution session in the event store to confirm `execution.session.completed` /
`execution.ac.completed` with no `Fat-harness verifier failed (files_touched...)` and no
`output_assertion 'exit code 0' not found`. Run it TWICE to confirm it is not flaky.

Also keep the single-AC `ooo run` regression green (the release gate that already works):
```bash
D=$(mktemp -d) && cd "$D" && cat > seed.yaml <<'EOF'
goal: Create a tiny greeting module
acceptance_criteria:
  - description: hello.py defines greet(name) returning the string Hello, <name>
    verify_command: python3 -c "from hello import greet; assert greet('x')=='Hello, x'; print('OK')"
    expected_artifacts: [hello.py]
    output_assertion: OK
ontology_schema: {name: Greeting, description: Minimal greeting domain}
metadata: {ambiguity_score: 0.1}
EOF
uv run --project /Users/jaegyu.lee/Project/ouroboros_evolve ooo run workflow seed.yaml   # must be 1/1
```

**Cleanup after runs:** remove your temp dirs; kill any leaked `opencode run` / `codex exec` workers
you spawned (they orphan to PPID 1) and any `dashboard_web --serve-daemon` the auto run auto-starts.
Do NOT touch sibling worktrees or the user's long-running `ouroboros mcp serve` processes.

---

## 6. Merge protocol (non-negotiable)

- One work order = one branch = one PR. This repo is SQUASH-ONLY; never stack branches; each starts
  from fresh `origin/main`.
- End every commit message with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- End every PR body with:
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
- **DO NOT self-merge on green CI.** Merge ONLY after the `ouroboros-agent[bot]` review returns
  APPROVED. On CHANGES_REQUESTED: fix on the same branch, push, post a short explanatory comment to
  trigger re-review, repeat until APPROVED. (This session learned the hard way — CI green ≠ correct;
  the bot catches real defects.)
- If a PR touches `src/ouroboros/auto/`, the PR body MUST include the `## R-run comparison` section
  (`.github/PULL_REQUEST_TEMPLATE.md`): all cells `N/A | N/A | n/a`. CI parser gotcha: do NOT put the
  metric-token phrases ("Rounds completed", "Per-round wall-clock", "Terminal reason", "EventStore
  event count") anywhere in prose above the table — the parser grabs the first line containing each
  token. After editing the body, the perf-budget gate reads a STALE event on `gh run rerun`; push an
  empty commit to retrigger.

---

## 7. Why this is the elegant end-state (keep it in mind, don't over-build)

After both work orders, a contract AC is graded ENTIRELY by the orchestrator's authoritative
observation: `verify_command` execution (exit code + literal `output_assertion`) and
`expected_artifacts` filesystem existence. Fragile transcript-evidence reconstruction remains only for
legacy (contract-less) ACs. Because `ooo auto` always generates contract ACs, this makes auto
backend-agnostic (codex's heredoc/`apply_patch` writes and any malformed typed evidence become
irrelevant). Do not add new evidence-matching machinery — you are REMOVING transcript dependence and
leaning on checks that already exist.
