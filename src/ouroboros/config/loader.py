"""Configuration loading and management for Ouroboros.

This module provides functions for loading, creating, and validating
Ouroboros configuration files.

Functions:
    load_config: Load configuration from ~/.ouroboros/config.yaml
    load_credentials: Load credentials from ~/.ouroboros/credentials.yaml
    create_default_config: Create default configuration files
    ensure_config_dir: Ensure ~/.ouroboros/ directory exists
    get_agent_runtime_backend: Get orchestrator runtime backend from env var or config
    get_runtime_profile: Get orchestrator backend profile (e.g. "worker") from env var or config
    get_agent_permission_mode: Get orchestrator permission mode from env var or config
    get_llm_backend: Get LLM-only backend from env var or config
    get_llm_permission_mode: Get LLM-only permission mode from env var or config
    get_clarification_model: Get clarification model from env var or config
    get_qa_model: Get QA model from env var or config
    get_dependency_analysis_model: Get dependency analysis model from env var or config
    get_ontology_analysis_model: Get ontology analysis model from env var or config
    get_context_compression_model: Get context compression model from env var or config
    get_wonder_model: Get Wonder model from env var or config
    get_reflect_model: Get Reflect model from env var or config
    get_semantic_model: Get semantic evaluation model from env var or config
    get_assertion_extraction_model: Get verification assertion extraction model
    get_consensus_models: Get consensus model roster from env var or config
    get_consensus_advocate_model: Get deliberative advocate model from env var or config
    get_consensus_devil_model: Get deliberative devil model from env var or config
    get_consensus_judge_model: Get deliberative judge model from env var or config
    get_cli_path: Get Claude CLI path from env var or config
    get_codex_cli_path: Get Codex CLI path from env var or config
    get_opencode_cli_path: Get OpenCode CLI path from env var or config
    get_hermes_cli_path: Get Hermes CLI path from env var or config
    get_goose_cli_path: Get Goose CLI path from env var or config
"""

import ast
from collections.abc import Callable
import math
import os
from pathlib import Path
import shutil
import stat
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import yaml

from ouroboros.backends import get_backend_capability
from ouroboros.config._model_defaults import (  # noqa: E402
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    recognized_shipped_defaults,
)
from ouroboros.config.models import (  # noqa: E402
    CredentialsConfig,
    OuroborosConfig,
    RuntimeControlsConfig,
    get_config_dir,
    get_default_config,
    get_default_credentials,
)
from ouroboros.core.errors import ConfigError  # noqa: E402
from ouroboros.orchestrator_stage import (  # noqa: E402
    Stage,
    UnknownLLMRoleError,
    normalize_llm_role,
    parse_stage,
    resolve_runtime_for_llm_role,
    resolve_runtime_for_stage,
    stage_for_llm_role,
)

_CODEX_LLM_BACKENDS = frozenset({"codex", "codex_cli", "opencode", "opencode_cli"})
_KIRO_LLM_BACKENDS = frozenset({"kiro", "kiro_cli"})
_COPILOT_LLM_BACKENDS = frozenset({"copilot", "copilot_cli"})
_HERMES_LLM_BACKENDS = frozenset({"hermes", "hermes_cli"})
_PI_LLM_BACKENDS = frozenset({"pi", "pi_cli"})
_GJC_LLM_BACKENDS = frozenset({"gjc", "gjc_cli"})
# Antigravity (`agy`) is runtime-only and Claude-incapable: it runs its own
# Gemini/Claude models, so generic Claude default ids map to the CLI's own
# configured default (the "default" sentinel), exactly like the other
# non-Claude CLI backends above.
_ANTIGRAVITY_LLM_BACKENDS = frozenset({"antigravity", "agy"})
# Grok Build (`grok`) is runtime-only and Claude-incapable: it runs xAI's own
# Grok models, so generic Claude default ids map to the CLI's own configured
# default (the "default" sentinel).
_GROK_LLM_BACKENDS = frozenset({"grok", "grok_cli", "grok_build"})
_OPENCODE_BACKENDS = frozenset({"opencode", "opencode_cli"})
_CODEX_DEFAULT_MODEL = "default"
_KIRO_DEFAULT_MODEL = "default"
_COPILOT_DEFAULT_MODEL = "default"
_HERMES_DEFAULT_MODEL = "default"
_PI_DEFAULT_MODEL = "default"
_GJC_DEFAULT_MODEL = "default"
_ANTIGRAVITY_DEFAULT_MODEL = "default"
_GROK_DEFAULT_MODEL = "default"
_PLACEHOLDER_API_KEY_PREFIX = "YOUR_"
_PLACEHOLDER_API_KEY_SUFFIX = "_API_KEY"
_DEFAULT_MAX_PARALLEL_WORKERS = 3
_DEFAULT_CONSENSUS_MODELS = (
    "openrouter/openai/gpt-4o",
    DEFAULT_CONSENSUS_OPUS_MODEL,
    "openrouter/google/gemini-2.5-pro",
)
_DEFAULT_CONSENSUS_ADVOCATE_MODEL = DEFAULT_CONSENSUS_OPUS_MODEL
_DEFAULT_CONSENSUS_DEVIL_MODEL = "openrouter/openai/gpt-4o"
_DEFAULT_CONSENSUS_JUDGE_MODEL = "openrouter/google/gemini-2.5-pro"
_DEFAULT_USAGE_LIMIT_PAUSE_HOURS = 5.0
_SECONDS_PER_HOUR = 3600
_USAGE_LIMIT_PAUSE_CONFIG_KEY = "orchestrator.usage_limit_pause_hours"
_RUNTIME_CONTROL_ENV_KEYS = {
    "OUROBOROS_MCP_TOOL_TIMEOUT_SECONDS": "mcp_tool_timeout_seconds",
    "OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS": "generation_idle_timeout_seconds",
    "OUROBOROS_GENERATION_NO_PROGRESS_TIMEOUT_SECONDS": ("generation_no_progress_timeout_seconds"),
    "OUROBOROS_GENERATION_SAFETY_TIMEOUT_SECONDS": "generation_safety_timeout_seconds",
    "OUROBOROS_WATCHDOG_POLL_SECONDS": "watchdog_poll_seconds",
}


def _parse_env_value(raw_value: str) -> str:
    candidate = raw_value.strip()
    if not candidate:
        return ""

    if candidate[0] in {'"', "'"} and candidate[-1:] == candidate[0]:
        try:
            parsed = ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            return candidate[1:-1]
        return str(parsed)

    comment_index = candidate.find(" #")
    if comment_index != -1:
        candidate = candidate[:comment_index]
    return candidate.rstrip()


def _is_placeholder_api_key(value: str) -> bool:
    """Treat common template placeholders as unset."""
    candidate = value.strip()
    return bool(
        candidate
        and candidate.startswith(_PLACEHOLDER_API_KEY_PREFIX)
        and candidate.endswith(_PLACEHOLDER_API_KEY_SUFFIX)
    )


# Environment variables that determine HOW Ouroboros executes work. This
# is the single authoritative trust boundary: a cloned repository's `.env`
# must not be able to change which binary runs or whether the user's
# approval gate applies. Three classes, all remote-code-execution sinks
# when sourced from an untrusted location:
#   1. Explicit CLI path overrides fed straight into a subprocess.
#   2. Runtime/backend selectors that pick which adapter (and therefore
#      which executable) is spawned — a selector can route to a backend
#      whose CLI then resolves via a weak shutil.which / bare-name lookup.
#   3. Permission-mode overrides — setting acceptEdits/bypassPermissions
#      silently removes the human approval gate, letting a malicious repo
#      auto-approve arbitrary tool calls (effectively RCE).
# These keys are only honored from trusted sources (the real process
# environment, ~/.ouroboros/.env, ~/.ouroboros/config.yaml), never from
# the project-directory .env that travels with a cloned repo. Trusted .env
# files still follow the loader's normal "do not override an already-set
# real process environment value" precedence. Enforcing this here — at the
# .env load — keeps the policy in one place rather than split across
# downstream sinks.
_UNTRUSTED_ENV_DENYLIST = frozenset(
    {
        # Search PATH used by shutil.which()/bare executable spawning.
        "PATH",
        # Explicit executable-path overrides.
        "OUROBOROS_CLI_PATH",
        "OUROBOROS_CODEX_CLI_PATH",
        "OUROBOROS_COPILOT_CLI_PATH",
        "OUROBOROS_KIRO_CLI_PATH",
        "OUROBOROS_OPENCODE_CLI_PATH",
        "OUROBOROS_HERMES_CLI_PATH",
        "OUROBOROS_GOOSE_CLI_PATH",
        "OUROBOROS_GEMINI_CLI_PATH",
        "OUROBOROS_PI_CLI_PATH",
        "OUROBOROS_GJC_CLI_PATH",
        "OUROBOROS_ANTIGRAVITY_CLI_PATH",
        "OUROBOROS_GROK_CLI_PATH",
        "OUROBOROS_OUROCODE_CLI_PATH",
        # Bare provider aliases (no OUROBOROS_ prefix) that adapters also
        # honor and then execute. Any new such alias MUST be added here:
        # `opencode_config._configured_opencode_cli_path` reads
        # OPENCODE_CLI_PATH and runs it via subprocess.run.
        "OPENCODE_CLI_PATH",
        # Spawned-CLI discovery roots. The gjc CLI resolves its agent dir
        # (rules/skills/extensions it loads into every session) from these
        # vars; an untrusted repo .env must not be able to point a spawned
        # gjc at attacker-controlled instruction/extension directories.
        "GJC_CODING_AGENT_DIR",
        "GJC_CONFIG_DIR",
        "PI_CONFIG_DIR",
        # Copilot custom-instruction roots — same instruction-injection class
        # as GJC_CODING_AGENT_DIR. `copilot/cli_policy.py` derives the child
        # env from os.environ and only *appends* the setup-owned dir, so an
        # untrusted .env entry survives and a spawned Copilot loads attacker
        # AGENTS.md from it.
        "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
        # Ouroboros agent-definition root. `agents/loader.py` resolves every
        # agent's role/persona markdown (socratic-interviewer, evaluator, …)
        # from this dir first; an untrusted .env pointing it at a committed
        # repo dir lets a cloned repo replace the system prompt of every
        # spawned sub-agent — instruction injection, same class as above.
        "OUROBOROS_AGENTS_DIR",
        # Backend config-home roots. The spawned vendor CLI resolves its own
        # config file — which can name MCP servers to launch, disable the
        # approval gate, and widen the sandbox — from these vars, and the var
        # passes through the child env untouched (it is not in any backend's
        # strip_keys). An untrusted repo .env must not redirect a nested agent
        # at attacker-controlled config. Codex honors $CODEX_HOME/config.toml
        # (mcp_servers.<name>.command/args -> arbitrary command execution;
        # approval_policy="never" + sandbox_mode="danger-full-access" ->
        # silent removal of the human approval gate). OpenCode resolves its
        # config from OPENCODE_CONFIG / OPENCODE_CONFIG_DIR and otherwise
        # falls back to $XDG_CONFIG_HOME/opencode. Completes CVE-2026-47211.
        "CODEX_HOME",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        # Ouroboros' own MCP-bridge / plugin execution roster roots. Each
        # selects a file whose contents name an external command that the
        # bridge or plugin dispatcher then spawns verbatim — direct RCE, the
        # same threat model as the backend config-home roots above:
        #   - OUROBOROS_MCP_CONFIG -> mcp/bridge/config.py:discover_config
        #     returns the path; the YAML's server `command`/`args` are spawned
        #     via stdio_client (loader -> discover_config -> MCPClientAdapter
        #     -> stdio_client).
        #   - OUROBOROS_PLUGIN_LOCKFILE / OUROBOROS_PLUGIN_TRUST_ROOT ->
        #     plugin_dispatch resolves the installed-plugin roster and trust
        #     root from these; redirecting them lets a cloned repo register an
        #     attacker manifest / mark a malicious plugin as trusted, so
        #     `ooo <name>` dispatches into attacker code.
        "OUROBOROS_MCP_CONFIG",
        "OUROBOROS_PLUGIN_LOCKFILE",
        "OUROBOROS_PLUGIN_TRUST_ROOT",
        # SSRF guard toggle. `mcp/types.py` blocks loopback/private/link-local
        # MCP transport targets unless this is "1"; an untrusted .env must not
        # be able to re-enable connections to internal addresses.
        "OUROBOROS_ALLOW_LOCAL_TRANSPORT",
        # Runtime/backend selectors — choose which adapter is spawned.
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_RUNTIME",
        "OUROBOROS_LLM_BACKEND",
        # Backend profile selector (get_runtime_profile): chooses the
        # orchestrator backend profile and therefore which backend behavior /
        # executable is used — same routing class as the selectors above.
        "OUROBOROS_RUNTIME_PROFILE",
        # Permission-mode overrides — must not silently disable the
        # user's approval gate from an untrusted repo.
        "OUROBOROS_AGENT_PERMISSION_MODE",
        "OUROBOROS_LLM_PERMISSION_MODE",
        "OUROBOROS_OPENCODE_PERMISSION_MODE",
        # Tool-capability override file. The override YAML can lower a tool's
        # approval_class (e.g. ELEVATED -> DEFAULT), weakening the human
        # approval gate for non-built-in tools. External control of this path
        # is therefore an approval-gate-bypass sink — same class as the
        # permission-mode overrides above.
        "OUROBOROS_TOOL_CAPABILITIES",
        # Execution-cost/behavior dial — an untrusted repo .env must not be able
        # to force a higher (or invalid) reasoning-effort level for every AC,
        # which changes runtime cost and behavior. Follows the same trusted-source
        # policy as the runtime/permission overrides above (RFC #1405).
        "OUROBOROS_AGENT_REASONING_EFFORT",
        # Explicit constructor model pin. Besides changing cost/capability, this
        # disables tier routing by design, so an untrusted project .env must not
        # be able to force an arbitrary model or bypass the frugality policy.
        "OUROBOROS_EXECUTION_MODEL",
        # Model-tier experiment controls are the same trust class: a cloned repo
        # must not disable frugal child routing or opt the user into shadow replay,
        # which re-executes successful children and can double token spend.
        "OUROBOROS_MODEL_TIER_ROUTING",
        "OUROBOROS_SHADOW_REPLAY",
    }
)

# The reasoning-effort vocabulary every native runtime accepts (mirrors
# OrchestratorConfig.reasoning_effort). A value outside this set — Codex-only
# ``minimal``, Claude-only ``max``, or a typo — must never reach a runtime, so the
# env override is validated against it before use.
_VALID_REASONING_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh"})


def _is_untrusted_env_denied_key(key: str) -> bool:
    """Return whether an untrusted .env key may alter execution routing."""
    return key.upper() in _UNTRUSTED_ENV_DENYLIST


def _load_env_file(path: Path, *, trusted: bool = False) -> None:
    if not path.is_file():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or any(ch.isspace() for ch in key):
            continue

        if not trusted and _is_untrusted_env_denied_key(key):
            # Untrusted project-directory .env must not redirect which
            # binary Ouroboros executes (remote code execution guard).
            continue

        parsed_value = _parse_env_value(raw_value)
        if not parsed_value or _is_placeholder_api_key(parsed_value):
            continue

        current_value = os.environ.get(key)
        if current_value is None or _is_placeholder_api_key(current_value):
            os.environ[key] = parsed_value


# The project-directory .env travels with whatever repository the user
# cloned and is therefore untrusted; ~/.ouroboros/.env lives in the user's
# home and is trusted. The trust flag gates execution-redirecting keys above.
# `_load_env_file` defaults to trusted=False (fail-closed) so any future
# caller is safe-by-default; trust must be opted into explicitly.
_load_env_file(Path(".env"), trusted=False)
_load_env_file(Path.home() / ".ouroboros" / ".env", trusted=True)


def ensure_config_dir() -> Path:
    """Ensure the configuration directory exists.

    Creates ~/.ouroboros/ directory and subdirectories if they don't exist.

    Returns:
        Path to the configuration directory.
    """
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (config_dir / "data").mkdir(exist_ok=True)
    (config_dir / "logs").mkdir(exist_ok=True)

    return config_dir


def _set_secure_permissions(file_path: Path) -> None:
    """Set secure permissions (chmod 600) on a file.

    Args:
        file_path: Path to the file to secure.
    """
    # Set permissions to owner read/write only (0o600)
    os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)


def _model_to_yaml_dict(model: OuroborosConfig | CredentialsConfig) -> dict[str, Any]:
    """Convert a Pydantic model to a YAML-serializable dict.

    Args:
        model: The Pydantic model to convert.

    Returns:
        A dict suitable for YAML serialization.
    """
    return model.model_dump(mode="json")


def create_default_config(
    config_dir: Path | None = None,
    *,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """Create default configuration files.

    Creates config.yaml and credentials.yaml with default templates
    in the specified directory. credentials.yaml is created with
    chmod 600 permissions for security.

    Args:
        config_dir: Directory to create files in. Defaults to ~/.ouroboros/
        overwrite: If True, overwrite existing files. Defaults to False.

    Returns:
        Tuple of (config_path, credentials_path).

    Raises:
        ConfigError: If files exist and overwrite=False.
    """
    if config_dir is None:
        config_dir = ensure_config_dir()
    else:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "data").mkdir(exist_ok=True)
        (config_dir / "logs").mkdir(exist_ok=True)

    config_path = config_dir / "config.yaml"
    credentials_path = config_dir / "credentials.yaml"

    # Check if files exist
    if not overwrite:
        if config_path.exists():
            raise ConfigError(
                f"Configuration file already exists: {config_path}",
                config_file=str(config_path),
            )
        if credentials_path.exists():
            raise ConfigError(
                f"Credentials file already exists: {credentials_path}",
                config_file=str(credentials_path),
            )

    # Create config.yaml
    default_config = get_default_config()
    config_dict = _model_to_yaml_dict(default_config)
    with config_path.open("w") as f:
        yaml.dump(
            config_dict,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    # Create credentials.yaml with secure permissions
    default_credentials = get_default_credentials()
    credentials_dict = _model_to_yaml_dict(default_credentials)
    with credentials_path.open("w") as f:
        yaml.dump(
            credentials_dict,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    # Set chmod 600 on credentials file
    _set_secure_permissions(credentials_path)

    return config_path, credentials_path


def load_config(config_path: Path | None = None) -> OuroborosConfig:
    """Load configuration from YAML file.

    Loads and validates configuration from the specified path or
    the default ~/.ouroboros/config.yaml.

    Args:
        config_path: Path to config file. Defaults to ~/.ouroboros/config.yaml.

    Returns:
        Validated OuroborosConfig instance.

    Raises:
        ConfigError: If file doesn't exist, is malformed, or fails validation.
    """
    if config_path is None:
        config_path = get_config_dir() / "config.yaml"

    if not config_path.exists():
        raise ConfigError(
            f"Configuration file not found: {config_path}. "
            "Run `ouroboros config init` to create default configuration.",
            config_file=str(config_path),
        )

    try:
        with config_path.open() as f:
            config_dict = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Failed to parse configuration file: {e}",
            config_file=str(config_path),
            details={"yaml_error": str(e)},
        ) from e

    if config_dict is None:
        config_dict = {}

    try:
        return OuroborosConfig.model_validate(config_dict)
    except PydanticValidationError as e:
        # Format validation errors for clarity
        validation_errors = e.errors()
        error_messages = []
        config_keys = []
        for error in validation_errors:
            loc = ".".join(str(x) for x in error["loc"])
            msg = error["msg"]
            error_messages.append(f"  - {loc}: {msg}")
            if loc:
                config_keys.append(loc)

        config_key = config_keys[0] if len(validation_errors) == 1 and config_keys else None

        raise ConfigError(
            "Configuration validation failed:\n" + "\n".join(error_messages),
            config_key=config_key,
            config_file=str(config_path),
            details={
                "validation_errors": validation_errors,
                "config_keys": config_keys,
            },
        ) from e


def load_credentials(credentials_path: Path | None = None) -> CredentialsConfig:
    """Load credentials from YAML file.

    Loads and validates credentials from the specified path or
    the default ~/.ouroboros/credentials.yaml.

    Args:
        credentials_path: Path to credentials file.
            Defaults to ~/.ouroboros/credentials.yaml.

    Returns:
        Validated CredentialsConfig instance.

    Raises:
        ConfigError: If file doesn't exist, is malformed, or fails validation.
    """
    if credentials_path is None:
        credentials_path = get_config_dir() / "credentials.yaml"

    if not credentials_path.exists():
        raise ConfigError(
            f"Credentials file not found: {credentials_path}. "
            "Run `ouroboros config init` to create default configuration.",
            config_file=str(credentials_path),
        )

    # Check file permissions (warn if too permissive)
    file_mode = credentials_path.stat().st_mode
    if file_mode & (stat.S_IRGRP | stat.S_IROTH):
        # File is readable by group or others - this is a security warning
        # We don't raise an error, but this could be logged
        pass

    try:
        with credentials_path.open() as f:
            credentials_dict = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Failed to parse credentials file: {e}",
            config_file=str(credentials_path),
            details={"yaml_error": str(e)},
        ) from e

    if credentials_dict is None:
        credentials_dict = {}

    try:
        return CredentialsConfig.model_validate(credentials_dict)
    except PydanticValidationError as e:
        error_messages = []
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            msg = error["msg"]
            error_messages.append(f"  - {loc}: {msg}")

        raise ConfigError(
            "Credentials validation failed:\n" + "\n".join(error_messages),
            config_file=str(credentials_path),
            details={"validation_errors": e.errors()},
        ) from e


def config_exists() -> bool:
    """Check if configuration files exist.

    Returns:
        True if both config.yaml and credentials.yaml exist.
    """
    config_dir = get_config_dir()
    return (config_dir / "config.yaml").exists() and (config_dir / "credentials.yaml").exists()


def credentials_file_secure(credentials_path: Path | None = None) -> bool:
    """Check if credentials file has secure permissions.

    Args:
        credentials_path: Path to credentials file.
            Defaults to ~/.ouroboros/credentials.yaml.

    Returns:
        True if file has chmod 600 (owner read/write only).
    """
    if credentials_path is None:
        credentials_path = get_config_dir() / "credentials.yaml"

    if not credentials_path.exists():
        return False

    file_mode = credentials_path.stat().st_mode
    # Check that only owner has read/write permissions
    return (file_mode & 0o777) == 0o600


def _runtime_controls_error(error: ConfigError) -> bool:
    """Return True when a config validation error concerns runtime_controls."""
    if error.config_key and error.config_key.startswith("runtime_controls"):
        return True
    config_keys = error.details.get("config_keys") if isinstance(error.details, dict) else None
    return isinstance(config_keys, list) and any(
        str(config_key).startswith("runtime_controls") for config_key in config_keys
    )


def _parse_runtime_control_number(
    raw_value: str,
    *,
    config_key: str,
    allow_float: bool = False,
    allow_zero: bool = True,
) -> int | float:
    """Parse a runtime-control value from an environment variable."""
    candidate = raw_value.strip()
    try:
        parsed = float(candidate) if allow_float else int(candidate)
    except ValueError as exc:
        raise ConfigError(
            f"{config_key} must be a {'positive number' if allow_float else 'non-negative integer'}",
            config_key=config_key,
            details={"value": raw_value},
        ) from exc

    if allow_float:
        if parsed < 0 or (not allow_zero and parsed == 0):
            raise ConfigError(
                f"{config_key} must be {'greater than 0' if not allow_zero else 'greater than or equal to 0'}",
                config_key=config_key,
                details={"value": raw_value},
            )
        return parsed

    if parsed < 0:
        raise ConfigError(
            f"{config_key} must be greater than or equal to 0",
            config_key=config_key,
            details={"value": raw_value},
        )
    return parsed


def get_runtime_controls_config() -> RuntimeControlsConfig:
    """Get progress-aware runtime controls from config and environment.

    Priority:
        1. Dedicated environment variable overrides
        2. Legacy OUROBOROS_GENERATION_TIMEOUT as the no-progress timeout
        3. config.yaml runtime_controls section
        4. built-in defaults

    The legacy generation timeout maps to semantic no-material-progress
    detection, not to the MCP adapter wall-clock timeout.
    """
    try:
        controls = load_config().runtime_controls
    except ConfigError as exc:
        if _runtime_controls_error(exc):
            raise
        controls = RuntimeControlsConfig()

    updates: dict[str, float] = {}
    for env_key, field_name in _RUNTIME_CONTROL_ENV_KEYS.items():
        env_value = os.environ.get(env_key, "").strip()
        if not env_value:
            continue
        updates[field_name] = _parse_runtime_control_number(
            env_value,
            config_key=env_key,
            allow_float=True,
            allow_zero=field_name != "watchdog_poll_seconds",
        )

    legacy_generation_timeout = os.environ.get("OUROBOROS_GENERATION_TIMEOUT", "").strip()
    if legacy_generation_timeout and "generation_no_progress_timeout_seconds" not in updates:
        updates["generation_no_progress_timeout_seconds"] = _parse_runtime_control_number(
            legacy_generation_timeout,
            config_key="OUROBOROS_GENERATION_TIMEOUT",
            allow_float=True,
        )

    if not updates:
        return controls

    return RuntimeControlsConfig.model_validate({**controls.model_dump(), **updates})


def get_cli_path() -> str | None:
    """Get Claude CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_CLI_PATH environment variable
        2. config.yaml orchestrator.cli_path
        3. None (use SDK default)

    Returns:
        Path to CLI binary or None to use SDK default.
    """
    # 1. Check environment variable (highest priority)
    env_path = os.environ.get("OUROBOROS_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    # 2. Check config file
    try:
        config = load_config()
        if config.orchestrator.cli_path:
            return config.orchestrator.cli_path
    except ConfigError:
        # Config doesn't exist or is invalid - fall back to default
        pass

    # 3. Default: None (SDK uses bundled CLI)
    return None


def get_agent_runtime_backend() -> str:
    """Get orchestrator runtime backend from environment variable or config.

    Priority:
        1. OUROBOROS_AGENT_RUNTIME environment variable
        2. OUROBOROS_RUNTIME environment variable
        3. config.yaml orchestrator.runtime_backend
        4. "claude"

    Returns:
        Normalized runtime backend name.
    """
    env_backend = os.environ.get("OUROBOROS_AGENT_RUNTIME", "").strip().lower()
    if env_backend:
        return env_backend

    env_runtime = os.environ.get("OUROBOROS_RUNTIME", "").strip().lower()
    if env_runtime:
        return env_runtime

    try:
        config = load_config()
        return config.orchestrator.runtime_backend
    except ConfigError:
        return "claude"


def get_runtime() -> str:
    """Alias for get_agent_runtime_backend."""
    return get_agent_runtime_backend()


def get_context_pack_enabled() -> bool:
    """Whether run worker prompts get a deterministic repo context pack.

    Priority:
        1. ``OUROBOROS_CONTEXT_PACK`` environment variable (1|true|on|yes /
           0|false|off|no)
        2. config.yaml ``execution.context_pack``
        3. True (default on — a best-effort, deterministic priming block)
    """
    env = os.environ.get("OUROBOROS_CONTEXT_PACK", "").strip().lower()
    if env in ("1", "true", "on", "yes"):
        return True
    if env in ("0", "false", "off", "no"):
        return False
    try:
        return load_config().execution.context_pack
    except ConfigError:
        return True


def get_native_session_index_enabled() -> bool:
    """Whether to register worker sessions in the host tool's native session list.

    OFF by default: the web dashboard is the primary, non-flooding worker view
    (it groups every worker under one run). Opt in with
    ``OUROBOROS_NATIVE_SESSION_INDEX=1|true|on|yes`` to ALSO dump each worker into
    the Codex app's conversation list (one ``ooo:`` entry per worker) so you can
    open it natively — at the cost of a busier app list.
    """
    return os.environ.get("OUROBOROS_NATIVE_SESSION_INDEX", "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    )


def _env_flag(name: str) -> bool | None:
    """Parse a boolean env override; None when unset so config can decide."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return None


def get_cross_harness_redispatch_enabled() -> bool:
    """Whether a terminally failing AC may redispatch onto an alternative harness.

    Priority:
        1. OUROBOROS_CROSS_HARNESS_REDISPATCH environment variable
        2. config.yaml execution.cross_harness_redispatch
        3. True (default: meta-harness recovery is on, but a no-op unless a
           second runtime backend is actually installed)
    """
    env = _env_flag("OUROBOROS_CROSS_HARNESS_REDISPATCH")
    if env is not None:
        return env
    try:
        return load_config().execution.cross_harness_redispatch
    except ConfigError:
        return True


def get_n_version_tournament_enabled() -> bool:
    """Whether an alt-harness-exhausted AC may fan out to an N-version tournament.

    Priority:
        1. OUROBOROS_N_VERSION_TOURNAMENT environment variable
        2. config.yaml execution.n_version_tournament
        3. False (default: opt-in only)
    """
    env = _env_flag("OUROBOROS_N_VERSION_TOURNAMENT")
    if env is not None:
        return env
    try:
        return load_config().execution.n_version_tournament
    except ConfigError:
        return False


def _uses_opencode_backend(backend: str | None) -> bool:
    """Return True when a backend name resolves to an OpenCode runtime."""
    return (backend or "").strip().lower() in _OPENCODE_BACKENDS


def get_agent_permission_mode(backend: str | None = None) -> str:
    """Get orchestrator agent permission mode from environment variable or config.

    Priority:
        1. OUROBOROS_AGENT_PERMISSION_MODE environment variable
        2. OUROBOROS_OPENCODE_PERMISSION_MODE for OpenCode runtimes
        3. config.yaml orchestrator.opencode_permission_mode for OpenCode runtimes
        4. config.yaml orchestrator.permission_mode
        5. backend default ("bypassPermissions" for OpenCode, otherwise "acceptEdits")
    """
    env_mode = os.environ.get("OUROBOROS_AGENT_PERMISSION_MODE", "").strip()
    if env_mode:
        return env_mode

    if _uses_opencode_backend(backend):
        opencode_env_mode = os.environ.get("OUROBOROS_OPENCODE_PERMISSION_MODE", "").strip()
        if opencode_env_mode:
            return opencode_env_mode

    try:
        config = load_config()
        if _uses_opencode_backend(backend):
            return config.orchestrator.opencode_permission_mode
        return config.orchestrator.permission_mode
    except ConfigError:
        return "bypassPermissions" if _uses_opencode_backend(backend) else "acceptEdits"


def get_agent_reasoning_effort() -> str | None:
    """Get the base reasoning-effort level for AC execution (RFC #1405).

    Priority:
        1. OUROBOROS_AGENT_REASONING_EFFORT environment variable
        2. config.yaml orchestrator.reasoning_effort
        3. None (effort routing stays dormant — no behavior change)

    The env override is validated against the native-shared vocabulary; an invalid
    value is ignored (falls through to config) rather than forwarded to a runtime
    that would reject it. From an untrusted project ``.env`` the key is denylisted
    entirely, so it is only honored from a trusted source.
    """
    env_effort = os.environ.get("OUROBOROS_AGENT_REASONING_EFFORT", "").strip()
    if env_effort in _VALID_REASONING_EFFORT_LEVELS:
        return env_effort
    # A set but invalid env value (Codex-only ``minimal``, Claude-only ``max``, or a
    # typo) is dropped rather than forwarded to a runtime that would reject it; fall
    # through to the schema-validated config value.
    try:
        return load_config().orchestrator.reasoning_effort
    except ConfigError:
        return None


def _parse_max_parallel_workers(value: Any, *, config_key: str) -> int:
    """Parse a worker-cap setting without validating unrelated config keys."""
    if isinstance(value, bool):
        raise ConfigError(
            f"{config_key} must be a positive integer",
            config_key=config_key,
            details={"value": value},
        )

    try:
        parsed = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ConfigError(
            f"{config_key} must be a positive integer",
            config_key=config_key,
            details={"value": value},
        ) from exc

    if isinstance(value, float) and not value.is_integer():
        raise ConfigError(
            f"{config_key} must be a positive integer",
            config_key=config_key,
            details={"value": value},
        )

    if not math.isfinite(parsed):
        raise ConfigError(
            f"{config_key} must be finite",
            config_key=config_key,
            details={"value": value},
        )

    if parsed <= 0:
        raise ConfigError(
            f"{config_key} must be greater than 0",
            config_key=config_key,
            details={"value": value},
        )

    return parsed


def _parse_positive_float(value: Any, *, config_key: str) -> float:
    """Parse a positive float setting without silently accepting booleans."""
    if isinstance(value, bool):
        raise ConfigError(
            f"{config_key} must be a positive number",
            config_key=config_key,
            details={"value": value},
        )

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{config_key} must be a positive number",
            config_key=config_key,
            details={"value": value},
        ) from exc

    if not math.isfinite(parsed):
        raise ConfigError(
            f"{config_key} must be finite",
            config_key=config_key,
            details={"value": value},
        )

    if parsed <= 0:
        raise ConfigError(
            f"{config_key} must be greater than 0",
            config_key=config_key,
            details={"value": value},
        )

    return parsed


def get_usage_limit_pause_seconds() -> int:
    """Get the default pause window for provider usage/quota limits.

    Priority:
        1. OUROBOROS_USAGE_LIMIT_PAUSE_HOURS environment variable
        2. config.yaml orchestrator.usage_limit_pause_hours
        3. built-in default (5 hours)
    """
    env_value = os.environ.get("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "").strip()
    if env_value:
        hours = _parse_positive_float(
            env_value,
            config_key="OUROBOROS_USAGE_LIMIT_PAUSE_HOURS",
        )
        return max(1, int(hours * _SECONDS_PER_HOUR))

    config_path = get_config_dir() / "config.yaml"
    if not config_path.exists():
        # No config file means no pause-window override; use the built-in default.
        return int(_DEFAULT_USAGE_LIMIT_PAUSE_HOURS * _SECONDS_PER_HOUR)

    try:
        with config_path.open() as f:
            config_dict = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Failed to parse configuration file: {e}",
            config_file=str(config_path),
            details={"yaml_error": str(e)},
        ) from e
    except OSError as e:
        raise ConfigError(
            f"Failed to read configuration file: {e}",
            config_file=str(config_path),
            details={"os_error": str(e), "error_type": type(e).__name__},
        ) from e

    if config_dict is None:
        # Empty config means no pause-window override; use the built-in default.
        return int(_DEFAULT_USAGE_LIMIT_PAUSE_HOURS * _SECONDS_PER_HOUR)
    if not isinstance(config_dict, dict):
        raise ConfigError(
            "Configuration file must contain a mapping",
            config_file=str(config_path),
            details={"value_type": type(config_dict).__name__},
        )

    orchestrator_config = config_dict.get("orchestrator")
    if orchestrator_config is None:
        # Missing orchestrator section means no pause-window override.
        return int(_DEFAULT_USAGE_LIMIT_PAUSE_HOURS * _SECONDS_PER_HOUR)
    if not isinstance(orchestrator_config, dict):
        raise ConfigError(
            "orchestrator must be a mapping",
            config_key="orchestrator",
            config_file=str(config_path),
            details={"value": orchestrator_config},
        )
    if "usage_limit_pause_hours" not in orchestrator_config:
        # Missing pause-window key means no override; invalid values still raise below.
        return int(_DEFAULT_USAGE_LIMIT_PAUSE_HOURS * _SECONDS_PER_HOUR)

    hours = _parse_positive_float(
        orchestrator_config["usage_limit_pause_hours"],
        config_key=_USAGE_LIMIT_PAUSE_CONFIG_KEY,
    )
    return max(1, int(hours * _SECONDS_PER_HOUR))


def get_max_parallel_workers() -> int:
    """Get the default AC worker cap from environment variable or config.

    Priority:
        1. OUROBOROS_MAX_PARALLEL_WORKERS environment variable
        2. config.yaml orchestrator.max_parallel_workers
        3. built-in default (3)
    """
    env_value = os.environ.get("OUROBOROS_MAX_PARALLEL_WORKERS", "").strip()
    if env_value:
        return _parse_max_parallel_workers(
            env_value,
            config_key="OUROBOROS_MAX_PARALLEL_WORKERS",
        )

    config_path = get_config_dir() / "config.yaml"
    if not config_path.exists():
        # No config file means no worker-cap override; use the built-in default.
        return _DEFAULT_MAX_PARALLEL_WORKERS

    try:
        with config_path.open() as f:
            config_dict = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Failed to parse configuration file: {e}",
            config_file=str(config_path),
            details={"yaml_error": str(e)},
        ) from e
    except OSError as e:
        raise ConfigError(
            f"Failed to read configuration file: {e}",
            config_file=str(config_path),
            details={"os_error": str(e), "error_type": type(e).__name__},
        ) from e

    if config_dict is None:
        # Empty config means no worker-cap override; use the built-in default.
        return _DEFAULT_MAX_PARALLEL_WORKERS
    if not isinstance(config_dict, dict):
        raise ConfigError(
            "Configuration file must contain a mapping",
            config_file=str(config_path),
            details={"value_type": type(config_dict).__name__},
        )

    orchestrator_config = config_dict.get("orchestrator")
    if orchestrator_config is None:
        # Missing orchestrator section means no worker-cap override.
        return _DEFAULT_MAX_PARALLEL_WORKERS
    if not isinstance(orchestrator_config, dict):
        raise ConfigError(
            "orchestrator must be a mapping",
            config_key="orchestrator",
            config_file=str(config_path),
            details={"value": orchestrator_config},
        )
    if "max_parallel_workers" not in orchestrator_config:
        # Missing worker-cap key means no override; invalid values still raise below.
        return _DEFAULT_MAX_PARALLEL_WORKERS

    return _parse_max_parallel_workers(
        orchestrator_config["max_parallel_workers"],
        config_key="orchestrator.max_parallel_workers",
    )


def get_auto_evaluate_enabled() -> bool:
    """Return whether successful execute_seed runs should enqueue formal evaluation."""
    try:
        return load_config().execution.auto_evaluate
    except ConfigError:
        return True


def get_runtime_profile() -> str | None:
    """Get the orchestrator backend profile from env var or config file.

    Priority:
        1. OUROBOROS_RUNTIME_PROFILE environment variable
        2. config.yaml orchestrator.runtime_profile.backend_profile
        3. None (no profile — backends keep their default user-config behavior)

    Returns:
        The backend profile name (e.g. ``"worker"``) or None.
    """
    env_value = os.environ.get("OUROBOROS_RUNTIME_PROFILE", "").strip()
    if env_value:
        return env_value

    try:
        config = load_config()
        profile = config.orchestrator.runtime_profile
        if profile is not None and profile.backend_profile:
            return profile.backend_profile
    except ConfigError:
        pass

    return None


def get_codex_cli_path() -> str | None:
    """Get Codex CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_CODEX_CLI_PATH environment variable
        2. config.yaml orchestrator.codex_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to Codex CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.codex_cli_path:
            return config.orchestrator.codex_cli_path
    except ConfigError:
        pass

    return None


def get_copilot_cli_path() -> str | None:
    """Get GitHub Copilot CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_COPILOT_CLI_PATH environment variable
        2. config.yaml orchestrator.copilot_cli_path
        3. None (resolve from PATH at runtime)

    Stale env var / config values that don't point to an executable are
    treated as missing so setup discovery can fall back to PATH instead of
    persisting an unusable explicit path.

    Returns:
        Path to Copilot CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_COPILOT_CLI_PATH", "").strip()
    if env_path:
        resolved = str(Path(env_path).expanduser())
        if shutil.which(resolved):
            return resolved

    try:
        config = load_config()
        copilot_path = getattr(config.orchestrator, "copilot_cli_path", None)
        if copilot_path:
            resolved = str(Path(copilot_path).expanduser())
            if shutil.which(resolved):
                return resolved
    except ConfigError:
        pass

    return None


def get_kiro_cli_path() -> str | None:
    """Get Kiro CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_KIRO_CLI_PATH environment variable
        2. config.yaml orchestrator.kiro_cli_path
        3. None (resolve from PATH at runtime)

    Stale env var / config values that don't point to an executable are
    treated as missing so setup discovery can fall back to PATH instead of
    persisting an unusable explicit path.

    Returns:
        Path to Kiro CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_KIRO_CLI_PATH", "").strip()
    if env_path:
        resolved = str(Path(env_path).expanduser())
        if shutil.which(resolved):
            return resolved

    try:
        config = load_config()
        if config.orchestrator.kiro_cli_path:
            resolved = str(Path(config.orchestrator.kiro_cli_path).expanduser())
            if shutil.which(resolved):
                return resolved
    except ConfigError:
        pass

    return None


def get_opencode_cli_path() -> str | None:
    """Get OpenCode CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_OPENCODE_CLI_PATH environment variable
        2. config.yaml orchestrator.opencode_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to OpenCode CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_OPENCODE_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.opencode_cli_path:
            return config.orchestrator.opencode_cli_path
    except ConfigError:
        pass

    return None


def get_opencode_stdout_idle_timeout_seconds() -> float | None:
    """Get OpenCode stdout-idle timeout from environment or config.

    Priority:
        1. OUROBOROS_OPENCODE_STDOUT_IDLE_TIMEOUT environment variable
        2. config.yaml orchestrator.opencode_stdout_idle_timeout_seconds
        3. None (runtime class default)

    Non-positive environment values disable the runtime stream-loop guard.
    Invalid values fall through to config/default behavior.
    """
    env_value = os.environ.get("OUROBOROS_OPENCODE_STDOUT_IDLE_TIMEOUT", "").strip()
    if env_value:
        try:
            parsed = float(env_value)
        except ValueError:
            parsed = None
        if parsed is not None and math.isfinite(parsed):
            return None if parsed <= 0 else parsed

    try:
        config = load_config()
        return config.orchestrator.opencode_stdout_idle_timeout_seconds
    except ConfigError:
        return None


def get_hermes_cli_path() -> str | None:
    """Get Hermes CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_HERMES_CLI_PATH environment variable
        2. config.yaml orchestrator.hermes_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to Hermes CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_HERMES_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        hermes_path = getattr(config.orchestrator, "hermes_cli_path", None)
        if hermes_path:
            return hermes_path
    except ConfigError:
        pass

    return None


def get_goose_cli_path() -> str | None:
    """Get Goose CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_GOOSE_CLI_PATH environment variable
        2. config.yaml orchestrator.goose_cli_path
        3. None (resolve from PATH at runtime)

    Stale env var / config values that don't point to an executable are
    treated as missing so callers can fall back to PATH discovery instead
    of persisting an unusable path.

    Returns:
        Path to Goose CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_GOOSE_CLI_PATH", "").strip()
    if env_path:
        resolved = str(Path(env_path).expanduser())
        if shutil.which(resolved):
            return resolved

    try:
        config = load_config()
        goose_path = getattr(config.orchestrator, "goose_cli_path", None)
        if goose_path:
            resolved = str(Path(goose_path).expanduser())
            if shutil.which(resolved):
                return resolved
    except ConfigError:
        pass

    return None


def get_pi_cli_path() -> str | None:
    """Get Pi CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_PI_CLI_PATH environment variable
        2. config.yaml orchestrator.pi_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to Pi CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_PI_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.pi_cli_path:
            return config.orchestrator.pi_cli_path
    except ConfigError:
        pass

    return None


def get_gjc_cli_path() -> str | None:
    """Get GJC CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_GJC_CLI_PATH environment variable
        2. config.yaml orchestrator.gjc_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to GJC CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_GJC_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.gjc_cli_path:
            return config.orchestrator.gjc_cli_path
    except ConfigError:
        pass

    return None


def get_ourocode_cli_path() -> str | None:
    """Get ourocode CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_OUROCODE_CLI_PATH environment variable
        2. config.yaml orchestrator.ourocode_cli_path
        3. None (resolve ``ourocode`` from PATH at runtime)

    Returns:
        Path to the ourocode executable or None.
    """
    env_path = os.environ.get("OUROBOROS_OUROCODE_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.ourocode_cli_path:
            return config.orchestrator.ourocode_cli_path
    except ConfigError:
        pass

    return None


def get_opencode_mode() -> str | None:
    """Get configured OpenCode integration mode from config file.

    Priority:
        1. config.yaml orchestrator.opencode_mode
        2. None (no explicit mode — runtime gate requires "plugin" to dispatch)

    No environment override by design. Users switch by re-running
    ``ouroboros setup --opencode-mode=<plugin|subprocess>``.

    Returns:
        "plugin", "subprocess", or None.
    """
    try:
        config = load_config()
        return config.orchestrator.opencode_mode
    except ConfigError:
        return None


def get_gemini_cli_path() -> str | None:
    """Get Gemini CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_GEMINI_CLI_PATH environment variable
        2. config.yaml orchestrator.gemini_cli_path
        3. None (resolve from PATH at runtime)

    Stale env var / config values that don't point to an executable are
    treated as missing so callers fall back to PATH discovery instead of
    persisting an unusable path. Mirrors the strictness of `shutil.which`
    used for the other runtime backends in the setup detection path.

    Returns:
        Path to Gemini CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_GEMINI_CLI_PATH", "").strip()
    if env_path:
        resolved = str(Path(env_path).expanduser())
        if shutil.which(resolved):
            return resolved

    try:
        config = load_config()
        gemini_path = getattr(config.orchestrator, "gemini_cli_path", None)
        if gemini_path:
            resolved = str(Path(gemini_path).expanduser())
            if shutil.which(resolved):
                return resolved
    except ConfigError:
        pass

    return None


def get_antigravity_cli_path() -> str | None:
    """Get the Antigravity CLI path (``agy``) from environment or config.

    Priority:
        1. OUROBOROS_ANTIGRAVITY_CLI_PATH environment variable
        2. config.yaml orchestrator.antigravity_cli_path
        3. None (resolve from PATH at runtime)

    Stale env var / config values that don't point to an executable are
    treated as missing so callers fall back to PATH discovery instead of
    persisting an unusable path. Mirrors the strictness of `shutil.which`
    used for the other runtime backends in the setup detection path.

    Returns:
        Path to the Antigravity CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_ANTIGRAVITY_CLI_PATH", "").strip()
    if env_path:
        resolved = str(Path(env_path).expanduser())
        if shutil.which(resolved):
            return resolved

    try:
        config = load_config()
        antigravity_path = getattr(config.orchestrator, "antigravity_cli_path", None)
        if antigravity_path:
            resolved = str(Path(antigravity_path).expanduser())
            if shutil.which(resolved):
                return resolved
    except ConfigError:
        pass

    return None


def get_grok_cli_path() -> str | None:
    """Get the Grok Build CLI path (``grok``) from environment or config.

    Priority:
        1. OUROBOROS_GROK_CLI_PATH environment variable
        2. config.yaml orchestrator.grok_cli_path
        3. None (resolve from PATH at runtime)

    Stale env var / config values that don't point to an executable are
    treated as missing so callers fall back to PATH discovery instead of
    persisting an unusable path. Mirrors the strictness of `shutil.which`
    used for the other runtime backends in the setup detection path.

    Returns:
        Path to the Grok Build CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_GROK_CLI_PATH", "").strip()
    if env_path:
        resolved = str(Path(env_path).expanduser())
        if shutil.which(resolved):
            return resolved

    try:
        config = load_config()
        grok_path = getattr(config.orchestrator, "grok_cli_path", None)
        if grok_path:
            resolved = str(Path(grok_path).expanduser())
            if shutil.which(resolved):
                return resolved
    except ConfigError:
        pass

    return None


def get_llm_backend() -> str:
    """Get default LLM backend from environment variable or config.

    Priority:
        1. OUROBOROS_LLM_BACKEND environment variable
        2. OUROBOROS_RUNTIME environment variable, when it names a runtime
           that also implements the LLM adapter contract
        3. config.yaml llm.backend
        4. "claude_code"

    Returns:
        Normalized LLM backend name.
    """
    env_backend = os.environ.get("OUROBOROS_LLM_BACKEND", "").strip().lower()
    if env_backend:
        return env_backend

    env_runtime = os.environ.get("OUROBOROS_RUNTIME", "").strip().lower()
    env_runtime_capability = get_backend_capability(env_runtime)
    if env_runtime_capability is not None and env_runtime_capability.supports_llm:
        if env_runtime in {"claude_code"}:
            return "claude_code"
        return env_runtime_capability.name

    try:
        config = load_config()
        return config.llm.backend
    except ConfigError:
        return "claude_code"


def _runtime_profile_stage_map(config: OuroborosConfig) -> dict[Stage, str] | None:
    profile = config.orchestrator.runtime_profile
    if profile is None:
        return None
    return {parse_stage(stage): backend for stage, backend in profile.stages.items()}


def _runtime_profile_default(config: OuroborosConfig) -> str | None:
    profile = config.orchestrator.runtime_profile
    return profile.default if profile is not None else None


def _explicit_llm_backend_override() -> str | None:
    """Return an explicitly-configured LLM-only backend override, or ``None``.

    Preserves the documented LLM-only contract — ``OUROBOROS_LLM_BACKEND``, an
    LLM-capable ``OUROBOROS_RUNTIME``, or ``config.llm.backend`` set away from the
    shipped ``"claude_code"`` default — so existing operator overrides keep
    steering internal-LLM roles. Returns ``None`` when nothing is explicitly set,
    letting per-stage routing fall through to the default agent runtime.
    """
    env_backend = os.environ.get("OUROBOROS_LLM_BACKEND", "").strip().lower()
    if env_backend:
        return env_backend

    env_runtime = os.environ.get("OUROBOROS_RUNTIME", "").strip().lower()
    runtime_capability = get_backend_capability(env_runtime)
    if runtime_capability is not None and runtime_capability.supports_llm:
        return "claude_code" if env_runtime == "claude_code" else runtime_capability.name

    try:
        configured = load_config().llm.backend
    except ConfigError:
        return None
    if configured and configured != "claude_code":
        return configured
    return None


def _internal_llm_fallback_backend(fallback_runtime_backend: str | None) -> str:
    """Fallback backend for stages/roles with no per-stage routing.

    Precedence: explicit legacy ``llm.backend`` / ``OUROBOROS_LLM_BACKEND``
    override (the documented LLM-only contract) → the caller-provided default
    agent runtime (e.g. ``create_ouroboros_server(runtime_backend=...)``) → the
    orchestrator's configured default agent runtime. The LLM-only override wins
    over the default agent so an explicit ``llm.backend`` is honored, while an
    un-configured override still inherits the caller/config default agent.
    """
    return (
        _explicit_llm_backend_override() or fallback_runtime_backend or get_agent_runtime_backend()
    )


def get_llm_backend_for_stage(
    stage: Stage | str,
    *,
    explicit_backend: str | None = None,
    fallback_runtime_backend: str | None = None,
) -> str:
    """Resolve the internal-LLM backend for a configured workflow stage.

    Precedence: ``explicit_backend`` (direct API/CLI) → ``runtime_profile.stages``
    (per-stage Agent) → ``runtime_profile.default`` → explicit legacy
    ``llm.backend`` / ``OUROBOROS_LLM_BACKEND`` override → orchestrator default
    agent runtime. Per-stage routing stays authoritative while existing LLM-only
    overrides remain honored for un-mapped stages.
    """
    if explicit_backend:
        return explicit_backend

    parsed_stage = stage if isinstance(stage, Stage) else parse_stage(stage)
    try:
        config = load_config()
        resolved = resolve_runtime_for_stage(
            parsed_stage,
            stages=_runtime_profile_stage_map(config),
            default=_runtime_profile_default(config),
            fallback=_internal_llm_fallback_backend(fallback_runtime_backend),
        )
    except ConfigError:
        # Config unreadable: still honor an env-level LLM override and the
        # caller's default agent before the documented get_llm_backend() default.
        return _explicit_llm_backend_override() or fallback_runtime_backend or get_llm_backend()

    return _guard_llm_completion_backend(resolved)


def _backend_supports_llm(name: str | None) -> bool:
    """Whether a backend can serve LLM completions (vs. being runtime-only).

    Runtime-only backends (e.g. ``antigravity``, ``grok``) declare
    ``supports_llm=False`` in the capability registry: they drive the agentic
    orchestrator runtime but have no LLM-completion adapter.
    """
    if not name:
        return False
    capability = get_backend_capability(name)
    return capability is not None and capability.supports_llm


def _guard_llm_completion_backend(resolved: str) -> str:
    """Ensure a resolved internal-LLM backend can actually serve completions.

    Per-stage routing may point a stage at a *runtime-only* backend
    (``supports_llm=False`` — e.g. ``antigravity``/``grok``) for agentic
    execution; using it for an internal LLM call would crash provider
    construction. The agentic runtime still uses the runtime-only backend (via
    ``resolve_runtime_for_stage``); only the LLM-completion call falls back to a
    completion backend — the explicit ``llm.backend`` override when valid, else
    the documented ``llm.backend`` default.
    """
    if _backend_supports_llm(resolved):
        return resolved
    override = _explicit_llm_backend_override()
    if override and _backend_supports_llm(override):
        return override
    return get_llm_backend()


def get_llm_backend_for_role(
    role: str,
    *,
    explicit_backend: str | None = None,
    fallback_runtime_backend: str | None = None,
) -> str:
    """Resolve the internal-LLM backend for a logical task role.

    Same precedence as :func:`get_llm_backend_for_stage`: per-stage routing wins,
    then the explicit legacy ``llm.backend`` / ``OUROBOROS_LLM_BACKEND`` override,
    then the default agent runtime.

    Capability guard: an LLM-completion role must resolve to a completion-capable
    backend. Per-stage routing may point a stage at a *runtime-only* backend
    (``supports_llm=False`` — e.g. ``antigravity``/``grok``) for agentic
    execution; such a backend would crash provider construction if used for an
    internal LLM call. In that case the agentic runtime still uses the
    runtime-only backend, but the LLM call falls back to a completion backend
    (the explicit ``llm.backend`` override when valid, else the documented
    ``llm.backend`` default).
    """
    if explicit_backend:
        return explicit_backend

    try:
        config = load_config()
        resolved = resolve_runtime_for_llm_role(
            role,
            stages=_runtime_profile_stage_map(config),
            default=_runtime_profile_default(config),
            fallback=_internal_llm_fallback_backend(fallback_runtime_backend),
        )
    except ConfigError:
        # Config unreadable: still honor an env-level LLM override and the
        # caller's default agent before the documented get_llm_backend() default.
        return _explicit_llm_backend_override() or fallback_runtime_backend or get_llm_backend()

    return _guard_llm_completion_backend(resolved)


# Legacy per-role model fields kept for backward compatibility. The stage
# model is the default, but a user who explicitly pinned one of these (env var,
# or a config field set away from its shipped default) still has it honored
# instead of silently dropped. Maps role -> (env var, field accessor, shipped
# default, dedicated getter name). The getter — resolved lazily because it is
# defined later in this module — applies the role's own backend normalization
# (e.g. snapping an opus pin to the "default" sentinel on codex backends).
# ``mechanical_detection`` reuses the assertion-extraction getter (its historical
# model source) to avoid recursing through ``get_mechanical_detector_model``.
_LEGACY_ROLE_MODEL_FIELDS: dict[str, tuple[str, Callable[["OuroborosConfig"], str], str, str]] = {
    "qa": ("OUROBOROS_QA_MODEL", lambda c: c.llm.qa_model, DEFAULT_SONNET_MODEL, "get_qa_model"),
    "assertion_extraction": (
        "OUROBOROS_ASSERTION_EXTRACTION_MODEL",
        lambda c: c.evaluation.assertion_extraction_model,
        DEFAULT_SONNET_MODEL,
        "get_assertion_extraction_model",
    ),
    "mechanical_detection": (
        "OUROBOROS_DETECTOR_MODEL",
        lambda c: c.evaluation.assertion_extraction_model,
        DEFAULT_SONNET_MODEL,
        "get_assertion_extraction_model",
    ),
    "dependency_analysis": (
        "OUROBOROS_DEPENDENCY_ANALYSIS_MODEL",
        lambda c: c.llm.dependency_analysis_model,
        DEFAULT_SONNET_MODEL,
        "get_dependency_analysis_model",
    ),
    "ontology_analysis": (
        "OUROBOROS_ONTOLOGY_ANALYSIS_MODEL",
        lambda c: c.llm.ontology_analysis_model,
        DEFAULT_SONNET_MODEL,
        "get_ontology_analysis_model",
    ),
    "context_compression": (
        "OUROBOROS_CONTEXT_COMPRESSION_MODEL",
        lambda c: c.llm.context_compression_model,
        "gpt-4",
        "get_context_compression_model",
    ),
    "wonder": (
        "OUROBOROS_WONDER_MODEL",
        lambda c: c.resilience.wonder_model,
        DEFAULT_OPUS_MODEL,
        "get_wonder_model",
    ),
}


def _explicit_legacy_role_model(role: str, backend: str | None) -> str | None:
    """Return an explicitly-set legacy per-role model override, or ``None``.

    "Explicit" means the role's env var is set, or its dedicated config field
    differs from the shipped default. This preserves pre-existing configs that
    pinned a per-role model before the stage-model consolidation. Resolution is
    delegated to the role's dedicated getter so backend normalization stays
    identical to the legacy path.
    """
    entry = _LEGACY_ROLE_MODEL_FIELDS.get(normalize_llm_role(role))
    if entry is None:
        return None
    env_var, field_getter, shipped_default, getter_name = entry
    # Env var wins and is returned raw, matching the legacy getters.
    if os.environ.get(env_var, "").strip():
        return os.environ[env_var].strip()
    try:
        config = load_config()
    except ConfigError:
        return None
    if field_getter(config) != shipped_default:
        getter: Callable[[str | None], str] = globals()[getter_name]
        return getter(backend)
    return None


def get_llm_model_for_role(
    role: str,
    *,
    backend: str | None = None,
    explicit_model: str | None = None,
) -> str:
    """Resolve the configured model for a logical internal-LLM role.

    Stage model fields are the default source of truth: interview roles use
    ``clarification.default_model``, evaluate/execute roles use
    ``evaluation.semantic_model``, and reflect roles use
    ``resilience.reflect_model``. An explicitly-pinned legacy per-role field
    (e.g. ``llm.qa_model``) still takes precedence for backward compatibility,
    and an unmapped role degrades to the evaluate model rather than raising.
    """
    if explicit_model:
        return explicit_model

    resolved_backend = backend or get_llm_backend_for_role(role)

    legacy_override = _explicit_legacy_role_model(role, resolved_backend)
    if legacy_override is not None:
        return legacy_override

    try:
        stage = stage_for_llm_role(role)
    except UnknownLLMRoleError:
        return get_semantic_model(resolved_backend)
    if stage == Stage.INTERVIEW:
        return get_clarification_model(resolved_backend)
    if stage == Stage.REFLECT:
        return get_reflect_model(resolved_backend)
    return get_semantic_model(resolved_backend)


def get_llm_permission_mode(backend: str | None = None) -> str:
    """Get default LLM permission mode from environment variable or config.

    Priority:
        1. OUROBOROS_LLM_PERMISSION_MODE environment variable
        2. OUROBOROS_OPENCODE_PERMISSION_MODE for OpenCode adapters
        3. config.yaml llm.opencode_permission_mode for OpenCode adapters
        4. config.yaml llm.permission_mode
        5. backend default ("acceptEdits" for OpenCode, otherwise "default")
    """
    env_mode = os.environ.get("OUROBOROS_LLM_PERMISSION_MODE", "").strip()
    if env_mode:
        return env_mode

    if _uses_opencode_backend(backend):
        opencode_env_mode = os.environ.get("OUROBOROS_OPENCODE_PERMISSION_MODE", "").strip()
        if opencode_env_mode:
            return opencode_env_mode

    try:
        config = load_config()
        if _uses_opencode_backend(backend):
            return config.llm.opencode_permission_mode
        return config.llm.permission_mode
    except ConfigError:
        return "acceptEdits" if _uses_opencode_backend(backend) else "default"


def _resolve_llm_backend_for_models(backend: str | None = None) -> str:
    """Resolve the effective backend name for backend-aware model defaults."""
    return (backend or get_llm_backend()).strip().lower()


def _default_model_for_backend(
    default_model: str,
    *,
    backend: str | None = None,
) -> str:
    """Map generic defaults to a backend-safe sentinel when needed."""
    resolved = _resolve_llm_backend_for_models(backend)
    if resolved in _CODEX_LLM_BACKENDS:
        return _CODEX_DEFAULT_MODEL
    if resolved in _KIRO_LLM_BACKENDS:
        return _KIRO_DEFAULT_MODEL
    if resolved in _COPILOT_LLM_BACKENDS:
        return _COPILOT_DEFAULT_MODEL
    if resolved in _HERMES_LLM_BACKENDS:
        return _HERMES_DEFAULT_MODEL
    if resolved in _PI_LLM_BACKENDS:
        return _PI_DEFAULT_MODEL
    if resolved in _GJC_LLM_BACKENDS:
        return _GJC_DEFAULT_MODEL
    if resolved in _ANTIGRAVITY_LLM_BACKENDS:
        return _ANTIGRAVITY_DEFAULT_MODEL
    if resolved in _GROK_LLM_BACKENDS:
        return _GROK_DEFAULT_MODEL
    return default_model


def _default_models_for_backend(
    default_models: tuple[str, ...],
    *,
    backend: str | None = None,
) -> tuple[str, ...]:
    """Map a tuple of default models to backend-safe defaults."""
    return tuple(_default_model_for_backend(model, backend=backend) for model in default_models)


def _normalize_configured_model_for_backend(
    configured_model: str,
    *,
    default_model: str,
    backend: str | None = None,
    extra_shipped_defaults: tuple[str, ...] = (),
) -> str:
    """Normalize config-backed models while preserving backend-safe defaults."""
    candidate = configured_model.strip()
    if not candidate:
        return _default_model_for_backend(default_model, backend=backend)

    resolved = _resolve_llm_backend_for_models(backend)
    # Recognize the current shipped default AND prior-release shipped defaults
    # (#1324): a config persisted before a pin bump still holds the old literal,
    # and for Claude-incapable backends it must normalize to the sentinel just
    # like the current default would. Genuinely explicit, never-shipped ids are
    # absent from this set and fall through to be preserved verbatim.
    is_shipped_default = candidate in (
        *recognized_shipped_defaults(default_model),
        *extra_shipped_defaults,
    )
    if resolved in _CODEX_LLM_BACKENDS and is_shipped_default:
        return _CODEX_DEFAULT_MODEL
    if resolved in _KIRO_LLM_BACKENDS and is_shipped_default:
        return _KIRO_DEFAULT_MODEL
    if resolved in _COPILOT_LLM_BACKENDS and is_shipped_default:
        return _COPILOT_DEFAULT_MODEL
    if resolved in _HERMES_LLM_BACKENDS and is_shipped_default:
        return _HERMES_DEFAULT_MODEL
    if resolved in _PI_LLM_BACKENDS and is_shipped_default:
        return _PI_DEFAULT_MODEL
    if resolved in _GJC_LLM_BACKENDS and is_shipped_default:
        return _GJC_DEFAULT_MODEL
    if resolved in _ANTIGRAVITY_LLM_BACKENDS and is_shipped_default:
        return _ANTIGRAVITY_DEFAULT_MODEL
    if resolved in _GROK_LLM_BACKENDS and is_shipped_default:
        return _GROK_DEFAULT_MODEL

    return candidate


def _normalize_configured_models_for_backend(
    configured_models: tuple[str, ...] | list[str],
    *,
    default_models: tuple[str, ...],
    backend: str | None = None,
) -> tuple[str, ...]:
    """Normalize config-backed model rosters while preserving explicit overrides."""
    normalized = tuple(model.strip() for model in configured_models if model.strip())
    if not normalized:
        return _default_models_for_backend(default_models, backend=backend)

    # Match the shipped roster element-wise against current + legacy shipped
    # defaults (#1324), so a roster persisted before a pin bump (e.g. the old
    # OpenRouter Opus slug in the consensus slot) still normalizes to the
    # backend-safe sentinel for Claude-incapable backends instead of leaking an
    # unrunnable id.
    is_shipped_roster = len(normalized) == len(default_models) and all(
        candidate in recognized_shipped_defaults(default)
        for candidate, default in zip(normalized, default_models, strict=True)
    )
    if (
        _resolve_llm_backend_for_models(backend)
        in (_CODEX_LLM_BACKENDS | _COPILOT_LLM_BACKENDS | _HERMES_LLM_BACKENDS)
        and is_shipped_roster
    ):
        return _default_models_for_backend(default_models, backend=backend)

    return normalized


def _parse_model_list(value: str) -> tuple[str, ...]:
    """Parse a comma-separated model list from an environment variable."""
    return tuple(part.strip() for part in value.split(",") if part.strip())


def get_clarification_model(backend: str | None = None) -> str:
    """Get clarification model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_CLARIFICATION_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.clarification.default_model,
            default_model=DEFAULT_OPUS_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_OPUS_MODEL, backend=backend)


def get_qa_model(backend: str | None = None) -> str:
    """Get QA model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_QA_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.qa_model,
            default_model=DEFAULT_SONNET_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_SONNET_MODEL, backend=backend)


def get_dependency_analysis_model(backend: str | None = None) -> str:
    """Get dependency analysis model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_DEPENDENCY_ANALYSIS_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.dependency_analysis_model,
            default_model=DEFAULT_SONNET_MODEL,
            backend=backend,
            extra_shipped_defaults=recognized_shipped_defaults(DEFAULT_OPUS_MODEL),
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_SONNET_MODEL, backend=backend)


def get_ontology_analysis_model(backend: str | None = None) -> str:
    """Get ontology analysis model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_ONTOLOGY_ANALYSIS_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.ontology_analysis_model,
            default_model=DEFAULT_SONNET_MODEL,
            backend=backend,
            extra_shipped_defaults=recognized_shipped_defaults(DEFAULT_OPUS_MODEL),
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_SONNET_MODEL, backend=backend)


def get_context_compression_model(backend: str | None = None) -> str:
    """Get workflow context compression model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_CONTEXT_COMPRESSION_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.context_compression_model,
            default_model="gpt-4",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("gpt-4", backend=backend)


def get_wonder_model(backend: str | None = None) -> str:
    """Get Wonder model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_WONDER_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.resilience.wonder_model,
            default_model=DEFAULT_OPUS_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_OPUS_MODEL, backend=backend)


def get_reflect_model(backend: str | None = None) -> str:
    """Get Reflect model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_REFLECT_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.resilience.reflect_model,
            default_model=DEFAULT_OPUS_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_OPUS_MODEL, backend=backend)


def get_semantic_model(backend: str | None = None) -> str:
    """Get semantic evaluation model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_SEMANTIC_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.evaluation.semantic_model,
            default_model=DEFAULT_OPUS_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_OPUS_MODEL, backend=backend)


def get_assertion_extraction_model(backend: str | None = None) -> str:
    """Get verification assertion extraction model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_ASSERTION_EXTRACTION_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.evaluation.assertion_extraction_model,
            default_model=DEFAULT_SONNET_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(DEFAULT_SONNET_MODEL, backend=backend)


def get_mechanical_detector_model(backend: str | None = None) -> str:
    """Resolve the model used by the mechanical.toml AI detector.

    The public helper remains for legacy imports, but the configured model
    source is now the Evaluate stage model (``evaluation.semantic_model``).
    """
    env_model = os.environ.get("OUROBOROS_DETECTOR_MODEL", "").strip()
    if env_model:
        return env_model
    return get_llm_model_for_role("mechanical_detection", backend=backend)


def get_consensus_models(backend: str | None = None) -> tuple[str, ...]:
    """Get consensus stage model roster from environment variable or config."""
    env_models = os.environ.get("OUROBOROS_CONSENSUS_MODELS", "").strip()
    if env_models:
        parsed = _parse_model_list(env_models)
        if parsed:
            return parsed

    try:
        config = load_config()
        if config.consensus.models:
            return _normalize_configured_models_for_backend(
                config.consensus.models,
                default_models=_DEFAULT_CONSENSUS_MODELS,
                backend=backend,
            )
    except ConfigError:
        pass

    return _default_models_for_backend(_DEFAULT_CONSENSUS_MODELS, backend=backend)


def get_consensus_advocate_model(backend: str | None = None) -> str:
    """Get deliberative advocate model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_CONSENSUS_ADVOCATE_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.consensus.advocate_model,
            default_model=_DEFAULT_CONSENSUS_ADVOCATE_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(_DEFAULT_CONSENSUS_ADVOCATE_MODEL, backend=backend)


def get_consensus_devil_model(backend: str | None = None) -> str:
    """Get deliberative devil model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_CONSENSUS_DEVIL_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.consensus.devil_model,
            default_model=_DEFAULT_CONSENSUS_DEVIL_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(_DEFAULT_CONSENSUS_DEVIL_MODEL, backend=backend)


def get_consensus_judge_model(backend: str | None = None) -> str:
    """Get deliberative judge model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_CONSENSUS_JUDGE_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.consensus.judge_model,
            default_model=_DEFAULT_CONSENSUS_JUDGE_MODEL,
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend(_DEFAULT_CONSENSUS_JUDGE_MODEL, backend=backend)
