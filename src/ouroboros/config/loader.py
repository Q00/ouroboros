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
    get_zcode_cli_path: Get zcode CLI path from env var or config
"""

from collections.abc import Callable
import math
import os
from pathlib import Path
import shutil
import stat
from typing import Any

from dotenv import dotenv_values
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
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key
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
# Zcode (Z.ai GLM-5) is runtime-only and Claude-incapable: it runs its own
# configured default model, so generic Claude default ids map to the CLI's
# own ``"default"`` sentinel, exactly like antigravity and grok above.
_ZCODE_LLM_BACKENDS = frozenset({"zcode", "zcode_cli"})
# OMP (Oh My Pi, the ``omp`` CLI): Pi-family agent; its model comes from its
# own config/roles, so generic Claude defaults map to the ``"default"`` sentinel.
_OMP_LLM_BACKENDS = frozenset({"omp", "omp_cli"})
# Every backend whose default model is the backend-safe ``"default"`` sentinel
# rather than a runnable shipped id, because the CLI selects its model via
# config (not a ``--model`` flag). Roster-level normalization must cover the
# same set as the element-wise mapping in ``_default_model_for_backend``;
# otherwise a shipped default roster leaks unrunnable ids for any backend
# added after the original Codex/Copilot/Hermes trio.
_SENTINEL_DEFAULT_BACKENDS = (
    _CODEX_LLM_BACKENDS
    | _KIRO_LLM_BACKENDS
    | _COPILOT_LLM_BACKENDS
    | _HERMES_LLM_BACKENDS
    | _PI_LLM_BACKENDS
    | _GJC_LLM_BACKENDS
    | _ANTIGRAVITY_LLM_BACKENDS
    | _GROK_LLM_BACKENDS
    | _ZCODE_LLM_BACKENDS
    | _OMP_LLM_BACKENDS
)
_ZCODE_SCRIPT_SUFFIXES = frozenset({".cjs", ".js", ".mjs"})
_OPENCODE_BACKENDS = frozenset({"opencode", "opencode_cli"})
_CODEX_DEFAULT_MODEL = "default"
_KIRO_DEFAULT_MODEL = "default"
_COPILOT_DEFAULT_MODEL = "default"
_HERMES_DEFAULT_MODEL = "default"
_PI_DEFAULT_MODEL = "default"
_GJC_DEFAULT_MODEL = "default"
_ANTIGRAVITY_DEFAULT_MODEL = "default"
_GROK_DEFAULT_MODEL = "default"
_ZCODE_DEFAULT_MODEL = "default"
_OMP_DEFAULT_MODEL = "default"
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
MAX_USAGE_LIMIT_PAUSE_SECONDS = 365 * 24 * _SECONDS_PER_HOUR
_USAGE_LIMIT_PAUSE_CONFIG_KEY = "orchestrator.usage_limit_pause_hours"
_RUNTIME_CONTROL_ENV_KEYS = {
    "OUROBOROS_MCP_TOOL_TIMEOUT_SECONDS": "mcp_tool_timeout_seconds",
    "OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS": "generation_idle_timeout_seconds",
    "OUROBOROS_GENERATION_NO_PROGRESS_TIMEOUT_SECONDS": ("generation_no_progress_timeout_seconds"),
    "OUROBOROS_GENERATION_SAFETY_TIMEOUT_SECONDS": "generation_safety_timeout_seconds",
    "OUROBOROS_WATCHDOG_POLL_SECONDS": "watchdog_poll_seconds",
}


def _is_assignable_env_key(key: str) -> bool:
    """Return whether `key` can be written to ``os.environ`` at all.

    python-dotenv's grammar is wider than the environment's. A quoted
    left-hand side such as ``'BROKEN=KEY'=value`` parses to the key
    ``BROKEN=KEY``, and CPython rejects any name containing ``=`` or NUL with
    ``ValueError: illegal environment variable name``.

    This module runs ``_load_env_file`` at import, so an unhandled rejection
    there would stop every Ouroboros command from starting -- a denial of
    service reachable from a cloned repository's ``.env``. Skipping the entry
    keeps startup alive and matches the previous parser, which also refused
    keys containing whitespace.
    """
    return bool(key) and not any(ch == "=" or ch == "\0" or ch.isspace() for ch in key)


def _is_placeholder_api_key(value: str) -> bool:
    """Treat common template placeholders as unset."""
    candidate = value.strip()
    return bool(
        candidate
        and candidate.startswith(_PLACEHOLDER_API_KEY_PREFIX)
        and candidate.endswith(_PLACEHOLDER_API_KEY_SUFFIX)
    )


# The reasoning-effort vocabulary every native runtime accepts (mirrors
# OrchestratorConfig.reasoning_effort). A value outside this set — Codex-only
# ``minimal``, Claude-only ``max``, or a typo — must never reach a runtime, so the
# env override is validated against it before use.
_VALID_REASONING_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh"})


def _load_env_file(path: Path, *, trusted: bool = False) -> None:
    """Apply a ``.env`` file to ``os.environ`` under this module's trust policy.

    Grammar is delegated to ``python-dotenv``, which owns it. The previous
    hand-rolled parser routed quoted values through :func:`ast.literal_eval`,
    applying *Python* string syntax to a POSIX-shaped file: `X='C:\\new\\table'`
    came back ten characters long with a newline and a tab in it, because single
    quotes are literal everywhere except in Python. Escapes, comments, quoting,
    and `export ` are now the library's problem.

    Every policy decision stays here:

    * the untrusted-source key denylist (RCE guard) is applied per key;
    * template placeholder keys are skipped;
    * an already-set real environment value is never overridden.

    ``interpolate=False`` is deliberate. python-dotenv expands ``${VAR}`` from
    the environment by default; leaving that on would both change today's
    behaviour (``$HOME`` is currently literal) and hand an untrusted
    project-directory ``.env`` a way to read the real process environment into
    a value it controls.
    """
    if not path.is_file():
        return

    try:
        entries = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # An unreadable or non-UTF-8 .env is not fatal; the process
        # environment still wins. `UnicodeDecodeError` is a `ValueError`, not
        # an `OSError`, so it needs naming: a single invalid byte
        # (`BROKEN=\xFF`) in a cloned repository would otherwise abort import.
        return
    except Exception:  # noqa: BLE001 - startup must survive any parser failure
        # This module runs `_load_env_file` at import, so a malformed `.env`
        # must degrade to "no variables loaded", never to an unstartable
        # process. Enumerating the parser's failure modes has already missed
        # two -- an illegal key name and a decoding error -- so the invariant
        # is enforced here rather than predicted.
        return

    for key, parsed_value in entries.items():
        # A bare `KEY` line with no `=` yields None, matching the old skip.
        if not key or parsed_value is None:
            continue

        if not _is_assignable_env_key(key):
            continue

        if not trusted and is_untrusted_env_denied_key(key):
            # Untrusted project-directory .env must not redirect which
            # binary Ouroboros executes (remote code execution guard).
            continue

        if not parsed_value or _is_placeholder_api_key(parsed_value):
            continue

        current_value = os.environ.get(key)
        if current_value is None or _is_placeholder_api_key(current_value):
            try:
                os.environ[key] = parsed_value
            except ValueError:
                # Defence in depth. `_is_assignable_env_key` already rejects
                # everything CPython refuses, but this module is imported at
                # startup: a future parser change must degrade to skipping one
                # entry, never to an unstartable process.
                continue


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
    with config_path.open("w", encoding="utf-8") as f:
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
    with credentials_path.open("w", encoding="utf-8") as f:
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
        with config_path.open(encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)
    except (UnicodeDecodeError, yaml.YAMLError) as e:
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
        with credentials_path.open(encoding="utf-8") as f:
            credentials_dict = yaml.safe_load(f)
    except (UnicodeDecodeError, yaml.YAMLError) as e:
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
        3. None (let the active Claude runtime resolve its default)

    Returns:
        Path to CLI binary or None to use the active runtime default.
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

    # 3. Default: None (the selected Claude runtime resolves its own CLI)
    return None


def get_agent_runtime_backend() -> str:
    """Get orchestrator runtime backend from environment variable or config.

    Priority:
        1. OUROBOROS_AGENT_RUNTIME environment variable
        2. OUROBOROS_RUNTIME environment variable
        3. config.yaml orchestrator.runtime_backend
        4. "claude" (Claude Agent SDK runtime)

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
    """Return explicit process-level opt-in for cross-harness mutation.

    Historical releases materialized ``true`` into generated config files, so
    a persisted scalar cannot distinguish operator consent from the old default.
    Only the environment flag is therefore authoritative during the deprecation
    window; absent explicit process consent, one run keeps one runtime.
    """
    env = _env_flag("OUROBOROS_CROSS_HARNESS_REDISPATCH")
    return env if env is not None else False


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


def get_execution_model() -> str | None:
    """Return an explicit Execute-stage model pin, if one was configured.

    Environment remains the one-off highest-priority override.  The web/TUI
    setting writes ``execution.default_model``; an empty value or the UI's
    ``default``/``current`` sentinel deliberately means "let this runtime pick"
    rather than a model named ``default``.
    """
    env_model = os.environ.get("OUROBOROS_EXECUTION_MODEL")
    if env_model is not None:
        stripped = env_model.strip()
        return None if not stripped or stripped.lower() in {"default", "current"} else stripped
    try:
        model = load_config().execution.default_model
    except ConfigError:
        return None
    if model is None:
        return None
    stripped = model.strip()
    return None if not stripped or stripped.lower() in {"default", "current"} else stripped


def resolve_execution_model(runtime_backend: str | None) -> str | None:
    """Resolve the exact Execute-stage model pin shared by CLI, MCP, and config views."""
    execution_model = get_execution_model()
    if execution_model is not None:
        return execution_model
    if (runtime_backend or "").strip().lower() in {"claude", "claude_code"}:
        return DEFAULT_SONNET_MODEL
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
    except (TypeError, ValueError, OverflowError) as exc:
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


def _bounded_usage_limit_pause_seconds(hours: float, *, config_key: str) -> int:
    """Convert a validated hour value into one finite durable policy value."""
    seconds = hours * _SECONDS_PER_HOUR
    if not math.isfinite(seconds) or seconds > MAX_USAGE_LIMIT_PAUSE_SECONDS:
        raise ConfigError(
            f"{config_key} must not exceed 365 days",
            config_key=config_key,
            details={
                "value": hours,
                "max_seconds": MAX_USAGE_LIMIT_PAUSE_SECONDS,
            },
        )
    return max(1, int(seconds))


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
        return _bounded_usage_limit_pause_seconds(
            hours,
            config_key="OUROBOROS_USAGE_LIMIT_PAUSE_HOURS",
        )

    config_path = get_config_dir() / "config.yaml"
    if not config_path.exists():
        # No config file means no pause-window override; use the built-in default.
        return int(_DEFAULT_USAGE_LIMIT_PAUSE_HOURS * _SECONDS_PER_HOUR)

    try:
        with config_path.open(encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)
    except (UnicodeDecodeError, yaml.YAMLError) as e:
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
    return _bounded_usage_limit_pause_seconds(
        hours,
        config_key=_USAGE_LIMIT_PAUSE_CONFIG_KEY,
    )


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
        with config_path.open(encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)
    except (UnicodeDecodeError, yaml.YAMLError) as e:
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
    """Return whether execute_seed runs should enqueue formal evaluation."""
    try:
        return load_config().execution.auto_evaluate
    except ConfigError:
        return True


def get_auto_evolve_enabled() -> bool:
    """Return whether rejected formal evaluations should enqueue Ralph."""

    try:
        return load_config().execution.auto_evolve
    except ConfigError:
        return True


def default_execution_efficiency_mode() -> str | None:
    """Map ``execution.default_policy`` to a fresh-start efficiency mode.

    ``None`` — for ``ask`` or an unreadable config — preserves the
    interactive-prompt contract exactly (#1733). ``efficient`` and
    ``quality_first`` return the efficiency mode whose documented coupling
    supplies the paired frugality default (adaptive/observe and
    quality_first/off); strict assurance never derives from here. Explicit
    invocation arguments take precedence at the call sites, and resumed
    sessions never consult this.
    """
    try:
        policy = load_config().execution.default_policy
    except ConfigError:
        return None
    if policy == "efficient":
        return "adaptive"
    if policy == "quality_first":
        return "quality_first"
    return None


def get_auto_evolve_max_generations() -> int:
    """Return the bounded generation budget for automatic Ralph chaining."""

    try:
        value = load_config().execution.auto_evolve_max_generations
    except ConfigError:
        return 3
    return max(1, min(10, value))


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


def get_configured_verify_bash_path() -> str | None:
    """Get ``orchestrator.verify_bash_path`` — config only, never the env.

    The usual env-then-config accessor shape is deliberately not used here.
    :func:`ouroboros.orchestrator.verify_shell.resolve_verify_shell` reads
    ``OUROBOROS_VERIFY_BASH`` itself, and reaches this function only after
    finding that value stale; an accessor that returned the environment first
    would hand back the same stale path and hide the configured shell entirely.
    Executability is checked by that caller, which falls through to its own
    candidate list when the configured value no longer resolves.

    Returns:
        Configured shell path or None.
    """
    try:
        config = load_config()
        verify_bash_path = getattr(config.orchestrator, "verify_bash_path", None)
        if verify_bash_path:
            return str(Path(verify_bash_path).expanduser())
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


def __getattr__(name: str) -> object:
    """Lazy re-exports for the OMP CLI-path helpers (module-size split)."""
    if name in ("get_omp_cli_path", "resolve_omp_cli_path"):
        from ouroboros.config import _omp_cli

        return getattr(_omp_cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def get_dsh_cli_path() -> str | None:
    """Get the DeepSeek Harness ACP server path from env or config file.

    Priority:
        1. OUROBOROS_DSH_CLI_PATH environment variable
        2. config.yaml orchestrator.dsh_cli_path
        3. None (resolve ``dsh-acp-demo`` from PATH at runtime)

    Returns:
        Path to the ``dsh-acp-demo`` executable or None.
    """
    env_path = os.environ.get("OUROBOROS_DSH_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.dsh_cli_path:
            return config.orchestrator.dsh_cli_path
    except ConfigError:
        pass

    return None


def get_dsh_config_path() -> str | None:
    """Get the dsh Cordis composition file path from env or config file.

    Priority:
        1. OUROBOROS_DSH_CONFIG_PATH environment variable
        2. config.yaml orchestrator.dsh_config_path
        3. None (the dsh client fails closed before spawning)

    Returns:
        Path to the trusted composition YAML, or None when dsh is not configured.
    """
    env_path = os.environ.get("OUROBOROS_DSH_CONFIG_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.dsh_config_path:
            return config.orchestrator.dsh_config_path
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


def get_telemetry_enabled() -> bool:
    """Whether anonymous usage telemetry may send events.

    Every control that can disable telemetry is resolved first; any one of
    them wins unconditionally (TELEMETRY.md: "Any one of these disables
    telemetry completely"):

        1. DO_NOT_TRACK environment variable (any truthy value disables)
        2. OUROBOROS_TELEMETRY=0/false/off/no
        3. config.yaml telemetry.enabled: false
        4. invalid or unreadable configuration (fails closed)

    Only when no disabling source is present does telemetry run (default:
    on, with first-run notice and TELEMETRY.md contract). An explicit
    ``OUROBOROS_TELEMETRY=1`` is therefore never an override: it cannot
    re-enable collection against a persisted opt-out or malformed
    configuration. A privacy preference can be present in a file whose
    unrelated field no longer validates; it is never safe to turn collection
    back on merely because the full application config could not be
    constructed.
    """
    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in ("1", "true", "on", "yes"):
        return False
    if _env_flag("OUROBOROS_TELEMETRY") is False:
        return False
    config_path = get_config_dir() / "config.yaml"
    # ``Path.exists()`` is false for a dangling symlink. Treat that as invalid
    # persisted configuration rather than the genuinely-absent default-on
    # case; ``load_config`` below will reject the unreadable target.
    if not config_path.exists() and not config_path.is_symlink():
        return True
    try:
        return load_config(config_path).telemetry.enabled
    except (ConfigError, OSError):
        return False


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


def _is_runnable_zcode_cli_path(path: str | Path) -> bool:
    """Whether *path* matches one of the supported Zcode launch shapes."""
    candidate = Path(path)
    if not candidate.is_file():
        return False
    if candidate.suffix.lower() in _ZCODE_SCRIPT_SUFFIXES:
        return os.access(candidate, os.R_OK)
    return shutil.which(str(candidate.resolve())) is not None


def _canonical_zcode_cli_path(path: str | Path) -> str:
    """Return a stable path that remains valid after the caller changes cwd."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve()
    return str(candidate)


def get_zcode_cli_path() -> str | None:
    """Get the zcode CLI path (Z.ai GLM-5 agent) from environment or config.

    Priority:
        1. OUROBOROS_ZCODE_CLI_PATH environment variable
        2. config.yaml orchestrator.zcode_cli_path
        3. macOS app-bundle default (/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs)
        4. None (resolve from PATH at runtime)

    The returned path may point to the app-bundle ``zcode.cjs`` script or to a
    directly executable wrapper. Official macOS app bundles declare an
    ``electron-node`` runtime in sibling metadata; the runtime wrapper then
    uses ZCode's bundled Electron/Node executable instead of the system Node.

    Stale env var / config values that don't point to a readable JavaScript
    entry script or a directly executable wrapper are treated as missing, so
    callers can fall back to PATH discovery instead of persisting an unusable
    path.

    Returns:
        Path to the zcode CLI script or None.
    """
    env_path = os.environ.get("OUROBOROS_ZCODE_CLI_PATH", "").strip()
    if env_path:
        resolved = _canonical_zcode_cli_path(env_path)
        if _is_runnable_zcode_cli_path(resolved):
            return resolved

    try:
        config = load_config()
        zcode_path = getattr(config.orchestrator, "zcode_cli_path", None)
        if zcode_path:
            resolved = _canonical_zcode_cli_path(zcode_path)
            if _is_runnable_zcode_cli_path(resolved):
                return resolved
    except ConfigError:
        pass

    # macOS app-bundle default
    macos_bundle_path = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
    if _is_runnable_zcode_cli_path(macos_bundle_path):
        return str(macos_bundle_path)

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
        fallback = _explicit_llm_backend_override() or fallback_runtime_backend or get_llm_backend()
        return _guard_llm_completion_backend(fallback)

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
        fallback = _explicit_llm_backend_override() or fallback_runtime_backend or get_llm_backend()
        return _guard_llm_completion_backend(fallback)

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
    if resolved in _ZCODE_LLM_BACKENDS:
        return _ZCODE_DEFAULT_MODEL
    if resolved in _OMP_LLM_BACKENDS:
        return _OMP_DEFAULT_MODEL
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

    # Recognize the current shipped default AND prior-release shipped defaults
    # (#1324): a config persisted before a pin bump still holds the old literal,
    # and it must normalize exactly like the current default would. Genuinely
    # explicit, never-shipped ids are absent from this set and are preserved
    # verbatim.
    is_shipped_default = candidate in (
        *recognized_shipped_defaults(default_model),
        *extra_shipped_defaults,
    )
    if is_shipped_default:
        # A recognized shipped default — current or prior-release — is a pin
        # the user never chose, so every backend maps it to its own default:
        # Claude-incapable backends keep their sentinel as before, and
        # Claude-capable backends now take the current default pin instead of
        # leaking a retired id to the API (#2069). Never-shipped ids are
        # deliberate user pins and fall through verbatim.
        return _default_model_for_backend(default_model, backend=backend)

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
    # OpenRouter Opus slug in the consensus slot) resolves exactly like the
    # current shipped roster. Claude-incapable backends receive their safe
    # sentinel; Claude-capable backends receive the current provider pin rather
    # than replaying a retired model id.
    is_shipped_roster = len(normalized) == len(default_models) and all(
        candidate in recognized_shipped_defaults(default)
        for candidate, default in zip(normalized, default_models, strict=True)
    )
    if is_shipped_roster:
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
