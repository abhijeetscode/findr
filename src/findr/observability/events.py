"""Named log events and timings (specs/logging-telemetry.md §4.5, §6).

Telemetry is fields on log events — durations, counts, outcomes — not a
separate metrics API. Every event has a stable dotted name in `event`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def log_event(
    logger: logging.Logger,
    event: str,
    msg: str | None = None,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Logs `event` with `fields` as structured data. Fields carry ids,
    counts and enums only: never content, secrets or client names (§8)."""
    logger.log(level, msg or event, exc_info=exc_info, extra={"event": event, **fields})


def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


@contextmanager
def timed(
    logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any
) -> Iterator[dict[str, Any]]:
    """Logs `event` on exit with `duration_ms`. The yielded dict collects
    more fields along the way. On an exception the event is still logged,
    at WARNING with outcome="error", and the exception propagates."""
    collected = dict(fields)
    start = time.perf_counter()
    try:
        yield collected
    except BaseException as exc:
        collected.update(outcome="error", error_type=type(exc).__name__)
        log_event(
            logger, event, level=logging.WARNING, duration_ms=elapsed_ms(start), **collected
        )
        raise
    log_event(logger, event, level=level, duration_ms=elapsed_ms(start), **collected)
