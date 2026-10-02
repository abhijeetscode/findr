"""Fields every log line carries without call sites passing them: request id,
user, workspace, job ids (specs/logging-telemetry.md §5).

One ContextVar holds a dict. `bind` starts a new scope (a copy of the
current dict plus the new fields) and restores the old one on exit.
`add_fields` instead updates the current scope's dict in place, which is
what lets a FastAPI dependency add `user_id` for the rest of its request:
sync dependencies run in a threadpool on a *copy* of the context, so a
`bind` there would be invisible to the endpoint and the access log, but
the copy still points at the same dict.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_fields: ContextVar[dict[str, Any] | None] = ContextVar("findr_log_fields", default=None)


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    token = _fields.set({**(_fields.get() or {}), **fields})
    try:
        yield
    finally:
        _fields.reset(token)


def add_fields(**fields: Any) -> None:
    """Adds fields to the enclosing `bind` scope. Outside any scope it does
    nothing, so it can't leak fields into unrelated work."""
    current = _fields.get()
    if current is not None:
        current.update(fields)


def current_fields() -> dict[str, Any]:
    return dict(_fields.get() or {})


def new_id(prefix: str | None = None) -> str:
    """16 hex chars for a request; `<prefix>-<8 hex>` for scheduler ticks,
    scripts and jobs that start without one."""
    if prefix is None:
        return secrets.token_hex(8)
    return f"{prefix}-{secrets.token_hex(4)}"
