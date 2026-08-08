"""Tests for configuration loading."""

import os
import pytest

from mcp_monday_server.config import load_config, MondayConfig, LoggingConfig


class TestLoadConfig:
    """Tests for load_config()."""

    def test_load_config_from_env(self, monkeypatch):
        """Config loads successfully when API key is set via environment."""
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "test-api-key-123")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        config = load_config()

        assert config.monday.get_api_key() == "test-api-key-123"
        assert config.monday.timeout == 30
        assert config.monday.max_retries == 3
        assert config.environment == "production"

    def test_load_config_missing_api_key_raises(self, monkeypatch):
        """load_config raises ValueError when API key is absent."""
        monkeypatch.delenv("MCP_MONDAY_API_KEY", raising=False)
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        with pytest.raises(ValueError, match="MCP_MONDAY_API_KEY is required"):
            load_config()

    def test_env_vars_override_defaults(self, monkeypatch):
        """Environment variables override built-in defaults."""
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "key-abc")
        monkeypatch.setenv("MCP_MONDAY_TIMEOUT", "60")
        monkeypatch.setenv("MCP_MONDAY_MAX_RETRIES", "5")
        monkeypatch.setenv("MCP_MONDAY_WORKSPACE_URL", "https://acme.monday.com")
        monkeypatch.setenv("MCP_MONDAY_ENVIRONMENT", "staging")
        monkeypatch.setenv("MCP_MONDAY_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        config = load_config()

        assert config.monday.timeout == 60
        assert config.monday.max_retries == 5
        assert config.monday.workspace_url == "https://acme.monday.com"
        assert config.environment == "staging"
        assert config.logging.level == "DEBUG"

    def test_api_key_redacted_in_repr(self, monkeypatch):
        """API key must not appear in string representations."""
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "super-secret-key")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        config = load_config()
        repr_str = repr(config.monday)

        assert "super-secret-key" not in repr_str

    def test_workspace_url_normalised(self, monkeypatch):
        """Trailing slash is stripped from workspace_url."""
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "key")
        monkeypatch.setenv("MCP_MONDAY_WORKSPACE_URL", "https://acme.monday.com/")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        config = load_config()

        assert config.monday.workspace_url == "https://acme.monday.com"

    def test_invalid_log_level_raises(self, monkeypatch):
        """Invalid log level raises ValueError."""
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "key")
        monkeypatch.setenv("MCP_MONDAY_LOG_LEVEL", "VERBOSE")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        with pytest.raises(ValueError):
            load_config()


class TestMondayConfig:
    """Tests for MondayConfig model."""

    def test_get_api_key_returns_plain_string(self):
        """get_api_key() unwraps the SecretStr."""
        cfg = MondayConfig(api_key="my-key")
        assert cfg.get_api_key() == "my-key"

    def test_invalid_workspace_url_raises(self):
        """workspace_url without http/https prefix is rejected."""
        with pytest.raises(ValueError):
            MondayConfig(api_key="key", workspace_url="acme.monday.com")
