"""ASGI middleware for request-scoped correlation ids.

See app/core/logging.py for the ContextVar this sets and the log record
factory that reads it, and docs/TDD.md section 8 for the overall design.
"""

from __future__ import annotations

import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import correlation_id_var

_REQUEST_ID_HEADER = "x-request-id"
_RESPONSE_HEADER_NAME = "X-Request-ID"


class CorrelationIdMiddleware:
    """Sets ``correlation_id_var`` for the lifetime of one HTTP request.

    Reuses the inbound ``X-Request-ID`` header if the caller sent one,
    otherwise generates a fresh ``req-<hex>`` id, so a request that already
    carries an id from an upstream proxy or another service keeps that same
    id through this one. Echoes the id back on the response's
    ``X-Request-ID`` header, and resets the contextvar to whatever it was
    before once the request finishes, so nothing from this request ever
    leaks into whatever request or task this worker handles next.

    Implemented as a plain ASGI callable rather than Starlette's
    ``BaseHTTPMiddleware``. ``BaseHTTPMiddleware`` runs the downstream call
    in a separate task, and a contextvar set in the outer task is not
    reliably guaranteed to still be visible to route handlers and services
    running in that inner task, which would defeat the entire point of this
    middleware. A raw ASGI callable runs header setup, routing, the handler,
    and the response send all as one task, so the contextvar is visible
    everywhere the request goes, including nested service calls several
    layers deep.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = headers.get(_REQUEST_ID_HEADER) or f"req-{uuid.uuid4().hex}"
        token = correlation_id_var.set(correlation_id)

        async def send_with_correlation_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.append(_RESPONSE_HEADER_NAME, correlation_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation_id)
        finally:
            correlation_id_var.reset(token)
