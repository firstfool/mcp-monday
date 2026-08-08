"""Monday.com MCP Server — Production-grade board and item management."""

__version__ = "0.1.0"
__author__ = "IBM Consulting Advantage"
__description__ = "MCP server for Monday.com board and item management"

from .config import load_config, MondayConfig, ServerConfig
from .logging_config import setup_logging, get_logger

__all__ = [
    "load_config",
    "MondayConfig",
    "ServerConfig",
    "setup_logging",
    "get_logger",
]

