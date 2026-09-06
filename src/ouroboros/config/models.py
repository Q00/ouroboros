"""Pydantic models for Ouroboros configuration.

This module defines the configuration schema using Pydantic v2.
All configuration validation happens through these models.

Classes:
    ModelConfig: Single LLM model configuration
    TierConfig: Tier configuration with cost factor and models
    ProviderCredentials: API credentials for a single provider
    CredentialsConfig: All provider credentials
    LLMConfig: Shared LLM backend/model defaults
    EconomicsConfig: Economic model with tier definitions
    ClarificationConfig: Phase 0 configuration
    ExecutionConfig: Phase 2 configuration
    ResilienceConfig: Phase 3 configuration
    EvaluationConfig: Phase 4 configuration
    ConsensusConfig: Phase 5 configuration
    PersistenceConfig: Storage configuration
    RuntimeControlsConfig: Long-running workflow timeout/progress controls
    LoggingConfig: Logging configuration
    OuroborosConfig: Top-level configuration combining all sections
"""

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
import yaml

from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
)
from ouroboros.orchestrator_stage import VALID_STAGE_KEYS

_SAFE_PROJECT_GUIDANCE_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")


class ModelConfig(BaseModel, frozen=True):
    """Configuration for a single LLM model.

    Attributes:
        provider: Provider name (openai, anthropic, google, openrouter)
        model: Model identifier string
    """

    provider: str
    model: str


class TierConfig(BaseModel, frozen=True):
    """Configuration for a cost tier.

    Attributes:
        cost_factor: Relative cost multiplier (1 for frugal, 10 for standard, etc.)
        intelligence_range: Tuple of min/max intelligence score
        models: List of models available in this tier
        use_cases: List of use cases this tier is suited for
    """

    cost_factor: int = Field(ge=1)
    intelligence_range: tuple[int, int] = Field(default=(1, 20))
    models: list[ModelConfig] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)

    @field_validator("intelligence_range")
    @classmethod
    def validate_intelligence_range(cls, v: tuple[int, int]) -> tuple[int, int]:
        """Validate that min <= max in intelligence range."""
        if v[0] > v[1]:
            msg = f"Intelligence range min ({v[0]}) must be <= max ({v[1]})"
            raise ValueError(msg)
        return v


class ProviderCredentials(BaseModel, frozen=True):
    """API credentials for a single provider.

    Attributes:
        api_key: The API key for the provider
        base_url: Optional custom base URL for the provider
    """

    api_key: str = Field(min_length=1)
    base_url: str | None = None


class CredentialsConfig(BaseModel, frozen=True):
    """Configuration for all provider credentials.

    Attributes:
        providers: Dict mapping provider name to credentials
    """

    providers: dict[str, ProviderCredentials] = Field(default_factory=dict)


class EconomicsConfig(BaseModel, frozen=True):
    """Economic model configuration.

    Attributes:
        default_tier: Default tier to use for tasks
        tiers: Dict mapping tier name to tier configuration
        escalation_threshold: Number of failures before upgrading tier
        downgrade_success_streak: Successes needed to downgrade tier
    """

    default_tier: Literal["frugal", "standard", "frontier"] = "frugal"
    tiers: dict[str, TierConfig] = Field(default_factory=dict)
    escalation_threshold: int = Field(default=2, ge=1)
    downgrade_success_streak: int = Field(default=5, ge=1)


class LLMConfig(BaseModel, frozen=True):
    """Shared LLM backend and model defaults.

    Attributes:
        backend: Default backend for LLM-only flows
        permission_mode: Default permission mode for local CLI-backed LLM flows
        opencode_permission_mode: Default permission mode for OpenCode-backed LLM flows
        qa_model: Default model for QA verdict generation
        dependency_analysis_model: Default model for AC dependency analysis
        ontology_analysis_model: Default model for ontological analysis
        context_compression_model: Default model for workflow context compression
    """

    backend: Literal[
        "claude",
        "claude_code",
        "litellm",
        "codex",
        "copilot",
        "gemini",
        "opencode",
        "kiro",
        "hermes",
        "goose",
        "pi",
        "omp",
        "ourocode",
        "dsh",
        "gjc",
        "zcode",
    ] = "claude_code"
    permission_mode: Literal["default", "acceptEdits", "bypassPermissions"] = "default"
    opencode_permission_mode: Literal["default", "acceptEdits", "bypassPermissions"] = "acceptEdits"
    qa_model: str = DEFAULT_SONNET_MODEL
    # Dependency and ontology analysis are bounded structured-extraction tasks;
    # Sonnet is sufficient and Opus is waste. (The "stable tier for reproducible
    # grading" rationale covers evaluation/consensus, not these.) Effort-first:
    # the floor should not start at the most expensive tier.
    dependency_analysis_model: str = DEFAULT_SONNET_MODEL
    ontology_analysis_model: str = DEFAULT_SONNET_MODEL
    context_compression_model: str = "gpt-4"


class LLMProviderProfileConfig(BaseModel, frozen=True):
    """Backend-specific overrides for an Ouroboros LLM task profile.

    ``profile`` is intentionally generic here: for Codex it maps to a
    ``codex exec --profile`` name, while other backends can ignore it and use
    portable fields such as ``model`` or ``temperature``.
    """

    profile: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_turns: int | None = Field(default=None, ge=1)
    # Provider-scoped mappings may use the Codex-native ``xhigh`` level.
    # Keeping it here (rather than in a generated Codex profile file) lets
    # Ouroboros apply it only to the selected Codex invocation.
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None


class LLMTaskProfileConfig(BaseModel, frozen=True):
    """Provider-neutral LLM task profile with optional backend overrides."""

    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_turns: int | None = Field(default=None, ge=1)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    providers: dict[str, LLMProviderProfileConfig] = Field(default_factory=dict)

    @field_validator("providers")
    @classmethod
    def validate_provider_reasoning_efforts(
        cls, providers: dict[str, LLMProviderProfileConfig]
    ) -> dict[str, LLMProviderProfileConfig]:
        """Keep Codex-only reasoning effort levels out of non-Codex providers."""
        for provider_name, provider_config in providers.items():
            if provider_config.reasoning_effort != "xhigh":
                continue
            if provider_name.strip().lower() not in {"codex", "codex_cli"}:
                msg = (
                    "reasoning_effort='xhigh' is only supported for Codex provider "
                    f"profiles, not {provider_name!r}"
                )
                raise ValueError(msg)
        return providers


class ClarificationConfig(BaseModel, frozen=True):
    """Phase 0 (Big Bang) configuration.

    Attributes:
        ambiguity_threshold: Maximum ambiguity score to proceed
        max_interview_rounds: Maximum number of clarification rounds
        model_tier: Tier to use for clarification
        default_model: Default LLM model for interview and seed generation
    """

    ambiguity_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    max_interview_rounds: int = Field(default=10, ge=1)
    model_tier: Literal["frugal", "standard", "frontier"] = "standard"
    default_model: str = DEFAULT_OPUS_MODEL


class ExecutionConfig(BaseModel, frozen=True):
    """Phase 2 (Execution) configuration.

    Attributes:
        max_iterations_per_ac: Maximum iterations per acceptance criteria
        retrospective_interval: Iterations between retrospectives
        tui_autolaunch: Whether `ooo run` should open the TUI without prompting
        auto_evaluate: When true, an evaluable terminal `execute_seed` run
            automatically enqueues formal evaluation as a background job.
        auto_evolve: When true, a rejected formal evaluation automatically
            continues through a bounded Ralph evolution loop.
        auto_evolve_max_generations: Maximum Ralph generations for that chain.
        default_model: Optional model pin for every Execute-stage runtime call.
            ``None`` (the default) keeps the runtime's own selected model.
        run_verify_commands: Whether the orchestrator checks an AC's success
            contract itself before accepting the AC: all ``expected_artifacts``
            must exist under the run workspace and ``verify_command`` must exit
            0 (plus any ``output_assertion``). On by default.
        verify_command_timeout_seconds: Timeout for an AC verify command.
        ac_retry_attempts: How many times a failed AC is re-dispatched before
            it is marked FAILED (per-AC, excludes stall retries).
        cross_harness_redispatch: Whether a terminally failing AC may be
            re-dispatched once onto a different (installed, capable) runtime
            backend before being marked FAILED (PR-X cross-harness recovery).
        n_version_tournament: Whether an AC that has already exhausted its
            alt-harness redispatch may fan out to multiple runtimes in parallel,
            first-passing-verification wins (PR-X N-version tournament, opt-in).
        decomposition_mode: Controls where AC decomposition is allowed:
            ``bounce_only`` only decomposes after an evidence-backed too-big
            bounce, and ``off`` disables decomposition. Legacy ``preflight``
            configuration is migrated to ``bounce_only`` at load time.
        context_pack: Whether to append a deterministic repo context pack
            (stack, verify commands, layout) to run worker system prompts.
        project_guidance: Allowlist of project guidance ids to resolve from
            fixed project-local paths under ``.ouroboros/guidance/<id>/GUIDANCE.md``.
        default_policy: Persistent default execution policy for FRESH runs
            (#1733). ``ask`` (the default) preserves the host's interactive
            prompt exactly; ``efficient`` resolves to adaptive/observe and
            ``quality_first`` to quality_first/off without asking. Explicit
            invocation arguments always win, resumed sessions keep their
            persisted immutable contract, and strict frugality assurance
            never derives from this setting.
    """

    max_iterations_per_ac: int = Field(default=10, ge=1)
    retrospective_interval: int = Field(default=3, ge=1)
    tui_autolaunch: bool = False
    auto_evaluate: bool = True
    auto_evolve: bool = True
    auto_evolve_max_generations: int = Field(default=3, ge=1, le=10)
    default_model: str | None = None
    run_verify_commands: bool = True
    verify_command_timeout_seconds: int = Field(default=600, ge=1)
    ac_retry_attempts: int = Field(default=2, ge=0)
    cross_harness_redispatch: bool = False
    n_version_tournament: bool = False
    decomposition_mode: Literal["bounce_only", "off"] = "bounce_only"
    context_pack: bool = True
    project_guidance: tuple[str, ...] = ()
    default_policy: Literal["ask", "efficient", "quality_first"] = "ask"

    @field_validator("decomposition_mode", mode="before")
    @classmethod
    def _migrate_legacy_decomposition_mode(cls, value: object) -> object:
        """Retire pre-execution splitting without breaking stored config files."""
        return "bounce_only" if value == "preflight" else value

    @field_validator("auto_evolve_max_generations", mode="before")
    @classmethod
    def _clamp_auto_evolve_max_generations(cls, value: object) -> object:
        """Keep the automatic loop inside Ralph's public 1..10 envelope."""

        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = int(value)
            except ValueError:
                return value
        else:
            return value
        return max(1, min(10, parsed))

    @field_validator("project_guidance")
    @classmethod
    def _validate_project_guidance(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject path-like or duplicate project guidance ids at config load."""
        seen: set[str] = set()
        validated: list[str] = []
        for raw_id in value:
            guidance_id = raw_id.strip()
            if not _SAFE_PROJECT_GUIDANCE_ID_RE.fullmatch(guidance_id):
                raise ValueError(
                    "execution.project_guidance entries must be safe ids containing only "
                    "letters, numbers, '.', '_', or '-' and must not be path segments"
                )
            if guidance_id in {".", ".."}:
                raise ValueError("execution.project_guidance entries must not be '.' or '..'")
            if guidance_id in seen:
                raise ValueError(f"duplicate execution.project_guidance id: {guidance_id!r}")
            seen.add(guidance_id)
            validated.append(guidance_id)
        return tuple(validated)


class ResilienceConfig(BaseModel, frozen=True):
    """Phase 3 (Resilience) configuration.

    Attributes:
        stagnation_enabled: Whether stagnation detection is enabled
        lateral_thinking_enabled: Whether lateral thinking is enabled
        lateral_model_tier: Tier for lateral thinking
        lateral_temperature: Temperature for lateral thinking LLM calls
        wonder_model: Default model for Wonder phase
        reflect_model: Default model for Reflect phase
    """

    stagnation_enabled: bool = True
    lateral_thinking_enabled: bool = True
    lateral_model_tier: Literal["frugal", "standard", "frontier"] = "frontier"
    lateral_temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    wonder_model: str = DEFAULT_OPUS_MODEL
    reflect_model: str = DEFAULT_OPUS_MODEL


class EvaluationConfig(BaseModel, frozen=True):
    """Phase 4 (Evaluation) configuration.

    Attributes:
        stage1_enabled: Whether mechanical checks are enabled
        stage2_enabled: Whether semantic evaluation is enabled
        stage3_enabled: Whether consensus evaluation is enabled
        satisfaction_threshold: Minimum satisfaction score
        uncertainty_threshold: Threshold above which to trigger consensus
        semantic_model: Default model for semantic evaluation
        assertion_extraction_model: Default model for verification assertion extraction
    """

    stage1_enabled: bool = True
    stage2_enabled: bool = True
    stage3_enabled: bool = True
    satisfaction_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    uncertainty_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    semantic_model: str = DEFAULT_OPUS_MODEL
    assertion_extraction_model: str = DEFAULT_SONNET_MODEL


class ConsensusConfig(BaseModel, frozen=True):
    """Phase 5 (Consensus) configuration.

    Attributes:
        min_models: Minimum number of models for consensus
        threshold: Agreement threshold for consensus
        diversity_required: Whether different providers are required
        models: Default model roster for stage 3 voting
        advocate_model: Default model for deliberative advocate role
        devil_model: Default model for deliberative devil role
        judge_model: Default model for deliberative judge role
    """

    min_models: int = Field(default=3, ge=2)
    threshold: float = Field(default=0.67, ge=0.0, le=1.0)
    diversity_required: bool = True
    models: tuple[str, ...] = (
        "openrouter/openai/gpt-4o",
        DEFAULT_CONSENSUS_OPUS_MODEL,
        "openrouter/google/gemini-2.5-pro",
    )
    advocate_model: str = DEFAULT_CONSENSUS_OPUS_MODEL
    devil_model: str = "openrouter/openai/gpt-4o"
    judge_model: str = "openrouter/google/gemini-2.5-pro"


class PersistenceConfig(BaseModel, frozen=True):
    """Persistence configuration.

    Attributes:
        enabled: Whether persistence is enabled
        database_path: Path to SQLite database (relative to config dir)
    """

    enabled: bool = True
    database_path: str = "data/ouroboros.db"


class DriftConfig(BaseModel, frozen=True):
    """Drift monitoring configuration.

    Attributes:
        warning_threshold: Drift score threshold for warnings
        critical_threshold: Drift score threshold for intervention
    """

    warning_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    critical_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("critical_threshold")
    @classmethod
    def validate_critical_threshold(cls, v: float, info: object) -> float:
        """Validate that critical threshold >= warning threshold."""
        data = getattr(info, "data", {})
        warning = data.get("warning_threshold", 0.3)
        if v < warning:
            msg = f"critical_threshold ({v}) must be >= warning_threshold ({warning})"
            raise ValueError(msg)
        return v


class RuntimeControlsConfig(BaseModel, frozen=True):
    """Runtime liveness and progress controls for long-running work.

    Attributes:
        mcp_tool_timeout_seconds: Server-side MCP tool timeout. ``0`` disables
            the adapter-level wall-clock timeout so progress-aware tools can
            enforce their own liveness policy.
        generation_idle_timeout_seconds: Seconds without any generation or
            execution activity before the generation is considered idle.
            ``0`` disables this guard.
        generation_no_progress_timeout_seconds: Seconds with activity but no
            material progress before the generation is considered stuck.
            ``0`` disables this guard.
        generation_safety_timeout_seconds: Optional final wall-clock safety
            cap for a generation. ``0`` disables this guard.
        watchdog_poll_seconds: How often the generation watchdog polls
            EventStore for new progress.
    """

    mcp_tool_timeout_seconds: float = Field(default=0, ge=0)
    generation_idle_timeout_seconds: float = Field(default=7200, ge=0)
    generation_no_progress_timeout_seconds: float = Field(default=14400, ge=0)
    generation_safety_timeout_seconds: float = Field(default=0, ge=0)
    watchdog_poll_seconds: float = Field(default=15.0, gt=0.0)


class LoggingConfig(BaseModel, frozen=True):
    """Logging configuration.

    Attributes:
        level: Log level (debug, info, warning, error)
        log_path: Path to log file (relative to config dir)
        include_reasoning: Whether to log LLM reasoning
    """

    level: Literal["debug", "info", "warning", "error"] = "info"
    log_path: str = "logs/ouroboros.log"
    include_reasoning: bool = True


VALID_RUNTIME_BACKENDS = frozenset(
    {
        "claude",
        "claude_code",
        "claude_mcp",
        "codex",
        "codex_cli",
        "opencode",
        "opencode_cli",
        "hermes",
        "hermes_cli",
        "gemini",
        "gemini_cli",
        "kiro",
        "kiro_cli",
        "copilot",
        "copilot_cli",
        "goose",
        "goose_cli",
        "pi",
        "pi_cli",
        "omp",
        "omp_cli",
        "gjc",
        "gjc_cli",
        "antigravity",
        "agy",
        "grok",
        "grok_cli",
        "grok_build",
        "zcode",
        "zcode_cli",
        "host",
        "host_dispatch",
    }
)


class RuntimeProfileConfig(BaseModel, frozen=True):
    """Runtime profile configuration (issue #519 / M4 / S3).

    The Agent OS architecture diagram agreed in #476 lets each pipeline
    stage (``interview`` / ``execute`` / ``evaluate`` / ``reflect``) be
    served by a different harness. This block exposes that decision as
    a configuration surface; the resolution helper in
    ``ouroboros.orchestrator.stage`` reads it.

    This object also reserves ``backend_profile`` for backend-native
    profile selection (for example PR #505's Codex ``worker`` profile),
    so the public ``orchestrator.runtime_profile`` key has one stable
    object shape instead of conflicting string-vs-table meanings.

    Attributes:
        backend_profile: Optional backend-native profile name. Stage
            routing does not interpret it; backend adapters may map it
            to their own profile mechanism.
        default: Optional runtime backend that serves any stage missing
            from ``stages``. ``None`` means "fall through to the
            orchestrator's top-level ``runtime_backend``".
        stages: Explicit per-stage mapping. Keys must be members of the
            closed stage vocabulary; unknown keys raise ``ValueError``
            during Pydantic validation at startup.
    """

    backend_profile: str | None = None
    default: str | None = None
    stages: dict[str, str] = Field(default_factory=dict)

    @field_validator("backend_profile")
    @classmethod
    def _validate_backend_profile(cls, value: str | None) -> str | None:
        """Normalize optional backend-native profile names."""
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            raise ValueError("runtime_profile.backend_profile must not be empty")
        return candidate

    @field_validator("default")
    @classmethod
    def _validate_default_backend(cls, value: str | None) -> str | None:
        """Reject invalid runtime_profile.default backend names at startup."""
        if value is None:
            return None
        return _validate_runtime_backend(value, field_name="runtime_profile.default")

    @field_validator("stages")
    @classmethod
    def _validate_stage_keys(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject unknown stage names and invalid backend names at startup."""
        validated: dict[str, str] = {}
        for key, backend in value.items():
            if key not in VALID_STAGE_KEYS:
                valid_list = ", ".join(sorted(VALID_STAGE_KEYS))
                raise ValueError(
                    f"Unknown runtime_profile.stages key: {key!r}. Valid keys are: {valid_list}.",
                )
            validated[key] = _validate_runtime_backend(
                backend,
                field_name=f"runtime_profile.stages[{key!r}]",
            )
        return validated


def _validate_runtime_backend(value: str, *, field_name: str) -> str:
    """Validate runtime_profile backend names against orchestrator backends."""
    candidate = value.strip().lower()
    if candidate not in VALID_RUNTIME_BACKENDS:
        valid_list = ", ".join(sorted(VALID_RUNTIME_BACKENDS))
        raise ValueError(f"{field_name} must be one of: {valid_list}")
    return candidate


class OrchestratorConfig(BaseModel, frozen=True):
    """Orchestrator runtime configuration.

    Attributes:
        runtime_backend: Agent runtime backend to use for orchestrator execution.
        runtime_profile: Optional Agent OS runtime profile object.
            ``runtime_profile.backend_profile`` selects backend-native profiles
            such as Codex ``--profile ouroboros-worker``. Default ``None``
            preserves the backend's normal user-config behavior. The ``default``
            and ``stages`` fields reserve the same object contract used by the
            stage-routing stack so this public key does not split into
            incompatible string-vs-table meanings.
        permission_mode: Default permission mode for local agent runtimes.
        opencode_permission_mode: Default permission mode for OpenCode agent runtimes.
        cli_path: Path to Claude CLI binary. Supports:
            - Absolute path: /path/to/my-claude-wrapper
            - ~ expansion: ~/.my-claude-wrapper/bin/my-claude-wrapper
            - None: Use SDK bundled CLI
        codex_cli_path: Path to Codex CLI binary. Supports:
            - Absolute path: /path/to/codex
            - ~ expansion: ~/.local/bin/codex
            - None: Resolve from PATH at runtime
        opencode_cli_path: Path to OpenCode CLI binary. Supports:
            - Absolute path: /path/to/opencode
            - ~ expansion: ~/.local/bin/opencode
            - None: Resolve from PATH at runtime
        hermes_cli_path: Path to Hermes CLI binary. Supports:
            - Absolute path: /path/to/hermes
            - ~ expansion: ~/.local/bin/hermes
            - None: Resolve from PATH at runtime
        gemini_cli_path: Path to Gemini CLI binary. Supports:
            - Absolute path: /path/to/gemini
            - ~ expansion: ~/.local/bin/gemini
            - None: Resolve from PATH at runtime (or OUROBOROS_GEMINI_CLI_PATH)
        antigravity_cli_path: Path to the Antigravity CLI binary (``agy``).
            Supports:
            - Absolute path: /path/to/agy
            - ~ expansion: ~/.local/bin/agy
            - None: Resolve from PATH at runtime (or OUROBOROS_ANTIGRAVITY_CLI_PATH)
        grok_cli_path: Path to the Grok Build CLI binary (``grok``). Supports:
            - Absolute path: /path/to/grok
            - ~ expansion: ~/.local/bin/grok
            - None: Resolve from PATH at runtime (or OUROBOROS_GROK_CLI_PATH)
        zcode_cli_path: Path to the Zcode CLI entry script (``zcode.cjs``).
            Official app-bundle scripts launch through ZCode's bundled
            Electron/Node runtime; standalone scripts use the system Node.
            Directly executable wrappers are also accepted. Supports:
            - Absolute path: /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
            - ~ expansion supported
            - None: Resolve from config/env or fall back to the macOS app-bundle
              default (or OUROBOROS_ZCODE_CLI_PATH)
        default_max_turns: Default max turns for agent execution
        max_parallel_workers: Default maximum concurrent AC workers
        usage_limit_pause_hours: Default pause window for provider usage/quota limits
        use_worktrees: Whether mutating workflows run in dedicated git worktrees
        worktree_root: Root directory for managed task worktrees
        worktree_cleanup: Cleanup policy for managed task worktrees applied when
            an auto session completes:
            - ``keep``: never remove anything (legacy behavior)
            - ``prune-merged`` (default): remove the worktree + ``ooo/*`` branch
              only when the branch is fully merged and the worktree checkout is
              clean
            - ``remove``: remove clean worktrees; delete the branch only when
              Git accepts a safe merged-branch deletion
        worktree_lock_stale_after_minutes: Staleness threshold for task lock recovery
        pm_snapshot_worktrees: Whether PM brownfield exploration reads from
            persistent detached worktrees pinned to the remote default branch
            (``origin/HEAD``) instead of the developer's live checkout. The
            worktree is created once per repo and refreshed (fetch + hard
            reset) on each PM interview start
        pm_snapshot_root: Root directory for PM snapshot worktrees
    """

    runtime_backend: Literal[
        "claude",
        "claude_mcp",
        "codex",
        "opencode",
        "hermes",
        "gemini",
        "kiro",
        "copilot",
        "goose",
        "pi",
        "omp",
        "gjc",
        "antigravity",
        "grok",
        "zcode",
        "host",
    ] = "claude"
    runtime_profile: RuntimeProfileConfig | None = None

    @field_validator("runtime_profile", mode="before")
    @classmethod
    def _coerce_runtime_profile(cls, value: Any) -> Any:
        """Accept the legacy PR #505 string shorthand as backend_profile."""
        if isinstance(value, str):
            return {"backend_profile": value}
        return value

    permission_mode: Literal["default", "acceptEdits", "bypassPermissions"] = "acceptEdits"
    opencode_permission_mode: Literal["default", "acceptEdits", "bypassPermissions"] = (
        "bypassPermissions"
    )
    # Effort-first investment dial (RFC #1405): base reasoning-effort level for
    # AC execution. Optional investment metadata may impose a floor or authorize
    # one lower notch; decomposition alone never lowers effort. The level is
    # ENFORCED on runtimes with a native per-call knob (Claude Agent SDK, Codex)
    # and advised elsewhere. ``None`` leaves effort routing dormant.
    # Restricted to the vocabulary every native runtime accepts: Codex-only
    # ``minimal`` and Claude-only ``max`` are excluded so a single global value is
    # never forwarded as an invalid level to whichever runtime executes the AC.
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    # OpenCode integration mode. Written by `ouroboros setup --opencode-mode`.
    # None = unset (legacy installs); gate treats None as NOT plugin (safe
    # default — require explicit opt-in via setup --opencode-mode=plugin).
    opencode_mode: Literal["plugin", "subprocess"] | None = None
    cli_path: str | None = None
    codex_cli_path: str | None = None
    copilot_cli_path: str | None = None
    opencode_cli_path: str | None = None
    opencode_stdout_idle_timeout_seconds: float | None = Field(default=None, gt=0.0)
    hermes_cli_path: str | None = None
    gemini_cli_path: str | None = None
    kiro_cli_path: str | None = None
    goose_cli_path: str | None = None
    pi_cli_path: str | None = None
    omp_cli_path: str | None = None
    gjc_cli_path: str | None = None
    antigravity_cli_path: str | None = None
    grok_cli_path: str | None = None
    ourocode_cli_path: str | None = None
    zcode_cli_path: str | None = None
    dsh_cli_path: str | None = None
    # dsh Cordis composition file passed to `dsh-acp-demo --config`. Not an
    # executable itself, but it names the plugins the Node process loads, so it
    # is treated with the same untrusted-source caution as a CLI path.
    dsh_config_path: str | None = None
    # POSIX shell used to run an AC's verify_command. Not an agent CLI, but it
    # is an executable path fed straight into a subprocess, so it carries the
    # same untrusted-source caution.
    verify_bash_path: str | None = None
    default_max_turns: int = Field(default=10, ge=1)
    max_parallel_workers: int = Field(default=3, ge=1)
    usage_limit_pause_hours: float = Field(default=5.0, gt=0.0)
    use_worktrees: bool = True
    worktree_root: str = "~/.ouroboros/worktrees"
    worktree_cleanup: Literal["keep", "remove", "prune-merged"] = "prune-merged"
    worktree_lock_stale_after_minutes: int = Field(default=60, ge=1)
    pm_snapshot_worktrees: bool = True
    pm_snapshot_root: str = "~/.ouroboros/pm-snapshots"

    @field_validator(
        "cli_path",
        "codex_cli_path",
        "copilot_cli_path",
        "opencode_cli_path",
        "hermes_cli_path",
        "gemini_cli_path",
        "kiro_cli_path",
        "goose_cli_path",
        "pi_cli_path",
        "omp_cli_path",
        "gjc_cli_path",
        "antigravity_cli_path",
        "grok_cli_path",
        "ourocode_cli_path",
        "zcode_cli_path",
        "dsh_cli_path",
        "dsh_config_path",
        "verify_bash_path",
    )
    @classmethod
    def expand_cli_path(cls, v: str | None) -> str | None:
        """Expand ~ in cli_path."""
        if v is None:
            return None
        return str(Path(v).expanduser())

    @field_validator("worktree_root")
    @classmethod
    def expand_worktree_root(cls, v: str) -> str:
        """Expand ~ in worktree_root."""
        return str(Path(v).expanduser())


class TelemetryConfig(BaseModel, frozen=True):
    """Anonymous usage telemetry configuration.

    Attributes:
        enabled: Whether anonymous usage events may be sent. Environment
            overrides (DO_NOT_TRACK, OUROBOROS_TELEMETRY) always win over
            this flag — see config.loader.get_telemetry_enabled().
    """

    enabled: bool = True


class SeedConfig(BaseModel, frozen=True):
    """Seed-authoring gates applied before a Seed is executed.

    Attributes:
        verify_command_gate: How to treat an acceptance criterion that declares
            neither a ``verify_command`` nor a ``verify_exemption_reason``.
            ``warn`` surfaces it and continues; ``block`` refuses the run.
            Such an AC can only be judged from the leaf's own transcript, which
            is both the gameable path and the one that breaks when transcript
            collection fails — so the default moves toward ``block`` over time.
            There is no permanent opt-out; the exemption reason is the escape
            hatch, and it is per-AC and explicit.
    """

    verify_command_gate: Literal["warn", "block"] = "warn"


class OuroborosConfig(BaseModel, frozen=True):
    """Top-level Ouroboros configuration.

    This is the main configuration model that combines all section configs.
    It validates against config.yaml in ~/.ouroboros/.

    Attributes:
        economics: Economic model and tier configuration
        llm: Shared LLM backend and model configuration
        clarification: Phase 0 (Big Bang) configuration
        execution: Phase 2 configuration
        resilience: Phase 3 configuration
        evaluation: Phase 4 configuration
        consensus: Phase 5 configuration
        llm_profiles: Named provider-neutral profiles for LLM-only tasks
        llm_role_profiles: Mapping from logical task roles to profile names
        persistence: Storage configuration
        drift: Drift monitoring configuration
        runtime_controls: Long-running workflow timeout/progress controls
        logging: Logging configuration
        seed: Seed-authoring gates applied before execution
    """

    economics: EconomicsConfig = Field(default_factory=EconomicsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    clarification: ClarificationConfig = Field(default_factory=ClarificationConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    llm_profiles: dict[str, LLMTaskProfileConfig] = Field(default_factory=dict)
    llm_role_profiles: dict[str, str] = Field(default_factory=dict)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    runtime_controls: RuntimeControlsConfig = Field(default_factory=RuntimeControlsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    seed: SeedConfig = Field(default_factory=SeedConfig)


def get_default_config() -> OuroborosConfig:
    """Get the default Ouroboros configuration.

    Returns:
        OuroborosConfig with all default values populated.
    """
    return OuroborosConfig(
        economics=EconomicsConfig(
            default_tier="frugal",
            # openai entries are the currently documented Codex CLI model ids per
            # tier (frugal/standard/frontier). Codex users on a custom
            # model_providers config or a proxy should override economics.tiers or
            # pin OUROBOROS_EXECUTION_MODEL, since those ids are not routable there.
            tiers={
                "frugal": TierConfig(
                    cost_factor=1,
                    intelligence_range=(9, 11),
                    models=[
                        ModelConfig(provider="openai", model="gpt-5.1-codex-mini"),
                        ModelConfig(provider="google", model="gemini-2.0-flash"),
                        ModelConfig(provider="anthropic", model=DEFAULT_HAIKU_MODEL),
                    ],
                    use_cases=["routine_coding", "log_analysis", "stage1_fix"],
                ),
                "standard": TierConfig(
                    cost_factor=10,
                    intelligence_range=(14, 16),
                    models=[
                        ModelConfig(provider="openai", model="gpt-5-codex"),
                        ModelConfig(provider="anthropic", model=DEFAULT_SONNET_MODEL),
                        ModelConfig(provider="google", model="gemini-2.5-pro"),
                    ],
                    use_cases=["logic_design", "stage2_evaluation", "refactoring"],
                ),
                "frontier": TierConfig(
                    cost_factor=30,
                    intelligence_range=(18, 20),
                    models=[
                        ModelConfig(provider="openai", model="gpt-5.2"),
                        ModelConfig(provider="anthropic", model=DEFAULT_OPUS_MODEL),
                    ],
                    use_cases=["consensus", "lateral_thinking", "big_bang"],
                ),
            },
            escalation_threshold=2,
            downgrade_success_streak=5,
        ),
    )


def get_default_credentials() -> CredentialsConfig:
    """Get the default credentials configuration template.

    Returns:
        CredentialsConfig with placeholder providers.

    Note:
        The returned credentials have empty API keys and should be
        filled in by the user.
    """
    return CredentialsConfig(
        providers={
            "openrouter": ProviderCredentials(
                api_key="YOUR_OPENROUTER_API_KEY",
                base_url="https://openrouter.ai/api/v1",
            ),
            "openai": ProviderCredentials(api_key="YOUR_OPENAI_API_KEY"),
            "anthropic": ProviderCredentials(api_key="YOUR_ANTHROPIC_API_KEY"),
            "google": ProviderCredentials(api_key="YOUR_GOOGLE_API_KEY"),
        }
    )


def get_config_dir() -> Path:
    """Get the Ouroboros configuration directory path.

    Returns:
        Path to ~/.ouroboros/
    """
    return Path.home() / ".ouroboros"


def _existing_event_store_target(path: Path) -> bool:
    """Return whether *path* exists, rejecting entries SQLite cannot open as a file."""
    try:
        exists = path.exists() or path.is_symlink()
        is_file = path.is_file() if exists else False
    except (OSError, RuntimeError, ValueError):
        raise ValueError("invalid EventStore configuration") from None
    if exists and not is_file:
        raise ValueError("invalid EventStore configuration")
    return exists


def event_store_path_from_config(data: Mapping[str, Any], config_path: Path) -> Path:
    """Resolve the EventStore path while preserving an existing legacy database."""
    persistence = data.get("persistence")
    if persistence is not None and not isinstance(persistence, Mapping):
        raise ValueError("config section 'persistence' must be a mapping")

    legacy_path = config_path.parent / "ouroboros.db"
    if not persistence or "database_path" not in persistence:
        _existing_event_store_target(legacy_path)
        return legacy_path
    configured = persistence["database_path"]
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError("config field 'persistence.database_path' must be a non-empty string")

    try:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = config_path.parent / configured_path
        configured_exists = _existing_event_store_target(configured_path)
        legacy_exists = legacy_path.is_file()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("invalid EventStore configuration") from None
    if configured_exists or not legacy_exists:
        return configured_path
    return legacy_path


def resolve_event_store_path(config_path: Path | None = None) -> Path:
    """Resolve the authoritative EventStore path for runtime and CLI readers."""
    if config_path is None:
        config_path = get_config_dir() / "config.yaml"

    if not config_path.exists():
        return config_path.parent / "ouroboros.db"

    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise ValueError("invalid EventStore configuration") from None
    if not isinstance(loaded, Mapping):
        raise ValueError("invalid EventStore configuration")
    return event_store_path_from_config(loaded, config_path)
