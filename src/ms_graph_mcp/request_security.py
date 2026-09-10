"""Apply cheap MCP checks before authentication and ignore public-route credentials."""

from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestSecurityMiddleware:
    def __init__(self, app: ASGIApp, public_paths: set[str], security: TransportSecuritySettings):
        self.app = app
        self.public_paths = public_paths
        self.security = TransportSecurityMiddleware(security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope["path"] in self.public_paths:
                # The SDK otherwise runs optional bearer authentication even on public routes.
                # Copy the scope; do not alter credentials on any protected route.
                scope = {
                    **scope,
                    "headers": [
                        (name, value)
                        for name, value in scope["headers"]
                        if name.lower() != b"authorization"
                    ],
                }
            elif scope["path"] in {"/mcp", "/mcp/"}:
                rejection = await self.security.validate_request(
                    Request(scope), is_post=scope["method"] == "POST"
                )
                if rejection is not None:
                    await rejection(scope, receive, send)
                    return
        await self.app(scope, receive, send)
