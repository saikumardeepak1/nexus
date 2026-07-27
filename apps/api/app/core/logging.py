"""Structured JSON logging and request/job correlation ids.

Same dogfooded design as the Helios project (see docs/TDD.md section 8): a
log record factory injects the current correlation id onto every
``LogRecord`` before it reaches any handler, so the JSON formatter only has
to read ``record.correlation_id`` off the record rather than the id being
threaded through every function call as an explicit parameter.

A handler-level ``logging.Filter`` was considered and rejected. A filter
only runs for handlers actually attached to the record's own logger, or an
ancestor logger it propagates to; it never sees a record at all once some
other code path attaches its own handler to a submodule logger. RQ installs
its own handler on its own loggers, and pytest's ``caplog`` fixture does the
same for tests, so either one would silently miss a filter installed only on
the root logger's handler. A record factory, by contrast, runs for every
``LogRecord`` ever constructed, in any logger, before any handler is chosen,
so nothing downstream can bypass it.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# Holds the current request's (API) or job's (worker) correlation id. Unset
# (``None``) outside of a request or job, e.g. at import time or in a
# background task that was never given one.
correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

# Attributes a stock logging.LogRecord already carries. Anything a caller
# passes via ``extra={...}`` shows up as an additional attribute alongside
# these, so diffing against this set is how the formatter finds "extra"
# fields worth including in the JSON payload without hard-coding their names.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}

_factory_installed = False


def _install_correlation_id_record_factory() -> None:
    """Wrap whatever record factory is currently installed so every new
    ``LogRecord`` picks up a ``correlation_id`` attribute from the current
    contextvar (see module docstring for why this runs at the factory level
    rather than as a handler filter).

    Idempotent: repeated calls (``configure_logging`` may run more than once,
    e.g. once at process startup and again inside a test) leave the already
    installed factory in place instead of wrapping it again.
    """
    global _factory_installed
    if _factory_installed:
        return

    existing_factory: Callable[..., logging.LogRecord] = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = existing_factory(*args, **kwargs)
        record.correlation_id = correlation_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class JSONFormatter(logging.Formatter):
    """Renders one JSON object per log record: ``timestamp``, ``level``,
    ``logger``, ``message``, plus ``correlation_id`` when one is set and any
    extra fields a caller passed via ``extra={...}``.

    Reads ``record.correlation_id`` rather than the contextvar directly, so
    formatting stays a pure function of the record it's given, exactly what
    the correlation-id record factory installed by ``configure_logging``
    exists to guarantee is already present.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key != "correlation_id":
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Replace the root logger's handlers with a single JSON-line handler on
    stdout, and install the correlation-id record factory.

    Both the API process (``app/main.py``) and the worker process
    (``app/workers/worker.py``) call this once at startup so every log line
    either process emits, including from submodule loggers like RQ's own,
    is a single JSON object.

    Safe to call more than once: clears any handlers a previous call
    installed rather than stacking duplicate handlers, and leaves the
    correlation-id record factory installed exactly once (see
    ``_install_correlation_id_record_factory``).
    """
    _install_correlation_id_record_factory()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
