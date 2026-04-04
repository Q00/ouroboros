"""Configuration loading and management for Ouroboros.

This module provides functions for loading, creating, and validating
Ouroboros configuration files.

Functions:
    load_config: Load configuration from ~/.ouroboros/config.yaml
    load_credentials: Load credentials from ~/.ouroboros/credentials.yaml
    create_default_config: Create default configuration files
    ensure_config_dir: Ensure ~/.ouroboros/ directory exists
    get_agent_runtime_backend: Get orchestrator runtime backend from env var or config
    get_agent_permission_mode: Get orchestrator permission mode from env var or config
    get_llm_backend: Get LLM-only backend from env var or config
    get_llm_permission_mode: Get LLM-only permission mode from env var or config
    get_clarification_model: Get clarification model from env var or config
    get_qa_model: Get QA model from env var or config
    get_dependency_analysis_model: Get dependency analysis model from env var or config
    get_ontology_analysis_model: Get ontology analysis model from env var or config
    get_context_compression_model: Get context compression model from env var or config
    get_atomicity_model: Get atomicity model from env var or config
    get_decomposition_model: Get decomposition model from env var or config
    get_double_diamond_model: Get Double Diamond model from env var or config
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
"""

import os
from pathlib import Path
import stat
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError as PydanticValidationError
import yaml

# Load .env file from current directory and ~/.ouroboros/
load_dotenv()  # Current directory .env
load_dotenv(Path.home() / ".ouroboros" / ".env")  # Global .env

from ouroboros.config.models import (  # noqa: E402
    CredentialsConfig,
    OuroborosConfig,
    get_config_dir,
    get_default_config,
    get_default_credentials,
)
from ouroboros.core.errors import ConfigError  # noqa: E402

_CODEX_LLM_BACKENDS = frozenset({"codex", "codex_cli", "opencode", "opencode_cli"})
_OPENCODE_BACKENDS = frozenset({"opencode", "opencode_cli"})
_CODEX_DEFAULT_MODEL = "default"
_DEFAULT_CONSENSUS_MODELS = (
    "openrouter/openai/gpt-4o",
    "openrouter/anthropic/claude-opus-4-6",
    "openrouter/google/gemini-2.5-pro",
)
_DEFAULT_CONSENSUS_ADVOCATE_MODEL = "openrouter/anthropic/claude-opus-4-6"
_DEFAULT_CONSENSUS_DEVIL_MODEL = "openrouter/openai/gpt-4o"
_DEFAULT_CONSENSUS_JUDGE_MODEL = "openrouter/google/gemini-2.5-pro"


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
        error_messages = []
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            msg = error["msg"]
            error_messages.append(f"  - {loc}: {msg}")

        raise ConfigError(
            "Configuration validation failed:\n" + "\n".join(error_messages),
            config_file=str(config_path),
            details={"validation_errors": e.errors()},
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
        2. config.yaml orchestrator.runtime_backend
        3. "claude"

    Returns:
        Normalized runtime backend name.
    """
    env_backend = os.environ.get("OUROBOROS_AGENT_RUNTIME", "").strip().lower()
    if env_backend:
        return env_backend

    try:
        config = load_config()
        return config.orchestrator.runtime_backend
    except ConfigError:
        return "claude"


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


def get_gemini_cli_path() -> str | None:
    """Get Gemini CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_GEMINI_CLI_PATH environment variable
        2. config.yaml orchestrator.gemini_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to Gemini CLI binary or None.
    """
    env_path = os.environ.get("OUROBOROS_GEMINI_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        gemini_path = getattr(config.orchestrator, "gemini_cli_path", None)
        if gemini_path:
            return gemini_path
    except ConfigError:
        pass

    return None


def get_llm_backend() -> str:
    """Get default LLM backend from environment variable or config.

    Priority:
        1. OUROBOROS_LLM_BACKEND environment variable
        2. config.yaml llm.backend
        3. "claude_code"

    Returns:
        Normalized LLM backend name.
    """
    env_backend = os.environ.get("OUROBOROS_LLM_BACKEND", "").strip().lower()
    if env_backend:
        return env_backend

    try:
        config = load_config()
        return config.llm.backend
    except ConfigError:
        return "claude_code"


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
    if _resolve_llm_backend_for_models(backend) in _CODEX_LLM_BACKENDS:
        return _CODEX_DEFAULT_MODEL
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
) -> str:
    """Normalize config-backed models while preserving backend-safe defaults."""
    candidate = configured_model.strip()
    if not candidate:
        return _default_model_for_backend(default_model, backend=backend)

    if (
        _resolve_llm_backend_for_models(backend) in _CODEX_LLM_BACKENDS
        and candidate == default_model
    ):
        return _CODEX_DEFAULT_MODEL

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

    if (
        _resolve_llm_backend_for_models(backend) in _CODEX_LLM_BACKENDS
        and normalized == default_models
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
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_qa_model(backend: str | None = None) -> str:
    """Get QA model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_QA_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.qa_model,
            default_model="claude-sonnet-4-20250514",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-sonnet-4-20250514", backend=backend)


def get_dependency_analysis_model(backend: str | None = None) -> str:
    """Get dependency analysis model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_DEPENDENCY_ANALYSIS_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.dependency_analysis_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_ontology_analysis_model(backend: str | None = None) -> str:
    """Get ontology analysis model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_ONTOLOGY_ANALYSIS_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.llm.ontology_analysis_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


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


def get_atomicity_model(backend: str | None = None) -> str:
    """Get atomicity analysis model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_ATOMICITY_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.execution.atomicity_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_decomposition_model(backend: str | None = None) -> str:
    """Get AC decomposition model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_DECOMPOSITION_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.execution.decomposition_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_double_diamond_model(backend: str | None = None) -> str:
    """Get Double Diamond default model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_DOUBLE_DIAMOND_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.execution.double_diamond_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_wonder_model(backend: str | None = None) -> str:
    """Get Wonder model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_WONDER_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.resilience.wonder_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_reflect_model(backend: str | None = None) -> str:
    """Get Reflect model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_REFLECT_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.resilience.reflect_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_semantic_model(backend: str | None = None) -> str:
    """Get semantic evaluation model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_SEMANTIC_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.evaluation.semantic_model,
            default_model="claude-opus-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-opus-4-6", backend=backend)


def get_assertion_extraction_model(backend: str | None = None) -> str:
    """Get verification assertion extraction model from environment variable or config."""
    env_model = os.environ.get("OUROBOROS_ASSERTION_EXTRACTION_MODEL", "").strip()
    if env_model:
        return env_model

    try:
        config = load_config()
        return _normalize_configured_model_for_backend(
            config.evaluation.assertion_extraction_model,
            default_model="claude-sonnet-4-6",
            backend=backend,
        )
    except ConfigError:
        return _default_model_for_backend("claude-sonnet-4-6", backend=backend)


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
