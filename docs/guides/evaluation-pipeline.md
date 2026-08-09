<!--
doc_metadata:
  runtime_scope: [all]
-->

# Evaluation Pipeline Guide

Ouroboros Phase 4 runs every execution result through a **three-stage progressive evaluation pipeline** before assigning formal acceptance-criterion (AC) verdicts. Cheaper checks gate the expensive ones: Stage 1 is free, Stage 2 uses one LLM call, and Stage 3 (multi-model consensus) runs only when specifically triggered.

> **Terminology boundary:** worker task completion is not a formal AC verdict, and task failure is not semantic drift. See [Execution vs. Evaluation Contract](./execution-vs-evaluation.md) for the shared `TaskResult` / `ACResult` distinction.

```
Artifact ready
      │
      ▼
┌─────────────────────────────┐
│  Stage 1: Mechanical ($0)   │ lint / build / test / static / coverage
│  All checks must pass       │
└────────────┬────────────────┘
             │ passed
             ▼
┌─────────────────────────────┐
│  Stage 2: Semantic ($$)     │ LLM evaluates AC compliance, goal
│  score ≥ 0.8 + ac_compliance│ alignment, drift, uncertainty
└────────────┬────────────────┘
             │ passed
             ▼
        ┌────┴────┐
        │ Trigger │ ← 7 conditions checked
        │ matrix  │
        └────┬────┘
             │ triggered?
        ┌────┴────────────────────────────┐
       YES                               NO
        │                                 │
        ▼                                 ▼
┌───────────────────────┐          ┌───────────────┐
│  Stage 3: Consensus   │          │   APPROVED    │
│  ($$$, 2/3 majority)  │          └───────────────┘
└───────────┬───────────┘
            │
   ┌────────┴────────┐
  YES               NO
   │                 │
   ▼                 ▼
APPROVED          REJECTED
```

> The diagram shows the happy path. Two things sit outside it: `trigger_consensus=true` jumps straight to Stage 3 from either the trigger matrix or a Stage 2 AC failure, and the [reward-hacking veto](#reward-hacking-veto) can flip any `APPROVED` above back to `REJECTED`.

---

## Stage 1: Mechanical Verification

The mechanical verifier runs zero-cost automated shell commands and checks the exit codes. It does **not** call any LLM.

### Checks

| Check | What it runs | Failure condition |
|-------|-------------|-------------------|
| `lint` | `lint_command` in config | Non-zero exit code |
| `build` | `build_command` in config | Non-zero exit code |
| `test` | `test_command` in config | Non-zero exit code |
| `static` | `static_command` in config | Non-zero exit code |
| `coverage` | `coverage_command` in config | Exit code != 0, OR parsed coverage < `coverage_threshold` (default **70%**) |

**Pipeline behavior:** If **any** check fails, Stage 2 and Stage 3 are skipped entirely and the artifact is rejected immediately.

**Skipped checks:** If a check has no command configured (`None`), it is silently skipped and treated as **passed**. This is the default when you have not set commands in `PipelineConfig.mechanical`.

### Stage 1 Failure Modes

| Failure mode | Symptom | Cause |
|---|---|---|
| **Command not found** | `Check <type> failed` with "Command not found" | Binary missing from PATH; check your environment |
| **Command timeout** | `Check <type> timed out after Ns` | Command exceeded `timeout_seconds` (default 300 s); increase timeout or fix slow tests |
| **Non-zero exit code** | `Check <type> failed (exit code N)` | Tool found real errors; inspect `stdout_preview`/`stderr_preview` in the event payload |
| **Coverage below threshold** | `Coverage X.X% below threshold Y.Y%` | Test suite does not meet the minimum coverage requirement; add tests or lower `coverage_threshold` |
| **Coverage not parseable** | Coverage check passes but no `coverage_score` in events | Output did not match the expected pattern (`TOTAL ... XX%`); ensure `pytest-cov` or compatible tool is used |
| **OS error** | `Check <type> failed` with "OS error" | Permissions problem or missing working directory; verify `working_dir` config |

### Where Stage 1 Commands Come From

Ouroboros does **not** ship hardcoded per-language presets. Stage 1 trusts exactly one source: `.ouroboros/mechanical.toml` in the project root. `build_mechanical_config(working_dir)` is the deterministic reader for that file — when the file is absent, every command resolves to `None` and Stage 1 skips gracefully rather than running the wrong tool.

The file is written by `ouroboros.evaluation.detector`, which makes **one AI call** that inspects the project's manifest files (`pyproject.toml`, `uv.lock`, `package.json`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `Makefile`, `justfile`, `Taskfile.yml`, `build.zig`, `CMakeLists.txt`, `mix.exs`, `Gemfile`, and others) and proposes commands for this specific repository. Each proposed command is validated before it is persisted — against the executable allowlist, against shell-operator and absolute-path injection, and against the repo itself (for example, a `cargo` command is only kept when `Cargo.toml` exists) — so the toml contains only safe, repository-supported commands.

Validation stops there. `_command_is_valid()` deliberately does not consult the host `PATH` (`evaluation/detector.py:496`), so it proves the command is safe and that the repository declares it, not that the executable is installed here. A manifest-declared `pytest` command is persisted even when `pytest` is missing from the environment, and Stage 1 reports "Command not found" at run time.

Generate or refresh it explicitly:

```bash
ouroboros detect              # inspect the current directory and write .ouroboros/mechanical.toml
ouroboros detect --force      # re-detect and overwrite an existing file
ouroboros detect --backend codex   # use a specific LLM backend for the detect call
```

`ensure_mechanical_toml()` is idempotent: when the file already exists and `force` is false it returns immediately without an LLM call. It never raises — on any failure it returns `False`, leaving Stage 1 with no commands.

> **If Stage 1 always passes, this is usually why.** With no `.ouroboros/mechanical.toml` and no explicitly configured `MechanicalConfig` commands, all five checks are skipped and treated as passed, which makes Stage 1 a no-op gate.
>
> Reaching that state through `ouroboros run` or the MCP evaluation path takes a failure, not just a missing file. Both author the file before reading it, by different routes: `run` calls `build_verification_artifacts()`, which invokes `_auto_detect_mechanical_toml()` (`cli/commands/run.py:785`, `evaluation/verification_artifacts.py:413`), while the MCP evaluation handler checks `has_mechanical_toml()` and calls `ensure_mechanical_toml()` followed by `build_mechanical_config()` directly (`mcp/tools/evaluation_handlers.py:724`). The auto-detection behavior is the same; the call path is not. Stage 1 ends up empty when that best-effort detection fails — no LLM adapter, a provider error, an unwritable `.ouroboros/` — or when a caller drives the lower-level pipeline directly and skips detection. If you see the no-op symptom, look for a failed detection before running `ouroboros detect` by hand.

> **Deprecated:** `detect_language()` no longer detects anything. It is a compatibility shim that reads `.ouroboros/mechanical.toml` and emits a `DeprecationWarning`; call `ensure_mechanical_toml()` plus `build_mechanical_config()` instead.

> **Go coverage note:** The `go test -cover` output format (`ok  ./... coverage: XX.X% of statements`) is not matched by the coverage parser (which expects `TOTAL ... XX%` or `Coverage: XX%`). For Go projects, `coverage_score` will always be `None` in the event payload and the coverage **threshold check is skipped even if coverage is low**. Use the `.ouroboros/mechanical.toml` override to supply a custom coverage command if you need threshold enforcement on Go projects.

### Project-Level Command Overrides

`.ouroboros/mechanical.toml` in your project root is where Stage 1 commands live. The detector writes it, and you can edit it or author it by hand without modifying Ouroboros configuration:

```toml
# .ouroboros/mechanical.toml
lint = "ruff check src/"
test = "pytest tests/unit -q"
coverage = "pytest --cov=src --cov-report=term-missing tests/"
coverage_threshold = 0.85
timeout = 120
```

**Override priority** (highest to lowest):
1. Explicit `overrides` dict passed programmatically (from MCP params)
2. `.ouroboros/mechanical.toml` in the project root
3. All `None` (all checks skip gracefully)

**TOML parse errors** are logged as a warning (`mechanical.toml_parse_error`) and silently ignored. There is no preset to fall back to, so every command stays `None` and Stage 1 skips all checks.

**Security: executable allowlist.** Commands in `.ouroboros/mechanical.toml` may only use executables from a built-in allowlist (e.g., `pytest`, `ruff`, `cargo`, `go`, `npm`, `make`). If a command specifies an executable not in the allowlist — or uses shell operators or an absolute path — it is silently blocked (logged as `mechanical.blocked_executable`) and the check is skipped. Commands passed programmatically through `MechanicalConfig` or the MCP `overrides` dict bypass this check, because they are vetted upstream. This prevents untrusted repository configs from running arbitrary commands in CI/CD environments.

| Override failure mode | Symptom | Cause / Action |
|---|---|---|
| **TOML parse error** | All Stage 1 checks skipped; no error raised | Malformed `.ouroboros/mechanical.toml`; check TOML syntax |
| **Blocked executable** | Check silently skipped | Executable not in allowlist; use an allowed tool or set the command in `MechanicalConfig` directly |
| **Auto-detection failed** | All Stage 1 checks skipped | `run` and the MCP path author the toml automatically; an empty Stage 1 means that attempt failed (no LLM adapter, provider error, unwritable `.ouroboros/`). Fix the cause, or run `ouroboros detect` / set commands explicitly in `MechanicalConfig` |
| **No toml present, detection bypassed** | All Stage 1 checks skipped | A caller drove the lower-level pipeline directly instead of `build_verification_artifacts()`; run `ouroboros detect`, or set commands explicitly in `MechanicalConfig` |

### Stage 1 Configuration

```yaml
# In PipelineConfig.mechanical (MechanicalConfig)
mechanical:
  coverage_threshold: 0.7           # 70% minimum (NFR9); lower for legacy projects
  timeout_seconds: 300              # Per-command timeout in seconds
  working_dir: /path/to/project     # Defaults to process cwd if omitted
  lint_command: ["ruff", "check", "."]
  build_command: ["python", "-m", "build"]
  test_command: ["pytest", "tests/"]
  static_command: ["mypy", "src/"]
  coverage_command: ["pytest", "--cov=src", "--cov-report=term-missing", "tests/"]
```

> **Important:** These fields are the programmatic escape hatch. In the normal `ouroboros run` path the commands come from `.ouroboros/mechanical.toml`, which that path authors for you when it is missing. Stage 1 silently skips every check (treating it as passed) when the file is absent **and** that automatic detection failed **and** no explicit commands are configured.

### Diagnosing Stage 1 Failures

Event query to inspect what happened:

```bash
uv run ouroboros status execution <exec_id> --events
```

Look for events of type `evaluation.stage1.completed`. The payload contains:
- `passed`: overall result
- `checks`: list with `check_type`, `passed`, `message` for each check
- `coverage_score`: numeric coverage if parsed
- `failed_count`: number of failed checks

---

## Stage 2: Semantic Evaluation

Stage 2 calls a Standard-tier LLM (default: `OUROBOROS_SEMANTIC_MODEL` / config value) to evaluate the artifact against the acceptance criterion. The model returns a structured JSON object.

### Scoring Fields

| Field | Type | Range | Meaning |
|-------|------|-------|---------|
| `score` | float | 0.0–1.0 | Overall quality score |
| `ac_compliance` | bool | — | Whether the AC is met |
| `goal_alignment` | float | 0.0–1.0 | Alignment with original seed goal |
| `drift_score` | float | 0.0–1.0 | Deviation from seed intent (lower is better) |
| `uncertainty` | float | 0.0–1.0 | Model's uncertainty about its own evaluation |
| `reasoning` | string | — | Free-text explanation |
| `reward_hacking_risk` | float | 0.0–1.0 | Suspicion that the artifact games the evaluator instead of solving the task. Distinct from `drift_score`, and vetoes approval at `>= 0.7` |
| `questions_used` | list | — | Questions the evaluator asked while verifying the artifact — it has to show its work |
| `evidence` | list | — | File snippets and observations the verdict relied on |

### Approval Logic

```
if ac_compliance == False and not trigger_consensus  → REJECTED (Stage 3 not attempted)
if score < 0.8                    → REJECTED (unless Stage 3 is triggered and approves)
if score >= 0.8 and no trigger    → APPROVED
if reward_hacking_risk >= 0.7     → REJECTED (final veto, overrides any approval)
```

> **The score gate is hardcoded at `0.8`.** `SemanticConfig.satisfaction_threshold` (default `0.8`) exists and is validated, but the pipeline compares against a literal `0.8` and never reads the field. Changing it has no effect on approval today — do not rely on it to loosen or tighten the gate.

> **`ac_compliance=False` is not always final.** When the evaluation context sets `trigger_consensus=True`, the pipeline continues to the trigger matrix instead of rejecting, so Stage 3 can deliver a second opinion on an AC the Stage 2 model failed.

> Scores are clamped to 0.0–1.0 after parsing; out-of-range model responses are corrected automatically.

### Reward-Hacking Veto

`_build_result()` applies one final gate that every approval path funnels through: if Stage 2 reported `reward_hacking_risk >= 0.7` (`REWARD_HACKING_VETO_THRESHOLD`), an otherwise-approved result is flipped to rejected. The threshold is deliberately high so that mild suspicion never blocks a genuine pass.

The veto only turns approve into reject — it never rescues an already-rejected result, so a Stage 3 consensus rejection stays a rejection.

`failure_reason` names the veto explicitly only when no earlier branch matched. `_build_result()` tests Stage 1, then Stage 3, then Stage 2 AC non-compliance, and reaches the veto branch last (`evaluation/pipeline.py:307-326`). So when `trigger_consensus=True` carries an `ac_compliance=False` result to an approving Stage 3 and the veto then rejects it, the reported reason is Stage 2 AC non-compliance, not the veto. Read `stage2_result.reward_hacking_risk` directly when you need to know whether the veto fired.

### Stage 2 Failure Modes

| Failure mode | Symptom | Cause / Action |
|---|---|---|
| **LLM API error** | `ProviderError` returned | Network issue, rate limit, or invalid API key. Check `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. The error propagates up — the pipeline stops without marking rejected. |
| **No JSON in response** | `ValidationError: Could not find JSON in response` | The LLM replied without a JSON object. This can happen with certain provider-model combinations. Check model compatibility with `json_schema` response format. |
| **Invalid JSON** | `ValidationError: Invalid JSON in response` | JSON parse error in model output. May indicate model truncation; try increasing `max_tokens`. |
| **Missing required fields** | `ValidationError: Missing required fields: [...]` | Model omitted required fields (`score`, `ac_compliance`, etc.). Usually means a model that does not support structured output reliably. |
| **AC non-compliance** | `Stage 2 failed: AC non-compliance (score=X.XX)` | The LLM determined the artifact does not meet the AC. Inspect `reasoning` in the Stage 2 completed event. |
| **Score below threshold** | `final_approved=False` with high `ac_compliance=True` | Score is between 0.0–0.79. Either the artifact quality is genuinely low, or the AC is too broad. |

### Stage 2 Configuration

```yaml
# In PipelineConfig.semantic (SemanticConfig)
semantic:
  model: null                            # null = resolve from OUROBOROS_SEMANTIC_MODEL / config
  temperature: 0.2                       # Low for consistency
  max_tokens: 2048                       # Response token budget
  satisfaction_threshold: 0.8           # Currently inert — the gate is hardcoded to 0.8
```

### Diagnosing Stage 2 Failures

Look for event `evaluation.stage2.completed`. Key fields:
- `score`, `ac_compliance`, `goal_alignment`, `drift_score`, `uncertainty`

If `ac_compliance` is `false` but `score` seems high, the LLM may have found a partial implementation. Read the `reasoning` field in the full event payload for the explanation.

---

## Consensus Trigger Matrix (Stage 2 → Stage 3 Gate)

After a **compliant** Stage 2 result (`ac_compliance=True`), trigger conditions are evaluated **in priority order**. The first matching condition triggers Stage 3. If none match, the `score >= 0.8` gate decides and the artifact is approved immediately when it clears.

Note the ordering: triggers are not gated on the score. The pipeline returns early only for `ac_compliance=False` without `trigger_consensus` (`evaluation/pipeline.py:178`). A compliant result scoring 0.7 with high drift still reaches Stage 3, and Stage 3 can approve it.

| Priority | Trigger | Condition |
|----------|---------|-----------|
| 0 | `manual_request` | `manual_consensus_request=True`, set from `trigger_consensus=true` on the evaluation context |
| 1 | `seed_modification` | `seed_modified=True` in context |
| 2 | `ontology_evolution` | `ontology_changed=True` in context |
| 3 | `goal_interpretation` | `goal_reinterpreted=True` in context |
| 4 | `seed_drift_alert` | `drift_score > drift_threshold` (default **0.3**) |
| 5 | `stage2_uncertainty` | `uncertainty > uncertainty_threshold` (default **0.3**) |
| 6 | `lateral_thinking_adoption` | `lateral_thinking_adopted=True` in context |

> **`manual_request` short-circuits everything.** It is checked before the other six, so asking for consensus explicitly always reaches Stage 3 regardless of drift, uncertainty, or Stage 2's verdict.

> **Only the first matching trigger fires.** If drift is 0.5 and lateral thinking was also adopted, only `seed_drift_alert` (priority 4) is reported.

> **Both threshold comparisons are strict (`>`).** A `drift_score` exactly equal to `drift_threshold` does not trigger Stage 3. When Stage 2 ran, its `drift_score` and `uncertainty` take precedence over any values pre-populated on the `TriggerContext`.

### Trigger Configuration

```yaml
# In PipelineConfig.trigger (TriggerConfig)
trigger:
  drift_threshold: 0.3        # Increase to reduce Stage 3 invocations
  uncertainty_threshold: 0.3  # Increase to reduce Stage 3 invocations
```

Raising these thresholds reduces Stage 3 cost but may allow low-confidence outputs to skip consensus.

### Trigger Failure Modes

| Failure mode | Symptom | Cause / Action |
|---|---|---|
| **Stage 3 triggered unexpectedly** | Unexpected high cost | Stage 2 uncertainty above threshold. Inspect `evaluation.consensus.triggered` event to find `trigger_type`. |
| **Stage 3 never fires** | Quality concerns go unverified | All trigger conditions evaluated to false; check that `drift_score` and `uncertainty` fields are being propagated correctly from Stage 2. |
| **Trigger validation error** | `ValidationError` from trigger | Malformed `TriggerContext`; ensure `execution_id` and numeric fields are valid. |

---

## Stage 3: Multi-Model Consensus

Stage 3 calls multiple Frontier-tier models concurrently. Each votes independently; a **2/3 majority** is required for approval.

### Simple Consensus (Default)

Three models are queried in parallel. **What those three are depends on the active LLM backend**, and the difference matters when you audit reviewer independence.

**OpenRouter-capable backends.** The shipped roster routes through OpenRouter so the voters come from three different vendors:

```
openrouter/openai/gpt-4o
openrouter/anthropic/claude-opus-4.8
openrouter/google/gemini-2.5-pro
```

**Sentinel-model backends.** Codex, Kiro, Copilot, Hermes, Pi, GJC, Antigravity, Grok, and Zcode select their model through their own CLI config rather than a `--model` flag, so `get_consensus_models()` maps the shipped roster to the literal string `"default"` for all three slots (`config/loader.py:1947`, `_SENTINEL_DEFAULT_BACKENDS` at `config/loader.py:101`).

`_should_use_multi_model()` treats anything that does not start with `openrouter/` as a genuine multi-model roster (`evaluation/consensus.py:313`), so `"default"` does **not** trigger the single-model perspective fallback below. On these backends Stage 3 casts three votes labeled `default`, all from the one model that backend is configured to use.

> **Auditing note.** Three votes labeled `default` are not three independent reviewers. If your consensus evidence shows that, the roster was resolved to the sentinel and the votes came from one model. Set `OUROBOROS_CONSENSUS_MODELS` or `consensus.models` explicitly when you need genuine cross-vendor independence on those backends.

Each returns `{ approved, confidence, reasoning }`.

**Approval rule:** `approving_votes / total_votes >= 0.66` (i.e., at least 2 of 3). The ratio is computed over the votes actually **collected**, not over the number of configured models — see [Parallel Consensus Failure Tolerance](#parallel-consensus-failure-tolerance).

> **Single-model fallback.** When the configured models are `openrouter/*` but `OPENROUTER_API_KEY` is missing or still a `YOUR_…` placeholder, `ConsensusEvaluator` silently switches to a single-model mode: the session model is queried three times with the advocate, devil's advocate, and judge system prompts, and those three perspectives vote. The 2-of-3 rule and the `<2` votes error are unchanged, but the reviewers are the same vendor by construction, so reviewer independence is reported as unavailable. Stage 3 events carry `session/<perspective>` model names and a `single-model-perspectives:` trigger reason — check for those if Stage 3 looks cheaper than expected. Configuring non-`openrouter/*` models skips the check and uses them as-is.

### Deliberative Consensus

An alternative two-round mode:
1. **Round 1 (parallel):** Advocate (finds strengths) and Devil's Advocate (ontological analysis for root-cause verification) present positions independently.
2. **Round 2:** Judge reviews both positions and returns a verdict: `approved`, `rejected`, or `conditional`.

> **Note:** `conditional` is a valid Judge verdict in the deliberative mode. A `conditional` verdict maps to **rejected** in the `DeliberationResult.approved` property (which returns `True` only for `approved`). Conditions are listed in `JudgmentResult.conditions`.

### Stage 3 Failure Modes

| Failure mode | Symptom | Cause / Action |
|---|---|---|
| **Fewer than 2 votes collected** | `ValidationError: Not enough votes collected: N/3` | Multiple models returned API errors. Check API keys for all configured consensus models. At least 2 of 3 models must respond. |
| **All models vote differently** | `majority_ratio` around 0.33–0.50 | Genuine disagreement. Inspect `disagreements` list in the event payload. Consider refining the AC or the artifact. |
| **Majority ratio below threshold** | `Stage 3 failed: Consensus not reached (XX%)` | Less than 2/3 approval. The `disagreements` tuple in `ConsensusResult` contains dissenters' reasoning. |
| **Individual model API error** | Logged but tolerated | One model fails; the remaining votes are used. If only 1 remains, a `ValidationError` is raised. |
| **Deliberative: Advocate fails** | `ValidationError: Advocate failed: ...` | Advocate model API error. The error is not tolerated in deliberative mode — the entire Stage 3 fails. |
| **Deliberative: Devil's Advocate LLM error** | Devil votes `approved=False` with low confidence | The `DevilAdvocateStrategy` handles LLM errors internally and returns `AnalysisResult.invalid` (soft failure) rather than propagating the error. A Devil LLM failure does **not** abort Stage 3; it results in the Devil casting a failing vote, which may cause the Judge to reject. |
| **Deliberative: Judge fails** | `ProviderError` or `ValidationError` | Judge model error. Stage 3 fails. Deliberative mode has no partial-vote tolerance for the Judge. |
| **Invalid JSON from voter** | `ValidationError: Could not find JSON in vote from <model>` | Model returned malformed JSON. Retry, or swap the model in `ConsensusConfig.models`. |
| **Invalid verdict from Judge** | `ValidationError: Invalid verdict '<x>' from <model>` | Judge responded with an unrecognized verdict string. Accepted values: `approved`, `rejected`, `conditional`. |

### Stage 3 Configuration

**Simple Consensus (`ConsensusConfig`)**

```yaml
# In PipelineConfig.consensus (ConsensusConfig)
consensus:
  models:
    - "openrouter/openai/gpt-4o"
    - "openrouter/anthropic/claude-opus-4.8"
    - "openrouter/google/gemini-2.5-pro"
  temperature: 0.3
  max_tokens: 1024
  majority_threshold: 0.66     # 2/3 majority
  diversity_required: true     # Currently inert — see note below
```

> **`diversity_required` is not enforced.** The field exists on `ConsensusConfig` and in the config schema, but nothing reads it. Provider diversity comes from the default model roster itself, not from this flag. If you replace the roster with three models from one vendor, setting `diversity_required: true` will not object.

**Deliberative Consensus (`DeliberativeConfig`)**

Used with `DeliberativeConsensus` (not `ConsensusEvaluator`). Each role uses a separate model:

```python
from ouroboros.evaluation.consensus import DeliberativeConfig, DeliberativeConsensus

config = DeliberativeConfig(
    advocate_model="openrouter/anthropic/claude-opus-4.8",  # Advocate role
    devil_model="openrouter/openai/gpt-4o",                 # Devil's Advocate (ontological analysis)
    judge_model="openrouter/google/gemini-2.5-pro",         # Final judgment
    temperature=0.3,
    max_tokens=2048,
)
evaluator = DeliberativeConsensus(llm_adapter, config)
```

Model defaults for `DeliberativeConfig` are read from `OUROBOROS_CONSENSUS_ADVOCATE_MODEL`, `OUROBOROS_CONSENSUS_DEVIL_MODEL`, and `OUROBOROS_CONSENSUS_JUDGE_MODEL` environment variables (or the config values documented in [Config Reference](../config-reference.md)).

### Diagnosing Stage 3 Failures

Look for event `evaluation.stage3.completed`. Key fields:
- `approved`: final decision
- `votes`: list of `{ model, approved, confidence, reasoning }`
- `majority_ratio`: fraction of approving votes
- `disagreements`: reasoning from dissenting votes

> **Deliberative mode `majority_ratio` caveat:** In deliberative consensus, the `majority_ratio` field in the `evaluation.stage3.completed` event is always `1.0` (approved) or `0.0` (rejected) — it does not reflect an actual vote fraction. Use the `votes` list and the `approved` field of each entry to see the Advocate and Devil's Advocate positions.

---

## Artifact Collection

Before Stage 2 runs, the `ArtifactCollector` attempts to read the actual source files changed during execution. This gives the semantic evaluator real code rather than just agent text summaries.

### Collection Limits

| Limit | Value | Effect when exceeded |
|-------|-------|---------------------|
| Max file size (`MAX_FILE_SIZE`) | 100 KB | Files larger than 100 KB are silently skipped |
| Max total content (`MAX_TOTAL_CHARS`) | 150,000 chars (~37K tokens) | Content is truncated at the remaining budget; `FileArtifact.truncated=True` |

There is **no cap on the number of files**. The character budget is the sole limiter, so a change touching many small files is collected in full while one oversized file is dropped.

> The two limits behave differently. A file over the per-file size limit is **skipped entirely** (never truncated); a file that only exhausts the shared character budget is **truncated** and marked `truncated=True`. If a critical file is always missing, check whether it is a generated binary or minified output that should be excluded from evaluation.

### Artifact Collection Failure Modes

| Failure mode | Symptom | Cause / Action |
|---|---|---|
| **project_dir not set** | Evaluation uses only text summary | `ArtifactBundle` built without file content; semantic evaluator falls back to agent text output. Set `project_dir` in the execution context. |
| **No file paths extracted** | Same as above | Execution output did not contain recognizable `Write:` / `Edit:` / `file_path:` patterns. The fallback is the text summary. |
| **Path traversal blocked** | File silently skipped | File path resolves outside `project_dir`. This is a security boundary, not a bug. |
| **Permission error** | File silently skipped | Execution ran as a different user. Verify file permissions. |
| **Large files skipped** | Missing context in evaluation | File > 100 KB. Refactor to split large files, or accept that the evaluator works from the text summary. |

---

## Pipeline-Level Error Handling

### Error vs. Failure

Ouroboros distinguishes between **failures** (the artifact does not meet criteria) and **errors** (the pipeline itself cannot complete):

| Outcome | Type | What happens |
|---------|------|-------------|
| Stage 1 check fails | Failure | `EvaluationResult.final_approved=False`, `failure_reason` set |
| Stage 2 AC non-compliance | Failure | Same — `EvaluationResult.final_approved=False` |
| Stage 3 minority vote | Failure | Same — `EvaluationResult.final_approved=False` |
| LLM API error (Stage 2/3) | Error | `Result.err(ProviderError)` propagated up — the runner receives the error, not a failed result |
| Too few votes (Stage 3) | Error | `Result.err(ValidationError)` — consensus could not be attempted |
| JSON parse failure (Stage 2/3) | Error | `Result.err(ValidationError)` — evaluation abandoned |

**Errors** leave the AC in an indeterminate state. The orchestrator runner handles them via tier escalation (retry with a stronger model) or stagnation detection if retries are exhausted.

### Disabling Stages

Individual stages can be disabled in `PipelineConfig`:

```python
from ouroboros.evaluation.pipeline import PipelineConfig

# Skip mechanical verification (e.g., for document-type artifacts)
config = PipelineConfig(stage1_enabled=False)

# Skip consensus (cost-constrained runs)
config = PipelineConfig(stage3_enabled=False)
```

> **Warning:** Disabling Stage 1 means that broken code can pass through to semantic evaluation. Disabling Stage 3 means that high-drift or high-uncertainty outputs will never be submitted to multi-model review.

> **Stage 2 disabled does not disable Stage 3.** The trigger context is built outside the Stage 2 block precisely so that `trigger_consensus=True` still works when `stage2_enabled=False` — it fires the `manual_request` trigger and Stage 3 runs on its own. What does silently disable Stage 3 is the combination of `stage2_enabled=False`, `trigger_consensus=False`, and no external `trigger_context`: every trigger field is left at its default, so nothing matches. To reach Stage 3 without Stage 2, either set `trigger_consensus=True` or pass a pre-populated `TriggerContext` to `EvaluationPipeline.evaluate()`.

### Failure Reason Lookup

`EvaluationResult.failure_reason` returns a human-readable string:

| Condition | `failure_reason` value |
|-----------|------------------------|
| Stage 1 failed | `"Stage 1 failed: lint, test"` (comma-separated failed check names) |
| Stage 2 AC non-compliance (`ac_compliance=False`) | `"Stage 2 failed: AC non-compliance (score=0.62)"` |
| Stage 2 score below threshold (`ac_compliance=True` but `score < 0.8`) | `"Unknown failure"` — the score check runs after Stage 2 but the `failure_reason` property only tests `ac_compliance`. Inspect `stage2_result.score` directly to distinguish this case. |
| Stage 3 consensus not reached | `"Stage 3 failed: Consensus not reached (44%)"` |
| Reward-hacking veto (`reward_hacking_risk >= 0.7`) | A message naming the risk score and the 0.70 threshold, **as long as no earlier branch matched**. The order is Stage 1, Stage 3, Stage 2 AC non-compliance, then the veto, so a vetoed result that also had `ac_compliance=False` reports the AC failure instead. Read `stage2_result.reward_hacking_risk` to be sure. |
| All stages passed/skipped but `final_approved=False` | `"Unknown failure"` |

---

## Evaluation Edge Cases

### AC-Specific Evaluation

Each AC in the tree is evaluated **independently**. The `EvaluationContext` carries a single `current_ac` string. If an artifact bundle references files from multiple ACs, the semantic evaluator still scores only for the single AC under evaluation.

### Numeric Score Clamping

Stage 2 scores are automatically clamped to [0.0, 1.0] regardless of what the LLM returns. Out-of-range values from the model do not cause errors; they are silently corrected. If you see a score of exactly 0.0 or 1.0, check whether the model was returning values outside the valid range.

### Stage 2 Uncertainty Propagation

If `TriggerContext` is provided externally with `uncertainty_score` already set, but the `semantic_result` field is also set, the **semantic_result** value takes precedence for the drift and uncertainty trigger checks. Pre-populated `TriggerContext` fields are only used when there is no `semantic_result`.

### Deliberative Mode `conditional` Verdicts

In deliberative consensus, the Judge can return `conditional`. This means the Judge sees merit but requires specific changes before approval. The conditions are listed in `JudgmentResult.conditions`. **`conditional` is treated as rejection** in the pipeline (`DeliberationResult.approved == False`). The conditions should be surfaced to the user as actionable feedback; they appear in the `evaluation.stage3.completed` event payload's `votes` list.

### Coverage Score Parsing

Stage 1 parses coverage from `pytest-cov` output by looking for the pattern `TOTAL  N  N  XX%` or `Coverage: XX%`. If your coverage tool outputs a different format, the `coverage_score` will be `None` and the coverage check will pass even if coverage is zero. Configure a compatible coverage command or check the event payload's `coverage_score` field to verify parsing worked.

### Parallel Consensus Failure Tolerance

In **simple consensus**, individual model failures are tolerated as long as at least 2 models respond successfully. The `majority_ratio` is calculated over only the collected votes (`approving / len(votes)`), not over the configured number of models. This means:
- 2 models respond, 1 approves → `majority_ratio = 0.5` → **rejected** (below 0.66)
- 2 models respond, both approve → `majority_ratio = 1.0` → **approved**

In **deliberative consensus**, the Advocate and Judge roles must complete successfully — a failure in either causes Stage 3 to return an error. The Devil's Advocate role handles LLM errors internally (returns a failing vote rather than propagating the error), so a Devil model failure does not abort Stage 3 by itself.

---

## Full Configuration Reference

```python
from ouroboros.evaluation.pipeline import PipelineConfig
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.semantic import SemanticConfig
from ouroboros.evaluation.consensus import ConsensusConfig
from ouroboros.evaluation.trigger import TriggerConfig

config = PipelineConfig(
    # Enable/disable stages
    stage1_enabled=True,
    stage2_enabled=True,
    stage3_enabled=True,

    # Stage 1: Mechanical verification
    mechanical=MechanicalConfig(
        coverage_threshold=0.7,       # NFR9 minimum; 0.0 disables threshold
        lint_command=("ruff", "check", "."),
        build_command=None,           # None = skip this check
        test_command=("pytest", "tests/"),
        static_command=("mypy", "src/"),
        coverage_command=("pytest", "--cov=src", "--cov-report=term-missing", "tests/"),
        timeout_seconds=300,          # Per-command timeout
        working_dir=None,             # Defaults to process cwd
    ),

    # Stage 2: Semantic evaluation
    semantic=SemanticConfig(
        model=None,                  # None = resolved from the semantic_evaluation role
        temperature=0.2,
        max_tokens=2048,
        satisfaction_threshold=0.8,  # Currently inert — the gate is hardcoded to 0.8
    ),

    # Stage 3: Simple consensus evaluation
    consensus=ConsensusConfig(
        models=(
            "openrouter/openai/gpt-4o",
            "openrouter/anthropic/claude-opus-4.8",
            "openrouter/google/gemini-2.5-pro",
        ),
        temperature=0.3,
        max_tokens=1024,
        majority_threshold=0.66,     # 2/3 majority required
        diversity_required=True,     # Currently inert
    ),

    # Consensus trigger thresholds
    trigger=TriggerConfig(
        drift_threshold=0.3,         # stage2 drift_score above this triggers Stage 3
        uncertainty_threshold=0.3,   # stage2 uncertainty above this triggers Stage 3
    ),
)
```

For deliberative consensus (separate from `EvaluationPipeline`):

```python
from ouroboros.evaluation.consensus import DeliberativeConfig, DeliberativeConsensus

deliberative_config = DeliberativeConfig(
    advocate_model="openrouter/anthropic/claude-opus-4.8",  # Advocate role
    devil_model="openrouter/openai/gpt-4o",                 # Devil's Advocate (ontological analysis)
    judge_model="openrouter/google/gemini-2.5-pro",         # Final judgment
    temperature=0.3,
    max_tokens=2048,
)
# Used directly, not via EvaluationPipeline
evaluator = DeliberativeConsensus(llm_adapter, deliberative_config)
result = await evaluator.deliberate(context, trigger_reason="seed_drift_alert")
```

---

## Event Audit Trail

Every stage emits events to the SQLite event store. Use these to reconstruct what happened in any evaluation:

| Event type | When emitted | Key payload fields |
|---|---|---|
| `evaluation.stage1.started` | Stage 1 begins | `checks_to_run` |
| `evaluation.stage1.completed` | Stage 1 ends | `passed`, `checks`, `coverage_score`, `failed_count` |
| `evaluation.stage2.started` | Stage 2 begins | `model`, `current_ac` |
| `evaluation.stage2.completed` | Stage 2 ends | `score`, `ac_compliance`, `goal_alignment`, `drift_score`, `uncertainty` |
| `evaluation.consensus.triggered` | Trigger matrix fires | `trigger_type`, `trigger_details` |
| `evaluation.stage3.started` | Stage 3 begins | `models`, `trigger_reason` |
| `evaluation.stage3.completed` | Stage 3 ends | `approved`, `votes`, `majority_ratio`, `disagreements` |
| `evaluation.pipeline.completed` | Full pipeline done | `final_approved`, `highest_stage`, `failure_reason` |

Query events for a specific execution:

```bash
uv run ouroboros status execution <exec_id> --events
```

---

## See Also

- [Architecture Guide](../architecture.md) — Phase 4 in the six-phase pipeline
- [Seed Authoring Guide](./seed-authoring.md) — Writing good acceptance criteria reduces AC non-compliance
- [Getting Started](../getting-started.md) — First-run onboarding for new users
- [Config Reference](../config-reference.md) — Model override environment variables (`OUROBOROS_SEMANTIC_MODEL`, `OUROBOROS_CONSENSUS_MODELS`)
