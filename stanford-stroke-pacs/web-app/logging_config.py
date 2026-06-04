"""JSON structured logging for the web-app service.

Configures the root logger with a `python-json-logger` formatter so every
log record is a single JSON line with a consistent field set. Two
contextvars — `request_id_ctx` and `user_ctx` — are injected into every
record so request-scoped context propagates into logs emitted deep in
the stack without plumbing it through every call.

Intended wiring:
  - `configure_logging()` is called once at app import time.
  - The request-ID middleware sets `request_id_ctx` (and `user_ctx`
    when available) at the start of each request and resets the tokens
    in a `finally` block.
  - Any logger created via `logging.getLogger(__name__)` automatically
    inherits the JSON formatter and the contextvar fields.

Rotation is handled by the systemd journal (see
`documentation/operations/observability.md`).
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar

from pythonjsonlogger import jsonlogger

# Request-scoped context populated by the request-ID middleware. Empty
# string sentinels (rather than None) keep the JSON shape stable across
# log records even for requests that don't set them yet (eviction loop,
# startup, etc.).
request_id_ctx: ContextVar[str] = ContextVar("request_id_ctx", default="")
user_ctx: ContextVar[str] = ContextVar("user_ctx", default="")


class ContextFilter(logging.Filter):
    """Attach `request_id` and `user` from contextvars onto each record.

    `extra=` on individual log calls can still override these (e.g. the
    cold-storage code attaches `study_uid`); this filter only fills in
    the defaults that every record is expected to carry.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = request_id_ctx.get() or ""
        if not getattr(record, "user", None):
            record.user = user_ctx.get() or ""
        return True


class _JsonFormatter(jsonlogger.JsonFormatter):
    """Force ISO-8601 UTC timestamps and a stable field order."""

    def add_fields(self, log_record, record, message_dict):  # type: ignore[override]
        super().add_fields(log_record, record, message_dict)
        # `timestamp` is set by the `%(asctime)s` reserved attribute but
        # python-json-logger emits it as `asctime`; rename for clarity.
        if "asctime" in log_record and "timestamp" not in log_record:
            log_record["timestamp"] = log_record.pop("asctime")
        # Guarantee the core fields always appear, even when the record
        # didn't set them.
        log_record.setdefault("level", record.levelname)
        log_record.setdefault("logger", record.name)
        log_record.setdefault("request_id", getattr(record, "request_id", "") or "")
        log_record.setdefault("user", getattr(record, "user", "") or "")


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger with the JSON formatter.

    Safe to call multiple times — reconfiguration replaces existing
    handlers rather than appending.
    """
    log_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    # ISO-8601 UTC timestamps. `%(asctime)s` honours `datefmt`, and
    # `logging.Formatter.converter = time.gmtime` makes it UTC.
    import time

    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    formatter = _JsonFormatter(
        fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        rename_fields={"levelname": "level", "name": "logger"},
    )
    formatter.converter = time.gmtime

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    # Remove any pre-existing handlers so we don't double-emit records
    # (uvicorn installs its own on import; alembic's `fileConfig()` also
    # attaches one during startup migrations).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(log_level)

    # Alembic calls `logging.config.fileConfig(...)` which defaults to
    # `disable_existing_loggers=True` — every pre-existing module logger
    # (including `app`, `cache_manager`, `uvicorn.error`) gets `disabled=True`
    # and stops emitting. Force them back on after reconfiguring.
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            lg.disabled = False

    # Make sure uvicorn's access/error loggers propagate through our
    # handler rather than their own plain-text ones.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True


__all__ = [
    "configure_logging",
    "request_id_ctx",
    "user_ctx",
]
