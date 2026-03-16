<!--
doc_metadata:
  runtime_scope: [local, claude, codex]
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
| `persistence` | `PersistenceConfig` | SQLite event store settings |
| `drift` | `DriftConfig` | Drift monitoring thresholds |
| `logging` | `LoggingConfig` | Log level, path, and verbosity |

---

## `orchestrator`

Controls how Ouroboros launches and communicates with the agent runtime backend.

```yaml
orchestrator:
  runtime_backend: claude       # "claude" | "codex" | "opencode"
  permission_mode: acceptEdits  # "default" | "acceptEdits" | "bypassPermissions"
  opencode_permission_mode: bypassPermissions
  cli_path: null                # Path to Claude CLI binary; null = use SDK default
  codex_cli_path: null          # Path to Codex CLI binary; null = resolve from PATH
  opencode_cli_path: null       # Path to OpenCode CLI binary; null = resolve from PATH
  default_max_turns: 10
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `runtime_backend` | `"claude"` \| `"codex"` \| `"opencode"` | `"claude"` | The agent runtime backend used for workflow execution. Overridable via `OUROBOROS_AGENT_RUNTIME`. |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"acceptEdits"` | Permission mode for Claude and Codex runtimes. Overridable via `OUROBOROS_AGENT_PERMISSION_MODE`. |
| `opencode_permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"bypassPermissions"` | Permission mode when using the OpenCode runtime. Overridable via `OUROBOROS_OPENCODE_PERMISSION_MODE`. |
| `cli_path` | `string \| null` | `null` | Absolute path to the Claude CLI binary (`~` is expanded). When `null`, the SDK-bundled CLI is used. Overridable via `OUROBOROS_CLI_PATH`. |
| `codex_cli_path` | `string \| null` | `null` | Absolute path to the Codex CLI binary (`~` is expanded). When `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_CODEX_CLI_PATH`. |
| `opencode_cli_path` | `string \| null` | `null` | Absolute path to the OpenCode CLI binary (`~` is expanded). When `null`, resolved from `PATH` at runtime. Overridable via `OUROBOROS_OPENCODE_CLI_PATH`. |
| `default_max_turns` | `int >= 1` | `10` | Default maximum number of turns per agent execution task. |

> **OpenCode scope note:** `opencode` runtime is out of scope for Claude and Codex documentation. The `opencode_*` options are listed here for completeness; consult the OpenCode-specific guide if available.

---

## `llm`

Defaults for LLM-only flows (interview, seed generation, QA, analysis). The `orchestrator` section governs agent runtime execution; the `llm` section governs model-level LLM calls within the orchestration pipeline.

```yaml
llm:
  backend: claude_code
  permission_mode: default
  opencode_permission_mode: acceptEdits
  qa_model: claude-sonnet-4-20250514
  dependency_analysis_model: claude-opus-4-6
  ontology_analysis_model: claude-opus-4-6
  context_compression_model: gpt-4
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `backend` | `"claude"` \| `"claude_code"` \| `"litellm"` \| `"codex"` \| `"opencode"` | `"claude_code"` | Default backend for LLM-only flows. Overridable via `OUROBOROS_LLM_BACKEND`. |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"default"` | Permission mode for non-OpenCode LLM flows. Overridable via `OUROBOROS_LLM_PERMISSION_MODE`. |
| `opencode_permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | `"acceptEdits"` | Permission mode for OpenCode-backed LLM flows. Overridable via `OUROBOROS_OPENCODE_PERMISSION_MODE`. |
| `qa_model` | `string` | `"claude-sonnet-4-20250514"` | Model used for post-execution QA verdict generation. Overridable via `OUROBOROS_QA_MODEL`. |
| `dependency_analysis_model` | `string` | `"claude-opus-4-6"` | Model used for AC dependency analysis. Overridable via `OUROBOROS_DEPENDENCY_ANALYSIS_MODEL`. |
| `ontology_analysis_model` | `string` | `"claude-opus-4-6"` | Model used for ontological analysis. Overridable via `OUROBOROS_ONTOLOGY_ANALYSIS_MODEL`. |
| `context_compression_model` | `string` | `"gpt-4"` | Model used for workflow context compression. Overridable via `OUROBOROS_CONTEXT_COMPRESSION_MODEL`. |

---

## `economics`

Configures the PAL Router (Progressive Adaptive LLM): cost tiers, escalation on failure, and downgrade on success.

```yaml
economics:
  default_tier: frugal          # "frugal" | "standard" | "frontier"
  escalation_threshold: 2       # Consecutive failures before upgrading tier
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
| `escalation_threshold` | `int >= 1` | `2` | Number of consecutive failures at the current tier before escalating to the next tier. |
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
  atomicity_model: claude-opus-4-6
  decomposition_model: claude-opus-4-6
  double_diamond_model: claude-opus-4-6
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `max_iterations_per_ac` | `int >= 1` | `10` | Maximum number of execution iterations for a single acceptance criterion before the system escalates or declares failure. |
| `retrospective_interval` | `int >= 1` | `3` | Number of iterations between automatic retrospective evaluations. |
| `atomicity_model` | `string` | `"claude-opus-4-6"` | Model used for atomicity analysis (deciding whether to decompose an AC). Overridable via `OUROBOROS_ATOMICITY_MODEL`. |
| `decomposition_model` | `string` | `"claude-opus-4-6"` | Model used for AC decomposition into child ACs. Overridable via `OUROBOROS_DECOMPOSITION_MODEL`. |
| `double_diamond_model` | `string` | `"claude-opus-4-6"` | Default model for Double Diamond phase prompts. Overridable via `OUROBOROS_DOUBLE_DIAMOND_MODEL`. |

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
| `models` | `list[string]` | (see above) | Model roster for Stage 3 simple voting. Specify as `provider/model` or `openrouter/provider/model`. Overridable via `OUROBOROS_CONSENSUS_MODELS` (comma-separated). |
| `advocate_model` | `string` | `"openrouter/anthropic/claude-opus-4-6"` | Model that argues in favor of the proposed solution in deliberative consensus. Overridable via `OUROBOROS_CONSENSUS_ADVOCATE_MODEL`. |
| `devil_model` | `string` | `"openrouter/openai/gpt-4o"` | Model that argues against (devil's advocate) in deliberative consensus. Overridable via `OUROBOROS_CONSENSUS_DEVIL_MODEL`. |
| `judge_model` | `string` | `"openrouter/google/gemini-2.5-pro"` | Model that renders a final verdict after deliberation. Overridable via `OUROBOROS_CONSENSUS_JUDGE_MODEL`. |

> **Note:** Consensus models are accessed via OpenRouter. Ensure `OPENROUTER_API_KEY` is set in `credentials.yaml` or as an environment variable when `stage3_enabled: true`.

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
| `OUROBOROS_AGENT_RUNTIME` | `orchestrator.runtime_backend` | Active runtime backend (`claude`, `codex`, `opencode`). |
| `OUROBOROS_AGENT_PERMISSION_MODE` | `orchestrator.permission_mode` | Permission mode for non-OpenCode runtimes. |
| `OUROBOROS_OPENCODE_PERMISSION_MODE` | `orchestrator.opencode_permission_mode` | Permission mode when using OpenCode runtime. |
| `OUROBOROS_CLI_PATH` | `orchestrator.cli_path` | Path to the Claude CLI binary. |
| `OUROBOROS_CODEX_CLI_PATH` | `orchestrator.codex_cli_path` | Path to the Codex CLI binary. |
| `OUROBOROS_OPENCODE_CLI_PATH` | `orchestrator.opencode_cli_path` | Path to the OpenCode CLI binary. |

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
| `OUROBOROS_ATOMICITY_MODEL` | `execution.atomicity_model` | Model for atomicity analysis. |
| `OUROBOROS_DECOMPOSITION_MODEL` | `execution.decomposition_model` | Model for AC decomposition. |
| `OUROBOROS_DOUBLE_DIAMOND_MODEL` | `execution.double_diamond_model` | Model for Double Diamond phases. |
| `OUROBOROS_WONDER_MODEL` | `resilience.wonder_model` | Model for the Wonder phase. |
| `OUROBOROS_REFLECT_MODEL` | `resilience.reflect_model` | Model for the Reflect phase. |
| `OUROBOROS_SEMANTIC_MODEL` | `evaluation.semantic_model` | Model for Stage 2 semantic evaluation. |
| `OUROBOROS_ASSERTION_EXTRACTION_MODEL` | `evaluation.assertion_extraction_model` | Model for assertion extraction. |
| `OUROBOROS_CONSENSUS_MODELS` | `consensus.models` | Comma-separated model roster for Stage 3 voting. |
| `OUROBOROS_CONSENSUS_ADVOCATE_MODEL` | `consensus.advocate_model` | Advocate model for deliberative consensus. |
| `OUROBOROS_CONSENSUS_DEVIL_MODEL` | `consensus.devil_model` | Devil's advocate model for deliberative consensus. |
| `OUROBOROS_CONSENSUS_JUDGE_MODEL` | `consensus.judge_model` | Judge model for deliberative consensus. |

### MCP Evolution

These variables are read by the MCP server adapter (`ouroboros-mcp`) and the evolutionary loop. They have **no** corresponding `config.yaml` key — env var is the only override mechanism.

| Variable | Default | Description |
|----------|---------|-------------|
| `OUROBOROS_EXECUTION_MODEL` | `null` (runtime default) | Model used for agent execution inside the MCP evolve loop. Only applicable when the Claude runtime is active. |
| `OUROBOROS_VALIDATION_MODEL` | `null` (runtime default) | Model used for import/validation fix passes during MCP evolution. Only applicable when the Claude runtime is active. |
| `OUROBOROS_EVOLVE_STAGE1` | `"false"` | Set to `"true"` to enable Stage 1 mechanical checks (lint/build/test) during MCP evolution. |
| `OUROBOROS_GENERATION_TIMEOUT` | **dual-usage** — see note | Per-generation timeout in seconds. **Note:** This variable controls two independent mechanisms with different hardcoded defaults: (1) `EvolutionConfig.generation_timeout_seconds` in `evolution/loop.py` uses default `"0"` (no loop-level timeout); (2) `EvolveStepTool.TIMEOUT_SECONDS` in `mcp/tools/definitions.py` uses default `"7200"` (2-hour MCP protocol-level timeout). Setting this variable to `"0"` disables the loop-level timeout only — the MCP-level timeout is unaffected. |

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

logging:
  level: info
```

### Full Config Skeleton

```yaml
orchestrator:
  runtime_backend: claude
  permission_mode: acceptEdits
  opencode_permission_mode: bypassPermissions
  cli_path: null
  codex_cli_path: null
  opencode_cli_path: null
  default_max_turns: 10

llm:
  backend: claude_code
  permission_mode: default
  opencode_permission_mode: acceptEdits
  qa_model: claude-sonnet-4-20250514
  dependency_analysis_model: claude-opus-4-6
  ontology_analysis_model: claude-opus-4-6
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
  atomicity_model: claude-opus-4-6
  decomposition_model: claude-opus-4-6
  double_diamond_model: claude-opus-4-6

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

logging:
  level: info
  log_path: logs/ouroboros.log
  include_reasoning: true
```
