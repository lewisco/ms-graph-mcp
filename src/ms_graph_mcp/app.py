"""ASGI factory: uvicorn ms_graph_mcp.app:create_app --factory."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from ms_graph_mcp.auth import DelegatedAccessToken, EntraVerifier
from ms_graph_mcp.config import Settings
from ms_graph_mcp.errors import AuthFailureMiddleware
from ms_graph_mcp.graph import DEFAULT_FIELDS, GraphClient, GraphFailure, ProfileField
from ms_graph_mcp.obo import OboClient
from ms_graph_mcp.request_security import RequestSecurityMiddleware
from ms_graph_mcp.tls import create_ssl_context


def create_app(
    settings: Settings | None = None,
    *,
    verifier: EntraVerifier | None = None,
    obo: OboClient | None = None,
    graph_http: httpx.AsyncClient | None = None,
) -> Starlette:
    settings = settings or Settings()
    obo = obo or OboClient(settings)
    verifier = verifier or EntraVerifier(settings, obo)
    http = graph_http or httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        verify=create_ssl_context(settings),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    graph = GraphClient(http, obo)

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await http.aclose()
            await verifier.aclose()
            await obo.aclose()

    mcp = MCPServer(
        "ms-graph-mcp",
        version="0.1.0",
        instructions=(
            "This initial server supports only reading the signed-in user's Graph profile. "
            "Use graph_capabilities for implemented coverage. Other Microsoft 365 operations "
            "and file/document tools are not implemented yet."
        ),
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            required_scopes=[settings.oauth_scope],
            validate_token_resource=True,
        ),
        lifespan=lifespan,
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @mcp.tool(annotations=readonly)
    def graph_capabilities() -> dict:
        """List implemented operations; does not claim access to unimplemented services."""
        return {"implemented": [{"method": "GET", "path": "/me"}], "stage": "authentication"}

    @mcp.tool(annotations=readonly)
    def graph_describe(path: Literal["/me"] = "/me") -> dict:
        """Describe the first supported Graph operation and its delegated permission."""
        return {
            "method": "GET",
            "path": path,
            "delegated_permission": "User.Read",
            "default_fields": DEFAULT_FIELDS,
        }

    @mcp.tool(annotations=readonly)
    async def graph_read(
        path: Literal["/me"] = "/me",
        method: Literal["GET"] = "GET",
        select: list[ProfileField] | None = None,
    ) -> CallToolResult:
        """Read selected profile fields from Graph /me as the connected user."""
        identity = get_access_token()
        if not isinstance(identity, DelegatedAccessToken):
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="Microsoft authorization required.")],
            )
        fields = list(dict.fromkeys(select or DEFAULT_FIELDS))
        try:
            result = await graph.me(identity, fields)
        except GraphFailure as exc:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": exc.status,
                                "error": {"code": exc.code, "message": str(exc)},
                                "request_id": exc.request_id,
                                "retry_after_seconds": exc.retry_after_seconds,
                            },
                            separators=(",", ":"),
                        ),
                    )
                ],
            )
        # One compact JSON text block works for clients that discard structuredContent and
        # avoids injecting the same profile twice when a client forwards both representations.
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, separators=(",", ":")))],
        )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readiness(request: Request) -> JSONResponse:
        # Process readiness only. Never suggest this proves user consent or Graph connectivity.
        return JSONResponse({"status": "ready", "graph_connectivity": "not_probed"})

    # Many clients try the root resource-metadata URL in addition to the RFC path-specific URL.
    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def metadata(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": settings.resource_url,
                "authorization_servers": [settings.issuer],
                "scopes_supported": [settings.oauth_scope],
                "bearer_methods_supported": ["header"],
            }
        )

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=65536,
        transport_security=transport_security,
    )
    app.add_middleware(AuthFailureMiddleware, metadata_url=settings.metadata_url)
    app.add_middleware(
        RequestSecurityMiddleware,
        public_paths={
            "/healthz",
            "/readyz",
            "/.well-known/oauth-protected-resource",
            settings.metadata_path,
        },
        security=transport_security,
    )
    return app
