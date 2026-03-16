<!--
doc_metadata:
  runtime_scope: [all]
-->

# Evaluation Pipeline Guide

Ouroboros Phase 4 runs every execution result through a **three-stage progressive evaluation pipeline** before marking an acceptance criterion (AC) as approved. Cheaper checks gate the expensive ones: Stage 1 is free, Stage 2 uses one LLM call, and Stage 3 (multi-model consensus) runs only when specifically triggered.

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
        │ Trigger │ ← 6 conditions checked
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

### Language Auto-Detection

When `build_mechanical_config(working_dir)` is used (the default when running via `ouroboros run`), Stage 1 commands are **automatically populated** by scanning the project directory for known marker files. You do not need to configure commands manually for supported toolchains.

**Detection priority** (first match wins):

| Marker file | Detected toolchain | Default commands |
|---|---|---|
| `uv.lock` | `python-uv` | `uv run ruff`, `uv run pytest --cov`, `uv run mypy` |
| `build.zig` | `zig` | `zig build`, `zig build test` |
| `Cargo.toml` | `rust` | `cargo clippy`, `cargo build`, `cargo test` |
| `go.mod` | `go` | `go vet ./...`, `go build ./...`, `go test ./...`, `go test -cover ./...` |
| `bun.lockb` / `bun.lock` | `node-bun` | `bun lint`, `bun run build`, `bun test` |
| `pnpm-lock.yaml` | `node-pnpm` | `pnpm lint`, `pnpm build`, `pnpm test` |
| `yarn.lock` | `node-yarn` | `yarn lint`, `yarn build`, `yarn test` |
| `package-lock.json` | `node-npm` | `npm run lint`, `npm run build`, `npm test` |
| `pyproject.toml` / `setup.py` / `setup.cfg` | `python` | `ruff check .`, `pytest --cov`, `mypy .` |
| `package.json` (no lockfile) | `node-npm` | `npm run lint`, `npm run build`, `npm test` |

If no marker file is found, all commands remain `None` and all checks are silently skipped.

> **Go coverage note:** The `go test -cover` output format (`ok  ./... coverage: XX.X% of statements`) is not matched by the coverage parser (which expects `TOTAL ... XX%` or `Coverage: XX%`). For Go projects, `coverage_score` will always be `None` in the event payload and the coverage **threshold check is skipped even if coverage is low**. Use the `.ouroboros/mechanical.toml` override to supply a custom coverage command if you need threshold enforcement on Go projects.

### Project-Level Command Overrides

Create `.ouroboros/mechanical.toml` in your project root to override auto-detected commands without modifying Ouroboros configuration:

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
3. Auto-detected language preset
4. All `None` (all checks skip gracefully)

**TOML parse errors** are logged as a warning (`mechanical.toml_parse_error`) and silently ignored; the auto-detected preset commands are still used.

**Security: executable allowlist.** Commands in `.ouroboros/mechanical.toml` may only use executables from a built-in allowlist (e.g., `pytest`, `ruff`, `cargo`, `go`, `npm`, `make`). If a command specifies an executable not in the allowlist, it is silently blocked (logged as `mechanical.blocked_executable`) and the check is skipped. Hardcoded language presets bypass this check. This prevents untrusted repository configs from running arbitrary commands in CI/CD environments.

| Override failure mode | Symptom | Cause / Action |
|---|---|---|
| **TOML parse error** | Auto-detected preset used; no error raised | Malformed `.ouroboros/mechanical.toml`; check TOML syntax |
| **Blocked executable** | Check silently skipped | Executable not in allowlist; use an allowed tool or set the command in `MechanicalConfig` directly |
| **Language not detected** | All Stage 1 checks skipped | No marker file found; add a `pyproject.toml` / `Cargo.toml` / etc., or set commands explicitly |

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

> **Important:** When using `ouroboros run`, commands are auto-detected from the project directory. All Stage 1 checks are silently skipped (and treated as passed) only when no marker file is found **and** no explicit commands are configured. Use `.ouroboros/mechanical.toml` or `MechanicalConfig` overrides to customize behavior.

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

### Approval Logic

```
if ac_compliance == False  → REJECTED (Stage 3 not attempted)
if score < 0.8             → REJECTED (unless Stage 3 is triggered and approves)
if score >= 0.8 and no trigger → APPROVED
```

> The `satisfaction_threshold` (default `0.8`) is in `SemanticConfig`. Values between 0.0–1.0 are clamped after parsing; out-of-range model responses are corrected automatically.

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
  model: "claude-3-5-sonnet-20241022"   # Standard tier model
  temperature: 0.2                       # Low for consistency
  max_tokens: 2048                       # Response token budget
  satisfaction_threshold: 0.8           # Minimum score to approve
```

### Diagnosing Stage 2 Failures

Look for event `evaluation.stage2.completed`. Key fields:
- `score`, `ac_compliance`, `goal_alignment`, `drift_score`, `uncertainty`

If `ac_compliance` is `false` but `score` seems high, the LLM may have found a partial implementation. Read the `reasoning` field in the full event payload for the explanation.

---

## Consensus Trigger Matrix (Stage 2 → Stage 3 Gate)

After Stage 2 passes (`ac_compliance=True`, `score >= 0.8`), six trigger conditions are evaluated **in priority order**. The first matching condition triggers Stage 3. If none match, the artifact is approved immediately.

| Priority | Trigger | Condition |
|----------|---------|-----------|
| 1 | `seed_modification` | `seed_modified=True` in context |
| 2 | `ontology_evolution` | `ontology_changed=True` in context |
| 3 | `goal_interpretation` | `goal_reinterpreted=True` in context |
| 4 | `seed_drift_alert` | `drift_score > drift_threshold` (default **0.3**) |
| 5 | `stage2_uncertainty` | `uncertainty > uncertainty_threshold` (default **0.3**) |
| 6 | `lateral_thinking_adoption` | `lateral_thinking_adopted=True` in context |

> **Only the first matching trigger fires.** If drift is 0.5 and lateral thinking was also adopted, only `seed_drift_alert` (priority 4) is reported.

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

Three models are queried in parallel (default: `gpt-4o`, `claude-sonnet-4`, `gemini-2.5-pro`). Each returns `{ approved, confidence, reasoning }`.

**Approval rule:** `approving_votes / total_votes >= 0.66` (i.e., at least 2 of 3).

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
    - "gpt-4o"
    - "claude-sonnet-4-20250514"
    - "gemini/gemini-2.5-pro"
  temperature: 0.3
  max_tokens: 1024
  majority_threshold: 0.66     # 2/3 majority
  diversity_required: true     # Prefer models from different providers
```

**Deliberative Consensus (`DeliberativeConfig`)**

Used with `DeliberativeConsensus` (not `ConsensusEvaluator`). Each role uses a separate model:

```python
from ouroboros.evaluation.consensus import DeliberativeConfig, DeliberativeConsensus

config = DeliberativeConfig(
    advocate_model="claude-sonnet-4-20250514",   # Advocate role
    devil_model="claude-sonnet-4-20250514",       # Devil's Advocate (ontological analysis)
    judge_model="gpt-4o",                         # Final judgment
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
| Max files | 30 | Files beyond 30th are silently skipped |
| Max file size | 50 KB | Files larger than 50 KB are silently skipped |
| Max total content | 150,000 chars (~37K tokens) | Files are truncated at budget; `FileArtifact.truncated=True` |

> Files that exceed the per-file size limit are **skipped entirely** (not truncated). If a critical file is always skipped, check whether it is a generated binary or minified output that should be excluded from evaluation.

### Artifact Collection Failure Modes

| Failure mode | Symptom | Cause / Action |
|---|---|---|
| **project_dir not set** | Evaluation uses only text summary | `ArtifactBundle` built without file content; semantic evaluator falls back to agent text output. Set `project_dir` in the execution context. |
| **No file paths extracted** | Same as above | Execution output did not contain recognizable `Write:` / `Edit:` / `file_path:` patterns. The fallback is the text summary. |
| **Path traversal blocked** | File silently skipped | File path resolves outside `project_dir`. This is a security boundary, not a bug. |
| **Permission error** | File silently skipped | Execution ran as a different user. Verify file permissions. |
| **Large files skipped** | Missing context in evaluation | File > 50 KB. Refactor to split large files, or accept that the evaluator works from the text summary. |

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

> **Stage 2 disabled → Stage 3 implicitly disabled.** Stage 3 runs only when a `TriggerContext` is available. When `stage2_enabled=False`, the pipeline never builds a `TriggerContext`, so Stage 3 will not run even if `stage3_enabled=True` and no external `trigger_context` is passed in. To use Stage 3 without Stage 2, pass a pre-populated `TriggerContext` explicitly to `EvaluationPipeline.evaluate()`.

### Failure Reason Lookup

`EvaluationResult.failure_reason` returns a human-readable string:

| Condition | `failure_reason` value |
|-----------|------------------------|
| Stage 1 failed | `"Stage 1 failed: lint, test"` (comma-separated failed check names) |
| Stage 2 AC non-compliance (`ac_compliance=False`) | `"Stage 2 failed: AC non-compliance (score=0.62)"` |
| Stage 2 score below threshold (`ac_compliance=True` but `score < 0.8`) | `"Unknown failure"` — the score check runs after Stage 2 but the `failure_reason` property only tests `ac_compliance`. Inspect `stage2_result.score` directly to distinguish this case. |
| Stage 3 consensus not reached | `"Stage 3 failed: Consensus not reached (44%)"` |
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
        model="claude-3-5-sonnet-20241022",
        temperature=0.2,
        max_tokens=2048,
        satisfaction_threshold=0.8,  # Minimum score for approval
    ),

    # Stage 3: Simple consensus evaluation
    consensus=ConsensusConfig(
        models=("gpt-4o", "claude-sonnet-4-20250514", "gemini/gemini-2.5-pro"),
        temperature=0.3,
        max_tokens=1024,
        majority_threshold=0.66,     # 2/3 majority required
        diversity_required=True,
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
    advocate_model="claude-sonnet-4-20250514",  # Advocate role
    devil_model="claude-sonnet-4-20250514",      # Devil's Advocate (ontological analysis)
    judge_model="gpt-4o",                        # Final judgment
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
