"""Configuration loading and management for Ouroboros.

This module provides functions for loading, creating, and validating
Ouroboros configuration files.

Functions:
    load_config: Load configuration from ~/.ouroboros/config.yaml
    load_credentials: Load credentials from ~/.ouroboros/credentials.yaml
    create_default_config: Create default configuration files
    ensure_config_dir: Ensure ~/.ouroboros/ directory exists
    get_cli_path: Get CLI path from env var or config
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

from ouroboros.config.models import (
    CredentialsConfig,
    OuroborosConfig,
    get_config_dir,
    get_default_config,
    get_default_credentials,
)
from ouroboros.core.errors import ConfigError


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
    return (config_dir / "config.yaml").exists() and (
        config_dir / "credentials.yaml"
    ).exists()


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
    """Get CLI path from environment variable or config file.

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
