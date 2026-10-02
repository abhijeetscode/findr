"""Logging and telemetry with the standard library only — see
specs/logging-telemetry.md.

Use cases and adapters log through `logging.getLogger(__name__)` plus the
helpers here, which are plain standard library. Entry points (the API, the
worker, scripts) call `findr.observability.setup.configure_logging` once at
startup; it's imported from there, not re-exported, so the application
layer never pulls in config or handler setup.
"""

from findr.observability.context import add_fields, bind, current_fields, new_id
from findr.observability.events import log_event, timed

__all__ = [
    "add_fields",
    "bind",
    "current_fields",
    "log_event",
    "new_id",
    "timed",
]
