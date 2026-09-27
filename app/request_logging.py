"""Structured JSON request logging.

``RequestLoggingMiddleware`` is a pure ASGI middleware that writes exactly one
JSON line per HTTP request to the ``inventory_api.request`` logger:

    {"request_id": ..., "method": ..., "path": ..., "status": ...,
     "latency_ms": ..., "tenant_id": ...}

Each request gets a request id that is also returned in the ``X-Request-Id``
response header. A well-formed incoming ``X-Request-Id`` is honoured so ids
can be correlated across services; anything else is replaced with a freshly
generated one.

Request and response bodies are never read or logged: the middleware does not
touch the ASGI ``receive`` channel at all, and only inspects the status line
of the response. The query string is not logged either (``path`` only), since
it can carry user-supplied values.
"""

import json
import logging
import re
import time
import uuid

logger = logging.getLogger("inventory_api.request")

REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")
_TENANT_HEADER_BYTES = b"x-tenant-id"

# Incoming ids are echoed back in a response header and written to the log,
# so only accept a conservative, bounded charset.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _request_id(scope) -> str:
    incoming = _header(scope, _REQUEST_ID_HEADER_BYTES)
    if incoming and _VALID_REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != _REQUEST_ID_HEADER_BYTES
                ]
                headers.append((_REQUEST_ID_HEADER_BYTES, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            logger.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "latency_ms": round(latency_ms, 3),
                        "tenant_id": _header(scope, _TENANT_HEADER_BYTES),
                    },
                    separators=(",", ":"),
                )
            )
