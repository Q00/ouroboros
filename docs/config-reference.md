<!--
doc_metadata:
  runtime_scope: [local, claude, codex, opencode]
-->

# Configuration Reference

Complete reference for `~/.ouroboros/config.yaml` and all related environment variables.

> **Source of truth:** `src/ouroboros/config/models.py` and `src/ouroboros/config/loader.py`
>
> Run `ouroboros config init` to generate defaults. Edit `~/.ouroboros/config.yaml` directly to apply changes.

---

## File Layout

```
~/.ouroboros/
├── config.yaml          # Main configuration (this document)
├── credentials.yaml     # API keys (chmod 600, do not put secrets in config.yaml)
├── ouroboros.db         # SQLite event store (EventStore hardcoded default)
├── seeds/               # Generated seed YAML files
├── data/                # Created by ensure_config_dir() — reserved for future use
├── logs/
│   └── ouroboros.log    # Log output
└── .env                 # Optional; loaded automatically by the CLI
```

---

## Codex CLI Users

For Codex-backed Ouroboros workflows:

- Put persistent Ouroboros role overrides in `~/.ouroboros/config.yaml`.
- Use `~/.codex/config.toml` only for the Codex MCP registration. On current Codex CLI releases, profile anchors written by `ouroboros setup --runtime codex` live in `~/.codex/<profile>.config.toml`.
- The Codex-aware loader does **not** hardcode a mini model when these keys are left at their shipped defaults. It resolves Codex-backed lookups to Codex's `default` sentinel unless you set an explicit model string.
- Use `llm_profiles` and `llm_role_profiles` when you want portable task profiles that can map to Codex CLI profiles or to ordinary model settings for other providers.

### Codex Role Override Map

| Role | `config.yaml` key |
|------|-------------------|
| Clarification / interview | `clarification.default_model` |
| QA verdict | `llm.qa_model` |
| Semantic evaluation | `evaluation.semantic_model` |
| Consensus simple voting | `consensus.models` |
| Consensus deliberative roles | `consensus.advocate_model`, `consensus.devil_model`, `consensus.judge_model` |

> **Recommended documented baseline:** use GPT-5.4 with medium reasoning effort in Codex CLI for standard work. `ouroboros setup --runtime codex` also creates cheaper fast defaults on GPT-5.4 Mini and deeper GPT-5.5 defaults for users whose ChatGPT plan exposes those models.

### Portable Task Profiles

`llm_profiles` are top-level, provider-neutral task profiles. `llm_role_profiles` maps logical Ouroboros roles to those profiles. For Codex, a provider mapping can use `profile`, which is passed as `codex exec --profile <name>`. Role mappings route model/native-profile selection and CLI turn budgets while preserving each call site's tuned sampling and token settings; explicit per-request `profile` selection opts into the profile's full tuning envelope.

```yaml
llm_profiles:
  fast:
    max_turns: 1
    temperature: 0.2
    providers:
      codex:
        profile: ouroboros-fast
      litellm:
        model: openrouter/openai/gpt-5.3-codex-spark

  standard:
    max_turns: 3
    temperature: 0.3
    providers:
      codex:
        profile: ouroboros-standard

  deep:
    max_turns: 5
    temperature: 0.4
    providers:
      codex:
        profile: ouroboros-deep
      claude_code:
        model: claude-opus-4-6
      gemini:
        model: gemini-2.5-pro
      opencode:
        model: openai/gpt-5.4
      litellm:
        model: openrouter/anthropic/claude-opus-4-6

  frontier:
    max_turns: 8
    temperature: 0.4
    providers:
      codex:
        profile: ouroboros-frontier

llm_role_profiles:
  ambiguity: deep
  assertion_extraction: fast
  brownfield: fast
  context_compression: deep
  mechanical_detection: fast
  question_classification: deep
  qa: frontier
  brownfield_explore: frontier
  clarification: frontier
  dependency_analysis: standard
  pm_interview: deep
  seed_generation: deep
  consensus_advocate: deep
  consensus_perspective: deep
  consensus_vote: deep
  ontology_analysis: deep
  pm_document: deep
  reflect: deep
  semantic_evaluation: deep
  wonder: frontier
  consensus_judge: frontier
  agent_runtime: standard
  agent_runtime_implementation: standard
  agent_runtime_interview: deep
  agent_runtime_coordinator: standard
  agent_runtime_evaluation: deep
```

Resolution order is: explicit request-level model pins, role mapping, profile provider mapping for the active backend, existing `*_model` field, then backend default behavior.

For Codex, setup creates flat profile-v2 anchors as `~/.codex/<profile>.config.toml` files on current Codex CLI releases:

```toml
# ~/.codex/ouroboros-fast.config.toml
model_reasoning_effort = "low"

# ~/.codex/ouroboros-standard.config.toml
model_reasoning_effort = "medium"

# ~/.codex/ouroboros-deep.config.toml
model_reasoning_effort = "high"

# ~/.codex/ouroboros-frontier.config.toml
model_reasoning_effort = "xhigh"
```

These are intentionally sparse, flat Codex profiles. Codex currently exposes a single `--profile <name>` selector; setup does not depend on unsupported profile-to-profile inheritance. Setup leaves `model` unset so the generated anchors inherit the user's local Codex default model instead of assuming account access to a specific frontier model. Add a `model = "..."` line to a profile file only when you want to pin a model that your account exposes. Older Codex CLI releases used `[profiles.ouroboros-*]` tables in `~/.codex/config.toml`; setup keeps that path only when the detected CLI still uses the legacy profile format.

If `~/.codex/config.toml` already contains a URL-based Ouroboros MCP server, setup preserves it instead of replacing it with a stdio command block:

```toml
[mcp_servers.ouroboros]
url = "http://127.0.0.1:12000/mcp"
```

---

## Top-Level Sections

| Section | Class | Purpose |
|---------|-------|---------|
| `orchestrator` | `OrchestratorConfig` | Runtime backend selection and agent permissions |
| `llm` | `LLMConfig` | LLM-only flow defaults (model selection, permission mode) |
| `economics` | `EconomicsConfig` | PAL Router tier definitions and escalation thresholds |
| `clarification` | `ClarificationConfig` | Phase 0 — Interview / Big Bang settings |
| `execution` | `ExecutionConfig` | Phase 2 — Double Diamond execution settings |
| `resilience` | `ResilienceConfig` | Phase 3 — Stagnation detection and lateral thinking |
| `evaluation` | `EvaluationConfig` | Phase 4 — 3-stage evaluation pipeline settings |
| `consensus` | `ConsensusConfig` | Phase 5 — Multi-model consensus settings |
| `llm_profiles` | `dict[str, LLMTaskProfileConfig]` | Provider-neutral LLM task profiles |
| `llm_role_profiles` | `dict[str, str]` | Logical role to LLM task profile mapping |
| `persistence` | `PersistenceConfig` | SQLite event store settings |
| `drift` | `DriftConfig` | Drift monitoring thresholds |
| `runtime_controls` | `RuntimeControlsConfig` | Long-running workflow liveness and progress controls |
| `logging` | `LoggingConfig` | Log level, path, and verbosity |

---

## `orchestrator`

Controls how Ouroboros launches and communicates with the agent runtime backend.

```yaml
orchestrator:
  runtime_backend: claude       # "claude" | "codex" | "opencode" | "hermes" | "gemini" | "kiro" | "copilot" | "pi"
  permission_mode: acceptEdits  # "default" | "acceptEdits" | "bypassPermissions"
  opencode_permission_mode: bypassPermissions
  max_parallel_workers: 3       # Maximum concurrent AC workers
  cli_path: null                # Path to Claude CLI binary; null = use SDK default
  codex_cli_path: null          # Path to Codex CLI binary; null = resolve from PATH
  opencode_cli_path: null       # Path to OpenCode CLI binary; null = resolve from PATH
  copilot_cli_path: null        # Path to Copilot CLI binary; null = resolve from PATH
  pi_cli_path: null             # Path to Pi CLI binary; null = resolve from PATH
  default_max_turns: 10
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `runtime_backend` | `"claude"` \| `"codex"` \| `"opencode"` \| `"hermes"` \| `"gemini"` \| `"kiro"` \| `"copilot"` \| `"pi"` | `"claude"` | The agent runtime backend used for workflow execution. Overridable via `OUROBOROS_AGENT_RUNTIME`. See [runtime capability matrix](runtime-capability-matrix.md). |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"acceptEdits"` | Stored permission preference. Runner-driven seed execution forces the native `bypassPermissions` equivalent for both fresh and resumed dispatches wherever the backend exposes an approval surface; persisted handles cannot downgrade it. Pi and GJC expose no separate approval flag and run headlessly without an approval dialogue. |
| `opencode_permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"bypassPermissions"` | Permission mode when using the OpenCode runtime. Overridable via `OUROBOROS_OPENCODE_PERMISSION_MODE`. |
| `max_parallel_workers` | `int >= 1` | `3` | Requested maximum concurrent Acceptance Criteria workers for parallel execution. Overridable via `OUROBOROS_MAX_PARALLEL_WORKERS`. Invalid explicit values fail instead of falling back to the default. **Note:** the effective fan-out is additionally capped to the connected backend's known concurrency limit — the native Claude backend is governed by its RPM/TPM rate bucket, while CLI runtimes whose underlying LLM limits are unknown (`hermes`, `codex`, `gemini`, `opencode`, ...) are **serialized to 1 by default** to avoid stampeding the provider's rate/quota window. Raise that cap with `OUROBOROS_MAX_CONCURRENCY`. |
| `cli_path` | `string \| null` | `null` | Absolute path to the Claude CLI binary (`~` is expanded). When `null`, the SDK-bundled CLI is used. Overridable via `OUROBOROS_CLI_PATH`. |
| `codex_cli_path` | `string \| null` | `null` | Absolute path to the Codex CLI binary (`~` is expanded). When `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_CODEX_CLI_PATH`. |
| `opencode_cli_path` | `string \| null` | `null` | Absolute path to the OpenCode CLI binary (`~` is expanded). When `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_OPENCODE_CLI_PATH`. |
| `copilot_cli_path` | `string \| null` | `null` | Absolute path to the GitHub Copilot CLI binary (`~` is expanded). When `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_COPILOT_CLI_PATH`. |
| `pi_cli_path` | `string \| null` | `null` | Absolute path to the Pi CLI binary (`~` is expanded). When `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_PI_CLI_PATH`. |
| `ourocode_cli_path` | `string \| null` | `null` | Absolute path to the ourocode CLI binary (`~` is expanded). Used by the LLM-only `ourocode` backend; when `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_OUROCODE_CLI_PATH`. |
| `default_max_turns` | `int >= 1` | `10` | Default maximum number of turns per agent execution task. |

---

## `llm`

Defaults for LLM-only flows (interview, seed generation, QA, analysis). The `orchestrator` section governs agent runtime execution; the `llm` section governs model-level LLM calls within the orchestration pipeline.

```yaml
llm:
  backend: claude_code
  permission_mode: default
  opencode_permission_mode: acceptEdits
  qa_model: claude-sonnet-4-6
  dependency_analysis_model: claude-sonnet-4-6
  ontology_analysis_model: claude-sonnet-4-6
  context_compression_model: gpt-4
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `backend` | `"claude"` \| `"claude_code"` \| `"litellm"` \| `"codex"` \| `"opencode"` \| `"hermes"` \| `"gemini"` \| `"kiro"` \| `"copilot"` \| `"goose"` \| `"pi"` \| `"ourocode"` \| `"gjc"` | `"claude_code"` | Default backend for LLM-only flows. Overridable via `OUROBOROS_LLM_BACKEND`. `ourocode` is LLM-only and is not valid for `orchestrator.runtime_backend`. |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"default"` | Permission mode for non-OpenCode LLM flows. Overridable via `OUROBOROS_LLM_PERMISSION_MODE`. |
| `opencode_permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"acceptEdits"` | Permission mode for OpenCode-backed LLM flows. Overridable via `OUROBOROS_OPENCODE_PERMISSION_MODE`. |
| `qa_model` | `string` | `"claude-sonnet-4-6"` | Model used for post-execution QA verdict generation. Overridable via `OUROBOROS_QA_MODEL`. |
| `dependency_analysis_model` | `string` | `"claude-sonnet-4-6"` | Model used for AC dependency analysis. Overridable via `OUROBOROS_DEPENDENCY_ANALYSIS_MODEL`. |
| `ontology_analysis_model` | `string` | `"claude-sonnet-4-6"` | Model used for ontological analysis. Overridable via `OUROBOROS_ONTOLOGY_ANALYSIS_MODEL`. |
| `context_compression_model` | `string` | `"gpt-4"` | Model used for workflow context compression. Overridable via `OUROBOROS_CONTEXT_COMPRESSION_MODEL`. |

When `llm.backend` is `ourocode`, model fields are `OUROCODE_MODEL` selectors,
not raw Anthropic model IDs. The supported selectors are `claude`, `claude_api`,
`codex`, and `gemini`; `default` and shipped Claude default pins resolve to
`claude` so journal metadata matches the ACP child process.

---

## `llm_profiles` and `llm_role_profiles`

`llm_profiles` define reusable task profiles outside any single provider. `llm_role_profiles` chooses which profile a logical Ouroboros task role should use.

Common role keys include `clarification`, `seed_generation`, `assertion_extraction`, `qa`, `semantic_evaluation`, `wonder`, `reflect`, `consensus_vote`, `consensus_advocate`, `consensus_judge`, `dependency_analysis`, `context_compression`, `ontology_analysis`, and `mechanical_detection`.

Profile fields:

| Field | Type | Description |
|-------|------|-------------|
| `model` | `string \| null` | Portable model override used when no provider-specific model is set. |
| `temperature` | `float \| null` | Portable temperature override. |
| `max_tokens` | `int \| null` | Portable token limit override. |
| `top_p` | `float \| null` | Portable nucleus sampling override. |
| `max_turns` | `int \| null` | Portable CLI-agent turn budget where supported. |
| `reasoning_effort` | `"low" \| "medium" \| "high" \| null` | Portable effort dial for providers with native reasoning-effort support. Ouroboros task profiles intentionally accept only `low`, `medium`, and `high`; backend-native profile files may expose additional provider-specific values such as Codex `xhigh`. |
| `providers` | `dict` | Backend-specific overrides keyed by `codex`, `claude_code`, `gemini`, `opencode`, `litellm`, or provider aliases such as `openrouter`. |

Provider-specific fields use the same keys plus `profile`. `profile` is currently backend-native metadata; Codex maps it to `codex exec --profile <name>`, while non-Codex adapters ignore it unless they add native profile support later. Role-based resolution uses these profile fields for model/native-profile routing, `max_turns`, and `reasoning_effort`; it intentionally preserves request-level `temperature`, `max_tokens`, and `top_p` so existing task-specific tuning does not change just because a role was annotated. Explicit `CompletionConfig.profile` requests use the profile's full sampling/token envelope. `ouroboros setup --runtime codex` installs missing default profiles and role mappings but preserves existing profile definitions, existing Codex provider model pins, and skips role mappings where explicit legacy model overrides are already configured.

Codex agent-runtime tasks also use these mappings. Runtime handles with `session_role: implementation`, `coordinator`, `interview`, or `evaluation` resolve through `agent_runtime_<session_role>`; tasks without a role fall back to `agent_runtime`. Explicit runtime models still win and are passed with `--model` instead of `--profile`.

---

## `economics`

Configures the PAL Router (Progressive Adaptive LLM): cost tiers, escalation on failure, and downgrade on success.

```yaml
economics:
  default_tier: frugal          # "frugal" | "standard" | "frontier"
  escalation_threshold: 2       # Retry attempt escalation begins; then +1 tier per retry
  downgrade_success_streak: 5   # Consecutive successes before downgrading tier
  tiers:
    frugal:
      cost_factor: 1
      intelligence_range: [9, 11]
      models:
        - provider: openai
          model: gpt-4o-mini
        - provider: google
          model: gemini-2.0-flash
        - provider: anthropic
          model: claude-3-5-haiku
      use_cases:
        - routine_coding
        - log_analysis
        - stage1_fix
    standard:
      cost_factor: 10
      intelligence_range: [14, 16]
      models:
        - provider: openai
          model: gpt-4o
        - provider: anthropic
          model: claude-sonnet-4-6
        - provider: google
          model: gemini-2.5-pro
      use_cases:
        - logic_design
        - stage2_evaluation
        - refactoring
    frontier:
      cost_factor: 30
      intelligence_range: [18, 20]
      models:
        - provider: openai
          model: o3
        - provider: anthropic
          model: claude-opus-4-6
      use_cases:
        - consensus
        - lateral_thinking
        - big_bang
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `default_tier` | `"frugal"` \| `"standard"` \| `"frontier"` | `"frugal"` | The starting tier used when no task-specific override applies. |
| `escalation_threshold` | `int >= 1` | `2` | The retry attempt at which tier escalation begins. From this attempt onward the tier climbs one notch per retry (progressive), capped at the frontier tier. Top-level and untrusted decomposed work start at the base tier. Only a decomposed child with explicit trust authorization starts one tier lower, so that trusted-child ladder may require one additional retry to reach the same frontier ceiling. Current live decomposition supplies no such trust authorization. |
| `downgrade_success_streak` | `int >= 1` | `5` | Number of consecutive successes at the current tier before downgrading to the previous tier. |
| `tiers` | `dict[str, TierConfig]` | (see above) | Tier definitions keyed by name. |

**`TierConfig` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `cost_factor` | `int >= 1` | Relative cost multiplier (1 = frugal, 10 = standard, 30 = frontier). |
| `intelligence_range` | `[int, int]` | Min/max intelligence score for this tier (min must be ≤ max). |
| `models` | `list[ModelConfig]` | Models available in this tier. |
| `use_cases` | `list[str]` | Descriptive tags for which task types this tier is suited for. |

**`ModelConfig` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `provider` | `string` | Provider name (`openai`, `anthropic`, `google`, `openrouter`). |
| `model` | `string` | Model identifier (e.g., `gpt-4o-mini`, `claude-opus-4-6`). |

---

## `clarification`

Controls Phase 0 — the Socratic Interview and seed generation.

```yaml
clarification:
  ambiguity_threshold: 0.2    # Interview completes when ambiguity score <= this value
  max_interview_rounds: 10    # Hard ceiling on clarification rounds
  model_tier: standard        # "frugal" | "standard" | "frontier"
  default_model: claude-opus-4-6
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `ambiguity_threshold` | `float [0.0, 1.0]` | `0.2` | Maximum ambiguity score to allow seed generation to proceed. Interview loops until the score falls at or below this value. |
| `max_interview_rounds` | `int >= 1` | `10` | Maximum number of question-answer rounds regardless of ambiguity score. |
| `model_tier` | `"frugal"` \| `"standard"` \| `"frontier"` | `"standard"` | PAL tier used for the clarification phase. |
| `default_model` | `string` | `"claude-opus-4-6"` | Default model for interview and seed generation. Overridable via `OUROBOROS_CLARIFICATION_MODEL`. |

---

## `execution`

Controls Phase 2 — the Double Diamond execution loop.

```yaml
execution:
  max_iterations_per_ac: 10   # Maximum execution iterations per acceptance criterion
  retrospective_interval: 3   # Iterations between automatic retrospectives
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `max_iterations_per_ac` | `int >= 1` | `10` | Maximum number of execution iterations for a single acceptance criterion before the system escalates or declares failure. |
| `retrospective_interval` | `int >= 1` | `3` | Number of iterations between automatic retrospective evaluations. |

---

## `resilience`

Controls Phase 3 — stagnation detection and lateral thinking.

```yaml
resilience:
  stagnation_enabled: true
  lateral_thinking_enabled: true
  lateral_model_tier: frontier   # "frugal" | "standard" | "frontier"
  lateral_temperature: 0.8
  wonder_model: claude-opus-4-6
  reflect_model: claude-opus-4-6
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `stagnation_enabled` | `bool` | `true` | Whether stagnation detection is active. When `false`, the system does not check for SPINNING / OSCILLATION / NO_DRIFT / DIMINISHING_RETURNS patterns. |
| `lateral_thinking_enabled` | `bool` | `true` | Whether lateral thinking persona rotation is active when stagnation is detected. |
| `lateral_model_tier` | `"frugal"` \| `"standard"` \| `"frontier"` | `"frontier"` | PAL tier used for lateral thinking calls. Frontier is the default because creative re-framing requires high model capability. |
| `lateral_temperature` | `float [0.0, 2.0]` | `0.8` | LLM sampling temperature for lateral thinking prompts. Higher values produce more divergent outputs. |
| `wonder_model` | `string` | `"claude-opus-4-6"` | Model for the Wonder phase (divergent exploration). Overridable via `OUROBOROS_WONDER_MODEL`. |
| `reflect_model` | `string` | `"claude-opus-4-6"` | Model for the Reflect phase (convergent synthesis). Overridable via `OUROBOROS_REFLECT_MODEL`. |

---

## `evaluation`

Controls Phase 4 — the 3-stage evaluation pipeline.

```yaml
evaluation:
  stage1_enabled: true         # Mechanical checks (lint, build, tests)
  stage2_enabled: true         # Semantic evaluation (AC compliance, drift)
  stage3_enabled: true         # Multi-model consensus (when triggered)
  satisfaction_threshold: 0.8  # Minimum semantic satisfaction score to pass
  uncertainty_threshold: 0.3   # Uncertainty score above which consensus is triggered
  semantic_model: claude-opus-4-6
  assertion_extraction_model: claude-sonnet-4-6
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `stage1_enabled` | `bool` | `true` | Enable mechanical checks (lint, build, test, static analysis). When `false`, skipped entirely — use only for debugging. |
| `stage2_enabled` | `bool` | `true` | Enable semantic evaluation (AC compliance, goal alignment, drift scoring). |
| `stage3_enabled` | `bool` | `true` | Enable multi-model consensus evaluation (triggered by the consensus trigger matrix). |
| `satisfaction_threshold` | `float [0.0, 1.0]` | `0.8` | Minimum semantic satisfaction score required to pass Stage 2 without triggering Stage 3. |
| `uncertainty_threshold` | `float [0.0, 1.0]` | `0.3` | Semantic uncertainty score above which Stage 3 consensus is triggered even if `satisfaction_threshold` is met. |
| `semantic_model` | `string` | `"claude-opus-4-6"` | Model used for Stage 2 semantic evaluation. Overridable via `OUROBOROS_SEMANTIC_MODEL`. |
| `assertion_extraction_model` | `string` | `"claude-sonnet-4-6"` | Model used for extracting verification assertions from seed criteria. Overridable via `OUROBOROS_ASSERTION_EXTRACTION_MODEL`. |

---

## `consensus`

Controls Phase 5 — multi-model consensus voting and deliberation.

```yaml
consensus:
  min_models: 3
  threshold: 0.67           # Fraction of models that must agree (2/3 majority)
  diversity_required: true  # Require models from different providers
  models:
    - openrouter/openai/gpt-4o
    - openrouter/anthropic/claude-opus-4-6
    - openrouter/google/gemini-2.5-pro
  advocate_model: openrouter/anthropic/claude-opus-4-6
  devil_model: openrouter/openai/gpt-4o
  judge_model: openrouter/google/gemini-2.5-pro
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `min_models` | `int >= 2` | `3` | Minimum number of models required for a consensus vote. |
| `threshold` | `float [0.0, 1.0]` | `0.67` | Fraction of models that must agree for consensus to pass (e.g., `0.67` = 2/3 majority). |
| `diversity_required` | `bool` | `true` | When `true`, consensus requires models from at least two different providers. |
| `models` | `list[string]` | (see above) | Model roster for Stage 3 simple voting. With `llm.backend: litellm`, use `provider/model` or `openrouter/provider/model`. With `llm.backend: codex`, use Codex/OpenAI model IDs such as `gpt-5.4`. Overridable via `OUROBOROS_CONSENSUS_MODELS` (comma-separated). |
| `advocate_model` | `string` | `"openrouter/anthropic/claude-opus-4-6"` | Model that argues in favor of the proposed solution in deliberative consensus. With `llm.backend: codex`, this can be a Codex/OpenAI model ID such as `gpt-5.4`. Overridable via `OUROBOROS_CONSENSUS_ADVOCATE_MODEL`. |
| `devil_model` | `string` | `"openrouter/openai/gpt-4o"` | Model that argues against (devil's advocate) in deliberative consensus. With `llm.backend: codex`, this can be a Codex/OpenAI model ID such as `gpt-5.4`. Overridable via `OUROBOROS_CONSENSUS_DEVIL_MODEL`. |
| `judge_model` | `string` | `"openrouter/google/gemini-2.5-pro"` | Model that renders a final verdict after deliberation. With `llm.backend: codex`, this can be a Codex/OpenAI model ID such as `gpt-5.4`. Overridable via `OUROBOROS_CONSENSUS_JUDGE_MODEL`. |

> **Backend note:** With `llm.backend: litellm`, consensus models typically go through OpenRouter/LiteLLM and require the corresponding provider credentials (commonly `OPENROUTER_API_KEY`). With `llm.backend: codex`, the configured model strings are sent through Codex CLI instead.

---

## `persistence`

Controls the SQLite event store.

```yaml
persistence:
  enabled: true
  database_path: data/ouroboros.db   # Relative to ~/.ouroboros/
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | `bool` | `true` | Whether event sourcing is active. Setting to `false` disables all persistence — not recommended for production use. |
| `database_path` | `string` | `"data/ouroboros.db"` | **Currently not honored by the EventStore.** The `EventStore` uses a hardcoded default of `~/.ouroboros/ouroboros.db` regardless of this value. This config key is reserved for a future configurable path feature. The TUI `--db-path` option also defaults to `~/.ouroboros/ouroboros.db`. |

---

## `drift`

Controls drift monitoring thresholds. Drift measures how far execution has strayed from the original seed (goal + constraint + ontology weighted formula).

```yaml
drift:
  warning_threshold: 0.3    # Drift score that triggers a warning
  critical_threshold: 0.5   # Drift score that triggers intervention
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `warning_threshold` | `float [0.0, 1.0]` | `0.3` | Drift score above which a warning event is emitted. |
| `critical_threshold` | `float [0.0, 1.0]` | `0.5` | Drift score above which the system triggers a critical intervention (re-alignment step). Must be ≥ `warning_threshold`. |

---

## `runtime_controls`

Controls long-running MCP/evolution liveness. These defaults are intended for normal local use: complex productive generations can run for hours, silent hangs are bounded, and repeated activity without material progress is eventually stopped.

```yaml
runtime_controls:
  mcp_tool_timeout_seconds: 0                 # 0 = no adapter wall-clock cap
  generation_idle_timeout_seconds: 7200       # No EventStore activity for 2 hours
  generation_no_progress_timeout_seconds: 14400  # Activity but no material progress for 4 hours
  generation_safety_timeout_seconds: 0        # Optional final hard cap; 0 = disabled
  watchdog_poll_seconds: 15.0
```

Material progress is stricter than liveness. Heartbeats, messages, and tool calls keep the generation from being considered idle; phase changes, workflow status changes, stage/subtask completion, and terminal execution events reset the no-progress timer.

Recommended tuning examples:

```yaml
# Long-running local work
runtime_controls:
  generation_idle_timeout_seconds: 7200
  generation_no_progress_timeout_seconds: 43200

# Strict CI / bounded automation
runtime_controls:
  generation_idle_timeout_seconds: 900
  generation_no_progress_timeout_seconds: 3600
  generation_safety_timeout_seconds: 14400
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `mcp_tool_timeout_seconds` | `float >= 0` | `0` | Adapter-level MCP timeout for progress-aware tools such as `ouroboros_evolve_step`. Keep `0` for normal use so the watchdog, not wall-clock time, decides liveness. |
| `generation_idle_timeout_seconds` | `float >= 0` | `7200` | Timeout when no lineage/execution activity is observed. `0` disables idle detection. |
| `generation_no_progress_timeout_seconds` | `float >= 0` | `14400` | Timeout when activity continues but material progress does not. `0` disables no-progress detection. |
| `generation_safety_timeout_seconds` | `float >= 0` | `0` | Optional final hard cap for a generation. `0` disables the hard cap. |
| `watchdog_poll_seconds` | `float > 0` | `15.0` | EventStore polling interval for generation watchdog decisions. |

---

## `logging`

Controls log output.

```yaml
logging:
  level: info                      # "debug" | "info" | "warning" | "error"
  log_path: logs/ouroboros.log     # Relative to ~/.ouroboros/
  include_reasoning: true
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `level` | `"debug"` \| `"info"` \| `"warning"` \| `"error"` | `"info"` | Minimum log level. Set to `"debug"` for verbose output. |
| `log_path` | `string` | `"logs/ouroboros.log"` | Path to the log file, relative to `~/.ouroboros/`. The resolved absolute path is `~/.ouroboros/logs/ouroboros.log`. |
| `include_reasoning` | `bool` | `true` | Whether to log LLM reasoning traces. Disable to reduce log volume when reasoning output is not needed. |

---

## `credentials.yaml`

API keys are stored separately from the main config. This file is created with `chmod 600` permissions by `ouroboros config init`.

```yaml
# ~/.ouroboros/credentials.yaml
providers:
  openrouter:
    api_key: YOUR_OPENROUTER_API_KEY
    base_url: https://openrouter.ai/api/v1
  openai:
    api_key: YOUR_OPENAI_API_KEY
  anthropic:
    api_key: YOUR_ANTHROPIC_API_KEY
  google:
    api_key: YOUR_GOOGLE_API_KEY
```

**Alternative — environment variables (recommended for CI/CD):**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export OPENROUTER_API_KEY="sk-or-..."
```

Environment variables take precedence over `credentials.yaml`.

---

## Environment Variables

All environment variables have higher priority than the corresponding `config.yaml` value.

### Runtime / Backend

| Variable | Overrides | Description |
|----------|-----------|-------------|
| `OUROBOROS_AGENT_RUNTIME` | `orchestrator.runtime_backend` | Active runtime backend (`claude`, `codex`, `opencode`, `hermes`, `gemini`, `kiro`, `copilot`, `pi`). |
| `OUROBOROS_AGENT_PERMISSION_MODE` | `orchestrator.permission_mode` | Stored permission preference; runner-driven seed execution forces the native `bypassPermissions` equivalent for fresh and resumed dispatches on approval-aware backends. Pi and GJC have no separate approval flag. |
| `OUROBOROS_OPENCODE_PERMISSION_MODE` | `orchestrator.opencode_permission_mode` | Stored OpenCode preference. Seed execution still forces `bypassPermissions`, translated to `--dangerously-skip-permissions`. |
| `OUROBOROS_MODEL_TIER_ROUTING` | _(routing kill switch)_ | Model-tier routing is enabled by default. Set to `0`, `off`, or `false` (case- and whitespace-insensitive) to disable routing and emit no routing events. |
| `OUROBOROS_SHADOW_REPLAY` | _(experiment arm)_ | Default OFF. Only `1`, `true`, or `on` arms the opt-in shadow-baseline harness. Current live decompositions lack deterministic MECE attestation and are skipped before baseline model dispatch; bundled runtimes also lack the complete filesystem/external-effect isolation attestation, so production emits no shadow baseline today. |
| `OUROBOROS_MAX_PARALLEL_WORKERS` | `orchestrator.max_parallel_workers` | Requested maximum concurrent Acceptance Criteria workers for parallel execution. Must be a positive integer. |
| `OUROBOROS_MAX_CONCURRENCY` | _(fan-out cap)_ | Caps the effective delivery fan-out for the connected backend, overriding Ouroboros' backend-aware default. Use it to raise CLI runtimes (`hermes`, `codex`, ...) above their serialized-by-default ceiling of 1. Must be a positive integer; blank/invalid values are ignored so the safety cap is never silently disabled. |
| `OUROBOROS_<BACKEND>_RPM` | _(rate budget)_ | Per-backend requests-per-minute ceiling for the shared dispatch rate bucket. `<BACKEND>` is the runtime name (the same value you set for `runtime_backend` / `OUROBOROS_AGENT_RUNTIME`) upper-cased with non-alphanumerics collapsed to `_` (e.g. `OUROBOROS_HERMES_RPM`, `OUROBOROS_CODEX_RPM`, `OUROBOROS_OPENCODE_RPM`). Internal `*_cli` adapter handles (`hermes_cli`, `codex_cli`, `gemini_cli`, `copilot_cli`) canonicalize to these names, so the user-facing key always applies. Dormant unless set — declaring it is how you safely raise `OUROBOROS_MAX_CONCURRENCY` on a CLI runtime. Must be a positive integer; blank/invalid values are ignored. |
| `OUROBOROS_<BACKEND>_TPM` | _(rate budget)_ | Per-backend tokens-per-minute ceiling for the shared dispatch rate bucket (same naming as `_RPM`). Dormant unless set. Must be a positive integer; blank/invalid values are ignored. |
| `OUROBOROS_BACKEND_LIMITS` | _(config path)_ | Path to the backend-limits YAML file (default `~/.ouroboros/backend_limits.yaml`). See [Backend concurrency & rate limits](#backend-concurrency--rate-limits). |
| `OUROBOROS_CLI_PATH` | `orchestrator.cli_path` | Path to the Claude CLI binary. |
| `OUROBOROS_CODEX_CLI_PATH` | `orchestrator.codex_cli_path` | Path to the Codex CLI binary. |
| `OUROBOROS_OPENCODE_CLI_PATH` | `orchestrator.opencode_cli_path` | Path to the OpenCode CLI binary. |
| `OUROBOROS_PI_CLI_PATH` | `orchestrator.pi_cli_path` | Path to the Pi CLI binary. |
| `OUROBOROS_OUROCODE_CLI_PATH` | `orchestrator.ourocode_cli_path` | Path to the ourocode CLI binary used by the LLM-only `ourocode` backend. |
| `OUROBOROS_SKIP_VERSION_CHECK` | *(none)* | Controls the Claude Agent SDK per-call version compatibility check. Defaults to `"1"` (skip the check, saving ~0.3-0.8 s per LLM call). Set to `"0"` to re-enable the check for debugging version-mismatch issues. Maps to `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK` internally. |

### LLM Flow

| Variable | Overrides | Description |
|----------|-----------|-------------|
| `OUROBOROS_LLM_BACKEND` | `llm.backend` | Default LLM backend for non-agent flows. |
| `OUROBOROS_LLM_PERMISSION_MODE` | `llm.permission_mode` | Permission mode for LLM flows. |
| `OUROBOROS_QA_MODEL` | `llm.qa_model` | Model for post-execution QA. |
| `OUROBOROS_DEPENDENCY_ANALYSIS_MODEL` | `llm.dependency_analysis_model` | Model for AC dependency analysis. |
| `OUROBOROS_ONTOLOGY_ANALYSIS_MODEL` | `llm.ontology_analysis_model` | Model for ontological analysis. |
| `OUROBOROS_CONTEXT_COMPRESSION_MODEL` | `llm.context_compression_model` | Model for context compression. |

### Phase Models

| Variable | Overrides | Description |
|----------|-----------|-------------|
| `OUROBOROS_CLARIFICATION_MODEL` | `clarification.default_model` | Model for interview and seed generation. |
| `OUROBOROS_WONDER_MODEL` | `resilience.wonder_model` | Model for the Wonder phase. |
| `OUROBOROS_REFLECT_MODEL` | `resilience.reflect_model` | Model for the Reflect phase. |
| `OUROBOROS_SEMANTIC_MODEL` | `evaluation.semantic_model` | Model for Stage 2 semantic evaluation. |
| `OUROBOROS_ASSERTION_EXTRACTION_MODEL` | `evaluation.assertion_extraction_model` | Model for assertion extraction. |
| `OUROBOROS_CONSENSUS_MODELS` | `consensus.models` | Comma-separated model roster for Stage 3 voting. |
| `OUROBOROS_CONSENSUS_ADVOCATE_MODEL` | `consensus.advocate_model` | Advocate model for deliberative consensus. |
| `OUROBOROS_CONSENSUS_DEVIL_MODEL` | `consensus.devil_model` | Devil's advocate model for deliberative consensus. |
| `OUROBOROS_CONSENSUS_JUDGE_MODEL` | `consensus.judge_model` | Judge model for deliberative consensus. |

### MCP Evolution

| Variable | Default | Description |
|----------|---------|-------------|
| `OUROBOROS_EXECUTION_MODEL` | `null` (runtime default) | Model used for agent execution inside the MCP evolve loop. Only applicable when the Claude runtime is active. |
| `OUROBOROS_VALIDATION_MODEL` | `null` (runtime default) | Model used for import/validation fix passes during MCP evolution. Only applicable when the Claude runtime is active. |
| `OUROBOROS_EVOLVE_STAGE1` | `"false"` | Set to `"true"` to enable Stage 1 mechanical checks (lint/build/test) during MCP evolution. |
| `OUROBOROS_MCP_TOOL_TIMEOUT_SECONDS` | `runtime_controls.mcp_tool_timeout_seconds` | Adapter-level MCP timeout for progress-aware tools. `0` disables the wall-clock cap. |
| `OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS` | `runtime_controls.generation_idle_timeout_seconds` | Idle timeout when no generation/execution activity is observed. |
| `OUROBOROS_GENERATION_NO_PROGRESS_TIMEOUT_SECONDS` | `runtime_controls.generation_no_progress_timeout_seconds` | Timeout when activity continues without material progress. |
| `OUROBOROS_GENERATION_SAFETY_TIMEOUT_SECONDS` | `runtime_controls.generation_safety_timeout_seconds` | Optional final hard cap for one generation. |
| `OUROBOROS_WATCHDOG_POLL_SECONDS` | `runtime_controls.watchdog_poll_seconds` | EventStore polling interval for watchdog decisions. |
| `OUROBOROS_GENERATION_TIMEOUT` | legacy alias | Backwards-compatible alias for `generation_no_progress_timeout_seconds`. It no longer creates a separate hard 2-hour MCP adapter timeout. Prefer `runtime_controls` in `config.yaml` for persistent tuning. |

### Observability & Agents

| Variable | Default | Description |
|----------|---------|-------------|
| `OUROBOROS_LOG_MODE` | `"dev"` | Logging output format. `"dev"` = human-readable console output; `"prod"` = structured JSON (suitable for log aggregation). |
| `OUROBOROS_AGENTS_DIR` | `null` | Path to a directory of custom agent `.md` prompt files. When set, overrides the bundled agents from the installed package. Useful for developing custom agent personas without reinstalling. |
| `OUROBOROS_WEB_SEARCH_TOOL` | `""` | MCP tool name to use for web search during the Big Bang interview (e.g., `mcp__tavily__search`). An empty string disables web-augmented interview. Only applicable when running with an MCP-capable host. |

### API Keys

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude models). |
| `OPENAI_API_KEY` | OpenAI API key (Codex CLI, GPT models). |
| `GOOGLE_API_KEY` | Google API key (Gemini models used in `frugal` and `standard` tiers). |
| `OPENROUTER_API_KEY` | OpenRouter API key (multi-provider model access for consensus). |

---

## Backend concurrency & rate limits

Ouroboros plans delivery fan-out — the parallel execution of acceptance criteria — and is
responsible for keeping it within the connected LLM backend's concurrency and rate limits
rather than relying on the agent runtime to throttle itself. Two independent levers govern this,
and **every value is configurable without source-level changes**:

- **Fan-out concurrency** (`max_concurrency`): how many acceptance criteria dispatch in
  parallel. The native Claude backend is uncapped here (it is paced by its own RPM/TPM bucket);
  every CLI runtime whose underlying LLM limits are unknown is **serialized to 1 by default**.
- **Rate budget** (`requests_per_minute` / `tokens_per_minute`): a shared sliding-window bucket
  that paces dispatch across all concurrent workers. For non-Claude runtimes it is **dormant
  until you declare a budget** — declaring one is what makes raising the fan-out cap safe.

### Resolution precedence

Each dimension is resolved independently, highest precedence first:

1. **Environment variables** — `OUROBOROS_MAX_CONCURRENCY` (cap, any backend) and per-backend
   `OUROBOROS_<BACKEND>_RPM` / `OUROBOROS_<BACKEND>_TPM`.
2. **Config file** — `~/.ouroboros/backend_limits.yaml` (path overridable via
   `OUROBOROS_BACKEND_LIMITS`).
3. **Built-in registry** — Claude's ceilings; otherwise the serialize-by-default cap of 1.

The config file is loaded lazily, cached by mtime (edits apply without a restart), and is
fully fault-tolerant: a missing, malformed, or non-regular file is ignored and resolution falls
back to the registry. Backend keys are canonicalized, so aliases (`anthropic`, `claude_code`)
map to `claude`. Only positive integers are honored; `0`/negative/blank values are ignored so a
typo never silently disables a safety limit.

### `~/.ouroboros/backend_limits.yaml`

```yaml
# Fallback applied to any backend without its own entry below.
default:
  max_concurrency: 1

backends:
  # Override the native Claude ceilings without touching source.
  claude:
    requests_per_minute: 40
    tokens_per_minute: 32000

  # Declare a safe budget for a CLI runtime, then raise its fan-out cap
  # (via OUROBOROS_MAX_CONCURRENCY) knowing dispatch will be paced.
  # Use the runtime name you select with `runtime_backend` (hermes, codex,
  # gemini, copilot, opencode, goose, pi, kiro, gjc) — the `*_cli` adapter handles
  # canonicalize to these, so either form resolves to the same entry.
  hermes:
    max_concurrency: 4
    requests_per_minute: 20
    tokens_per_minute: 60000
```

A flat top-level mapping (backend → fields, without the `backends:` wrapper) is also accepted.

---

## Minimal Config Examples

### Claude Code Runtime (recommended default)

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: claude

logging:
  level: info
```

### Codex CLI Runtime

```yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex   # omit if codex is already on PATH

llm:
  backend: codex

logging:
  level: info
```

### Codex CLI Runtime With Explicit Role Overrides

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex

llm:
  backend: codex
  qa_model: gpt-5.4

clarification:
  default_model: gpt-5.4

evaluation:
  semantic_model: gpt-5.4

consensus:
  advocate_model: gpt-5.4
  devil_model: gpt-5.4
  judge_model: gpt-5.4
```

This is the recommended Ouroboros-side pattern for Codex users. Keep `~/.codex/config.toml` limited to the MCP/env block created by setup.

### OpenCode Runtime

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: opencode
  opencode_cli_path: /usr/local/bin/opencode   # omit if opencode is already on PATH

llm:
  backend: opencode

logging:
  level: info
```

OpenCode supports multiple model providers (Anthropic, OpenAI, Google, and others). Model selection is configured in OpenCode itself (`~/.config/opencode/opencode.jsonc` or `opencode.json`), not in `config.yaml`. The `orchestrator.opencode_permission_mode` defaults to `bypassPermissions` since OpenCode runs non-interactively via `opencode run --format json`. The `llm.opencode_permission_mode` defaults to `acceptEdits`, but the factory forces `bypassPermissions` for interview/seed use cases to avoid CLI sandbox blocking.

### GitHub Copilot CLI Runtime

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: copilot
  copilot_cli_path: null                   # omit if `copilot` is already on PATH

llm:
  backend: copilot
  default_model: claude-opus-4.6           # written by `ouroboros setup --runtime copilot`

clarification:
  default_model: claude-opus-4.6           # same value written by setup
```

The Copilot CLI runtime is unique in that `ouroboros setup --runtime copilot` **live-discovers the available models** from the GitHub Copilot models API at setup time and writes the chosen default into the config above. Re-run setup after GitHub publishes new models. Authentication uses `gh auth login`; no separate API key is required. Hyphenated Anthropic IDs (for example `claude-opus-4-6`) used elsewhere in your config are auto-mapped to the dotted Copilot form (`claude-opus-4.6`) at runtime, so existing per-role overrides keep working when you switch backends. See [Copilot CLI runtime guide](runtime-guides/copilot.md) for full details.

### Pi CLI Runtime

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: pi
  pi_cli_path: null                       # omit if `pi` is already on PATH

llm:
  backend: pi
```

Pi is available as an agent runtime backend and, when the Pi LLM adapter is installed, an LLM-only backend for interview, ambiguity scoring, seed-extraction, and structured JSON flows. `ouroboros setup --runtime pi` records the Pi executable and installs a managed Pi extension at `~/.pi/agent/extensions/ouroboros-ooo-bridge.ts`, so interactive Pi/roach-pi sessions can route exact-prefix `ooo ...` input back into Ouroboros after Pi restart or `/reload`. The Pi LLM adapter supports structured `response_format` requests through prompt-level JSON/schema instructions plus adapter-side extraction and validation; Pi does not expose a Codex-style native `--output-schema` hard-enforcement flag. The runtime uses documented JSON mode (`pi --mode json <prompt>`) and preserves Pi native session IDs for targeted resume.

### Full Config Skeleton

```yaml
orchestrator:
  runtime_backend: claude
  permission_mode: acceptEdits
  opencode_permission_mode: bypassPermissions
  max_parallel_workers: 3
  cli_path: null
  codex_cli_path: null
  opencode_cli_path: null
  pi_cli_path: null
  ourocode_cli_path: null
  default_max_turns: 10

llm:
  backend: claude_code
  permission_mode: default
  opencode_permission_mode: acceptEdits
  qa_model: claude-sonnet-4-6
  dependency_analysis_model: claude-sonnet-4-6
  ontology_analysis_model: claude-sonnet-4-6
  context_compression_model: gpt-4

economics:
  default_tier: frugal
  escalation_threshold: 2
  downgrade_success_streak: 5
  tiers:
    frugal:
      cost_factor: 1
      intelligence_range: [9, 11]
      models:
        - provider: openai
          model: gpt-4o-mini
        - provider: google
          model: gemini-2.0-flash
        - provider: anthropic
          model: claude-3-5-haiku
      use_cases: [routine_coding, log_analysis, stage1_fix]
    standard:
      cost_factor: 10
      intelligence_range: [14, 16]
      models:
        - provider: openai
          model: gpt-4o
        - provider: anthropic
          model: claude-sonnet-4-6
        - provider: google
          model: gemini-2.5-pro
      use_cases: [logic_design, stage2_evaluation, refactoring]
    frontier:
      cost_factor: 30
      intelligence_range: [18, 20]
      models:
        - provider: openai
          model: o3
        - provider: anthropic
          model: claude-opus-4-6
      use_cases: [consensus, lateral_thinking, big_bang]

clarification:
  ambiguity_threshold: 0.2
  max_interview_rounds: 10
  model_tier: standard
  default_model: claude-opus-4-6

execution:
  max_iterations_per_ac: 10
  retrospective_interval: 3

resilience:
  stagnation_enabled: true
  lateral_thinking_enabled: true
  lateral_model_tier: frontier
  lateral_temperature: 0.8
  wonder_model: claude-opus-4-6
  reflect_model: claude-opus-4-6

evaluation:
  stage1_enabled: true
  stage2_enabled: true
  stage3_enabled: true
  satisfaction_threshold: 0.8
  uncertainty_threshold: 0.3
  semantic_model: claude-opus-4-6
  assertion_extraction_model: claude-sonnet-4-6

consensus:
  min_models: 3
  threshold: 0.67
  diversity_required: true
  models:
    - openrouter/openai/gpt-4o
    - openrouter/anthropic/claude-opus-4-6
    - openrouter/google/gemini-2.5-pro
  advocate_model: openrouter/anthropic/claude-opus-4-6
  devil_model: openrouter/openai/gpt-4o
  judge_model: openrouter/google/gemini-2.5-pro

persistence:
  enabled: true
  database_path: data/ouroboros.db

drift:
  warning_threshold: 0.3
  critical_threshold: 0.5

runtime_controls:
  mcp_tool_timeout_seconds: 0
  generation_idle_timeout_seconds: 7200
  generation_no_progress_timeout_seconds: 14400
  generation_safety_timeout_seconds: 0
  watchdog_poll_seconds: 15.0

logging:
  level: info
  log_path: logs/ouroboros.log
  include_reasoning: true
```
