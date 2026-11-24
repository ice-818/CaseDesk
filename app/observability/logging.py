"""Structured JSON logging setup."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextvars import ContextVar

try:
    from pythonjsonlogger.jsonlogger import JsonFormatter
except ImportError:  # python-json-logger >= 3
    from pythonjsonlogger.json import JsonFormatter

from app.config.settings import Settings
from app.core.security import redact_mapping, redact_text, redact_value

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        record.request_id = request_id_var.get()
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            values = list(record.args)
            values[2] = str(values[2]).split("?", 1)[0]
            record.args = tuple(values)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_value(item) for item in record.args)
        elif isinstance(record.args, Mapping):
            record.args = redact_mapping(dict(record.args))
        if isinstance(record.msg, str):
            try:
                # Interpolate sanitized arguments before masking assignments.
                # Masking a %s template first could make logging dump args.
                record.msg = redact_text(record.getMessage())
            except (TypeError, ValueError, KeyError):
                record.msg = "invalid log format"
        else:
            record.msg = redact_value(record.msg)
        record.args = ()
        protected = {key: value for key, value in record.__dict__.items()
                     if key not in {"msg", "args", "exc_info", "exc_text"}}
        record.__dict__.update(redact_mapping(protected))
        if record.exc_info:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


class RedactingFormatter(JsonFormatter):
    def formatException(self, exc_info):
        return redact_text(super().formatException(exc_info))

    def process_log_record(self, log_record):
        # Redact structured values BEFORE JSON serialization. Rewriting the
        # encoded JSON could introduce unescaped quotation marks.
        return redact_mapping(super().process_log_record(log_record))


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s %(request_id)s"
    )
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True
