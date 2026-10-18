"""Structured logging setup: JSON to stderr with redaction of sensitive data.

Every process logs to **stderr**: for stdio MCP servers, stdout is reserved for
protocol traffic and any write there corrupts the session.

The redaction processor runs before JSON serialization and removes sensitive
keys (case-insensitively, including nested dicts), strips credentials from
URLs, and replaces unsupported object types with safe type markers. It builds
new containers — the original event dict is never mutated.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# Substring fragments matched case-insensitively against dict key names.
# Deliberately avoids over-matching ordinary diagnostic fields (tool, server,
# status, duration_ms, count, outcome, etc.).
_SENSITIVE_KEY_FRAGMENTS = frozenset({
    "authorization", "api_key", "api-key", "apikey", "token",
    "password", "secret", "credentials", "bearer", "cookie",
    "access_token", "refresh_token", "client_secret", "session_token",
    "private_key", "x-api-key", "passwd", "auth",
})

_URL_CREDENTIALS_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)")

_MAX_DEPTH = 6


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_url_credentials(text: str) -> str:
    """Replace password segments in URLs (e.g. DSNs) with [REDACTED]."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1[REDACTED]\2", text)


def _redact_value(value: Any, depth: int) -> Any:
    """Build a redacted copy of *value*; the original is never mutated."""
    if depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            key: _redact_sensitive_entry(str(key), val, depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_url_credentials(value)
    if isinstance(value, (bool, int, float, type(None))):
        return value
    return f"<{type(value).__name__}>"


def _redact_sensitive_entry(key: str, value: Any, depth: int) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    return _redact_value(value, depth)


def redact_sensitive_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor: returns a redacted copy of the event dict.

    Never mutates the input; never raises (exceptions from the redaction walk
    are caught and the original dict is returned — a redaction failure must
    not suppress the log entry)."""
    try:
        return _redact_value(event_dict, depth=0)
    except Exception:
        return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog (JSON to stderr) and route stdlib logging."""
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
            redact_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
