"""Sanitized failures returned before the MCP response starts."""

import base64
import json

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class AuthFailure(Exception):
    def __init__(self, status: int, code: str, description: str, claims: str | None = None):
        super().__init__(description)
        self.status = status
        self.code = code
        self.description = description
        self.claims = claims


class AuthFailureMiddleware:
    def __init__(self, app: ASGIApp, metadata_url: str):
        self.app = app
        self.metadata_url = metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except AuthFailure as exc:
            if scope["type"] != "http":
                raise
            headers = {"Cache-Control": "no-store"}
            if exc.status in (401, 403):
                # All values except metadata_url are static or JSON-quoted/base64-encoded.
                challenge = (
                    f"Bearer error={json.dumps(exc.code)}, "
                    f"resource_metadata={json.dumps(self.metadata_url)}"
                )
                if exc.claims:
                    encoded = base64.b64encode(exc.claims.encode()).decode("ascii")
                    challenge += f', claims="{encoded}"'
                headers["WWW-Authenticate"] = challenge
            if exc.status == 503:
                headers["Retry-After"] = "10"
            await JSONResponse(
                {"error": exc.code, "error_description": exc.description},
                status_code=exc.status,
                headers=headers,
            )(scope, receive, send)
