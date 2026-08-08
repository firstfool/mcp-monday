"""Main entry point for Monday.com MCP Server (read-only, change-detection sync)."""

import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from chuk_mcp_runtime.entry import main as runtime_main

from .clients.monday_client import get_monday_client
from .config import load_config
from .logging_config import setup_logging, get_logger
from .sync.db import init_sync_db
from .sync.engine import init_sync_engine

# Import tools to register them with the MCP runtime via @mcp_tool() decorators
from . import tools  # noqa: F401

# Load environment variables from .env file (no-op if absent)
load_dotenv()

logger = get_logger(__name__)


def main() -> None:
    """
    Initialize and start the Monday.com MCP Server.

    Steps:
    1. Load configuration from config.yaml and environment variables
    2. Set up structured logging
    3. Initialise the SQLite sync database
    4. Initialise the sync engine
    5. Configure transport mode (stdio or SSE) from config.yaml
    6. Start the chuk-mcp-runtime (auto-discovers all @mcp_tool() functions)
    """
    try:
        # Load and validate configuration
        config = load_config()

        # Set up logging as early as possible
        setup_logging(level=config.logging.level, format_type=config.logging.format)

        logger.info(
            "Starting Monday.com MCP Server",
            extra={
                "extra_fields": {
                    "environment": config.environment,
                    "log_level": config.logging.level,
                    "sync_db": config.sync.db_path,
                    "max_age_hours": config.sync.max_age_hours,
                }
            },
        )

        # Initialise the sync database
        sync_db = init_sync_db(db_path=config.sync.db_path)
        logger.info(
            "Sync database initialised",
            extra={"extra_fields": {"db_path": config.sync.db_path}},
        )

        # Initialise the sync engine (lazy — does not call Monday.com yet)
        init_sync_engine(
            client=get_monday_client(),
            db=sync_db,
            max_age_seconds=config.sync.max_age_hours * 3600,
            items_page_size=config.sync.items_page_size,
            max_sync_wait_seconds=config.sync.max_sync_wait_seconds,
        )
        logger.info("Sync engine initialised")

        # Set artifact storage to a writable location for non-root container users
        os.environ["CHUK_ARTIFACTS_DIR"] = os.getenv(
            "CHUK_ARTIFACTS_DIR", "/tmp/chuk_mcp_artifacts"
        )

        # Determine transport from config.yaml
        config_path = os.getenv("CONFIG_PATH", "config.yaml")
        os.environ["CHUK_CONFIG_PATH"] = config_path

        if Path(config_path).exists():
            with open(config_path) as f:
                config_data = yaml.safe_load(f) or {}

            transport_type = config_data.get("server", {}).get("type", "stdio")

            if transport_type == "sse":
                sse_config = config_data.get("sse", {})
                os.environ["CHUK_TRANSPORT"] = "sse"
                os.environ["CHUK_SSE_HOST"] = sse_config.get("host", "0.0.0.0")
                os.environ["CHUK_SSE_PORT"] = str(sse_config.get("port", 8080))
                logger.info(
                    "Configured SSE transport",
                    extra={
                        "extra_fields": {
                            "transport": "sse",
                            "host": os.environ["CHUK_SSE_HOST"],
                            "port": os.environ["CHUK_SSE_PORT"],
                        }
                    },
                )
            else:
                logger.info(
                    "Using stdio transport",
                    extra={"extra_fields": {"transport": "stdio"}},
                )
        else:
            logger.warning(
                f"Config file not found: {config_path}, defaulting to stdio transport"
            )

        # Start the MCP runtime — auto-discovers all @mcp_tool() decorated functions
        runtime_main()

    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Failed to start server: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
