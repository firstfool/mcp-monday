"""Configuration management for Monday.com MCP Server."""

import os
import stat
import warnings
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SyncConfig(BaseModel):
    """Sync database configuration — controls the SQLite change-detection store."""

    model_config = ConfigDict(populate_by_name=True)

    db_path: str = Field(
        default="/tmp/monday_sync.db",
        description="SQLite file path, or ':memory:' for ephemeral in-process store",
    )
    max_age_hours: int = Field(
        default=168,
        ge=1,
        le=8760,  # 1 year upper bound
        description="Force re-sync if local DB is older than this many hours (safety valve)",
    )
    items_page_size: int = Field(
        default=500,
        ge=1,
        le=500,
        description="Items per API page during full sync (Monday.com max is 500)",
    )
    max_sync_wait_seconds: int = Field(
        default=120,
        ge=1,
        description="Max seconds to wait for the sync lock before returning stale data or SYNC_IN_PROGRESS",
    )


class MondayConfig(BaseModel):
    """Monday.com connection configuration with secure credential handling."""

    model_config = ConfigDict(populate_by_name=True)

    api_key: SecretStr = Field(..., description="Monday.com API key (Personal API token)")
    workspace_url: Optional[str] = Field(
        default=None,
        description="Monday.com workspace base URL (e.g. https://mycompany.monday.com)",
    )
    timeout: int = Field(default=30, ge=5, le=300, description="Request timeout in seconds")
    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")

    def get_api_key(self) -> str:
        """Get the API key as plain string (use carefully)."""
        return self.api_key.get_secret_value()

    @field_validator("workspace_url")
    @classmethod
    def validate_workspace_url(cls, v: Optional[str]) -> Optional[str]:
        """Normalise workspace URL."""
        if v is None:
            return v
        v = v.rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("workspace_url must start with http:// or https://")
        return v


class LoggingConfig(BaseModel):
    """Logging configuration."""

    model_config = ConfigDict(populate_by_name=True)

    level: str = Field(default="INFO", description="Log level")
    format: str = Field(default="json", description="Log format: json or text")

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v = v.upper()
        if v not in valid_levels:
            raise ValueError(f"level must be one of {valid_levels}")
        return v


class ServerConfig(BaseModel):
    """Complete server configuration."""

    model_config = ConfigDict(populate_by_name=True)

    monday: MondayConfig
    sync: SyncConfig = Field(default_factory=SyncConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    environment: str = Field(default="production", description="Environment name")


def load_config(config_path: Optional[str] = None) -> ServerConfig:
    """
    Load configuration from YAML file and environment variables.

    Environment variables always take precedence over YAML configuration.

    Args:
        config_path: Path to YAML config file (default: config.yaml)

    Returns:
        ServerConfig instance

    Raises:
        ValueError: If configuration is invalid (e.g. missing API key)
    """
    if config_path is None:
        config_path = os.getenv("MCP_MONDAY_CONFIG_PATH", "config.yaml")

    config_data: dict = {}
    config_file = Path(config_path)

    if config_file.exists():
        # Warn if config file is world-readable on Unix
        if hasattr(os, "stat") and hasattr(stat, "S_IROTH"):
            stat_info = config_file.stat()
            if stat_info.st_mode & stat.S_IROTH:
                warnings.warn(
                    f"Config file {config_path} is readable by others. "
                    "Consider restricting permissions: chmod 600",
                    UserWarning,
                    stacklevel=2,
                )

        with open(config_file) as f:
            config_data = yaml.safe_load(f) or {}

    monday_data = config_data.get("monday", {})

    # Environment variables always win; also accept legacy MONDAY_API_KEY
    api_key = (
        os.getenv("MCP_MONDAY_API_KEY")
        or os.getenv("MONDAY_API_KEY")
        or monday_data.get("api_key")
    )
    if not api_key:
        raise ValueError(
            "MCP_MONDAY_API_KEY is required. "
            "Set it in your environment or in config.yaml under monday.api_key."
        )

    monday_config: dict = {
        "api_key": api_key,
        "workspace_url": (
            os.getenv("MCP_MONDAY_WORKSPACE_URL")
            or os.getenv("MONDAY_WORKSPACE_URL")
            or monday_data.get("workspace_url")
        ),
        "timeout": int(os.getenv("MCP_MONDAY_TIMEOUT", monday_data.get("timeout", 30))),
        "max_retries": int(os.getenv("MCP_MONDAY_MAX_RETRIES", monday_data.get("max_retries", 3))),
    }

    logging_data = config_data.get("logging", {})
    logging_config: dict = {
        "level": os.getenv("MCP_MONDAY_LOG_LEVEL", logging_data.get("level", "INFO")),
        "format": os.getenv("MCP_MONDAY_LOG_FORMAT", logging_data.get("format", "json")),
    }

    sync_data = config_data.get("sync", {})
    sync_config: dict = {
        "db_path": os.getenv(
            "MCP_MONDAY_SYNC_DB_PATH",
            sync_data.get("db_path", "/tmp/monday_sync.db"),
        ),
        "max_age_hours": int(
            os.getenv("MCP_MONDAY_SYNC_MAX_AGE_HOURS", sync_data.get("max_age_hours", 24))
        ),
        "items_page_size": int(
            os.getenv("MCP_MONDAY_SYNC_PAGE_SIZE", sync_data.get("items_page_size", 500))
        ),
        "max_sync_wait_seconds": int(
            os.getenv("MCP_MONDAY_SYNC_MAX_WAIT", sync_data.get("max_sync_wait_seconds", 120))
        ),
    }

    return ServerConfig(
        monday=MondayConfig(**monday_config),
        sync=SyncConfig(**sync_config),
        logging=LoggingConfig(**logging_config),
        environment=os.getenv(
            "MCP_MONDAY_ENVIRONMENT", config_data.get("environment", "production")
        ),
    )
