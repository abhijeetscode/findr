"""Request id + one access-log line per request (specs/logging-telemetry.md §4.4).

Pure ASGI rather than BaseHTTPMiddleware, so streamed request and response
bodies pass through untouched.
"""

from __future__ import annotations

import logging
import re
import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from findr.observability import bind, log_event, new_id
from findr.observability.events import elapsed_ms

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
# Expected client-side outcomes (logged out, not found): not warnings.
_QUIET_CLIENT_ERRORS = {401, 404}


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


def _route_template(scope: Scope) -> str:
    """The matched route's template (`/workspaces/{workspace_id}/search`),
    never the raw path or query string, so no ids or search text leak in."""
    route = scope.get("route")
    return getattr(route, "path", None) or "<unmatched>"


def _level_for(status: int) -> int:
    if status >= 500:
        return logging.ERROR
    if status >= 400 and status not in _QUIET_CLIENT_ERRORS:
        return logging.WARNING
    return logging.INFO


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or new_id()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        start = time.perf_counter()
        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                log_event(
                    logger,
                    "http.request.failed",
                    level=logging.ERROR,
                    exc_info=True,
                    method=scope["method"],
                    route=_route_template(scope),
                    duration_ms=elapsed_ms(start),
                )
                raise
            log_event(
                logger,
                "http.request",
                f"{scope['method']} {_route_template(scope)} {status_code}",
                level=_level_for(status_code),
                method=scope["method"],
                route=_route_template(scope),
                status=status_code,
                duration_ms=elapsed_ms(start),
            )
