"""Logging setup.

JSON by default, because these logs are meant to be searched when reconciling a
disputed bill, and grepping human-formatted lines for a request id is miserable.
Set ``GATEWAY_LOG_JSON=false`` for readable local output.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import orjson

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything passed via `extra=` is merged in, so callers can attach a
        # request_id without a custom formatter per call site.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return orjson.dumps(payload, default=str).decode()


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JSONFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours instead so the
    # output format is uniform.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # SQLAlchemy is chatty at INFO when echo is on; keep it at WARNING unless the
    # operator explicitly wants query logs.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
