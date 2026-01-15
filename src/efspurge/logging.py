"""JSON logging module for Kubernetes compatibility."""

import json
import logging
import sys
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    """JSON log formatter for Kubernetes and CloudWatch compatibility."""

    def format(self, record: logging.LogRecord) -> str:
        """Format LogRecord into JSON string."""
        log_obj: Dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        # Add error information if present
        if record.exc_info:
            log_obj["error"] = self.formatException(record.exc_info)
            log_obj["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None

        # Add extra fields if any
        if hasattr(record, "extra_fields"):
            log_obj["extra_fields"] = record.extra_fields

        return json.dumps(log_obj)


def setup_logging(logger_name: str = "efspurge", level: str = "INFO") -> logging.Logger:
    """
    Configure JSON logging for Kubernetes compatibility.

    Args:
        logger_name: Name of the logger
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper()))

    # Avoid adding duplicate handlers
    if not logger.handlers:
        # Single handler to stdout (K8s captures both stdout and stderr)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger


def log_with_context(
    logger: logging.Logger, level: str, message: str, extra: Optional[Dict[str, Any]] = None
) -> None:
    """
    Log message with additional context fields.

    Args:
        logger: Logger instance
        level: Log level (info, warning, error, etc.)
        message: Log message
        extra: Additional context fields to include in JSON output
    """
    if extra is None:
        extra = {}

    log_method = getattr(logger, level.lower())
    log_method(message, extra={"extra_fields": extra})
