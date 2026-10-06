"""Structured logging setup.

Every process logs to **stderr**: for stdio MCP servers, stdout is reserved for
protocol traffic and any write there corrupts the session.
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog (JSON) and route stdlib logging to stderr."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stderr,
        level=numeric_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
