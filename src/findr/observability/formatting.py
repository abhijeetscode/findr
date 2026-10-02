"""The JSON-lines format and the filters every record passes through
(specs/logging-telemetry.md §3, §5, §8)."""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import UTC, datetime
from typing import Any

from findr.observability.context import current_fields

# Attributes every LogRecord has; anything else on a record came from `extra`.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}
_HEAD_KEYS = ("ts", "level", "logger", "event", "msg", "service")
# Extras some libraries add that only make sense on a terminal.
_DROPPED_KEYS = frozenset({"color_message"})

# Safety net behind "only ids, counts and enums" (§8): `extra` keys that
# look like they hold a secret are blanked whatever their value.
_SENSITIVE_KEY = re.compile(r"password|secret|token|authorization|cookie|^code$|^state$", re.I)
REDACTED = "[redacted]"
# Second safety net (§8, Q2): Gmail addresses never reach the logs, even
# inside a message or traceback that quotes an upstream error body.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MASKED_EMAIL = "[email]"


def _mask_emails(text: str) -> str:
    return _EMAIL.sub(MASKED_EMAIL, text)


class ContextFilter(logging.Filter):
    """Copies the bound context fields (request id, user, workspace, job ids)
    onto each record. Fields passed explicitly via `extra` win."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_fields().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key not in _STANDARD_ATTRS and _SENSITIVE_KEY.search(key):
                setattr(record, key, REDACTED)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Tracebacks go inside `stack`, so a record
    never spans lines."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "msg": _mask_emails(record.getMessage()),
            "service": self._service,
        }
        if data["event"] is None:
            del data["event"]
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in _HEAD_KEYS and key not in _DROPPED_KEYS:
                data[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc, tb = record.exc_info
            data["exc_type"] = exc_type.__name__
            data["exc_message"] = _mask_emails(str(exc))
            data["stack"] = _mask_emails("".join(traceback.format_exception(exc_type, exc, tb)))
        elif record.stack_info:
            data["stack"] = _mask_emails(record.stack_info)
        return json.dumps(data, default=str, ensure_ascii=False)
