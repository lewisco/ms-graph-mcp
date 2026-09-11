"""ASGI factory: uvicorn ms_graph_mcp.app:create_app --factory."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, get_args

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
from ms_graph_mcp.catalog import OPERATIONS, describe
from ms_graph_mcp.config import Settings
from ms_graph_mcp.errors import AuthFailureMiddleware
from ms_graph_mcp.gateway import Gateway
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
    gateway = Gateway(graph, settings)

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await gateway.aclose()
            await http.aclose()
            await verifier.aclose()
            await obo.aclose()

    mcp = MCPServer(
        "ms-graph-mcp",
        version="0.3.0",
        instructions=(
            "Delegated Microsoft 365 operations: discover services with graph_capabilities and "
            "routes with graph_describe. Use graph_read for reads, graph_write for mutations, "
            "graph_continue for page handles, and transfer tools for native drive files. "
            "Access depends on user rights and Graph consent. Do not repeat uncertain writes. "
            "Tenant administration, beta, batch, and server-staged artifacts are not exposed."
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

    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

    def tool_result(value, error=False):
        return CallToolResult(
            isError=error,
            content=[TextContent(type="text", text=json.dumps(value, separators=(",", ":")))],
        )

    async def execute(operation):
        identity = get_access_token()
        if not isinstance(identity, DelegatedAccessToken):
            return tool_result(
                {
                    "error": {
                        "code": "authorization_required",
                        "message": "Microsoft authorization required.",
                    }
                },
                True,
            )
        try:
            return tool_result(await operation(identity))
        except GraphFailure as exc:
            return tool_result(
                {
                    "status": exc.status,
                    "error": {"code": exc.code, "message": str(exc)},
                    "request_id": exc.request_id,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
                True,
            )
        except (ValueError, KeyError, TypeError) as exc:
            return tool_result(
                {
                    "error": {
                        "code": "invalid_operation",
                        "message": str(exc)
                        if isinstance(exc, ValueError)
                        else "Invalid Graph response or operation input.",
                    }
                },
                True,
            )

    @mcp.tool(annotations=readonly)
    def graph_capabilities() -> dict:
        """Discover implemented service families and limits; access requires delegated consent."""
        return {
            "stage": "microsoft365",
            "api_version": "v1.0",
            "services": [
                {
                    "name": service,
                    "operations": sum(o.service == service for o in OPERATIONS),
                    "availability": "supported_unverified",
                }
                for service in sorted({o.service for o in OPERATIONS})
            ],
            "operation_count": len(OPERATIONS),
            "discovery": "graph_describe(service=...) returns paged routes",
            "transfers": {
                "drive_download": True,
                "drive_upload_session": True,
                "max_bytes": 250000000,
                "server_staged_artifacts": False,
                "native_handle_ttl_seconds": 3600,
            },
            "limits": {
                "inline_response_bytes": 2000000,
                "request_body_bytes": 60000,
                "onenote_multipart": False,
                "presence_write": "Signed-in user's object ID only; no automatic renewal.",
                "beta": False,
                "batch": False,
                "tenant_administration": False,
                "word_powerpoint_editing": "Use terminal tools after download.",
            },
        }

    @mcp.tool(annotations=readonly)
    def graph_describe(
        service: str | None = None,
        query: str | None = None,
        path: str | None = None,
        method: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict:
        """Search supported Graph routes, method classification and permission guidance.
        Page with offset.
        """
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("offset must be nonnegative and limit must be between 1 and 50")
        return describe(service, query, path, method, offset, limit)

    @mcp.tool(annotations=readonly)
    async def graph_read(
        path: str = "/me",
        method: Literal["GET", "POST"] = "GET",
        select: list[str] | None = None,
        query: dict[str, str] | None = None,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> CallToolResult:
        """Read Graph resources. POST is allowed only for cataloged
        searches/availability reads. Use Graph query keys such as
        $select/$filter/$top; follow returned handles with graph_continue.
        """

        async def operation(identity):
            if path == "/me" and method == "GET" and not query and not body and not headers:
                fields = list(dict.fromkeys(select or DEFAULT_FIELDS))
                if not all(field in get_args(ProfileField) for field in fields):
                    raise ValueError(
                        "Unsupported profile field. Use query.$select for broader Graph queries."
                    )
                return await graph.me(identity, fields)
            options = dict(query or {})
            if select:
                if "$select" in options:
                    raise ValueError("Specify select or query.$select, not both.")
                options["$select"] = ",".join(select)
            return await gateway.request(identity, method, path, options, body, headers)

        return await execute(operation)

    @mcp.tool(annotations=write)
    async def graph_write(
        path: str,
        method: Literal["POST", "PATCH", "PUT", "DELETE"],
        body: dict | list[dict] | None = None,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        html: str | None = None,
    ) -> CallToolResult:
        """Execute a cataloged Microsoft 365 mutation, including sends/deletes/sharing.
        Supply Graph JSON (an array for OneNote content patches), or html for
        OneNote page creation. Preserve eTags through If-Match. Never blindly
        retry an uncertain write. Presence writes target your own object ID.
        """
        return await execute(
            lambda identity: gateway.request(
                identity, method, path, query, body, headers, read_only=False, html=html
            )
        )

    @mcp.tool(annotations=readonly)
    async def graph_continue(continuation: str) -> CallToolResult:
        """Read the next page from an owner-bound handle; never replays the original mutation."""
        return await execute(lambda identity: gateway.continue_page(identity, continuation))

    @mcp.tool(annotations=write)
    async def graph_prepare_transfer(
        direction: Literal["download", "upload"],
        path: str,
        filename: str | None = None,
        size: int | None = None,
        etag: str | None = None,
    ) -> CallToolResult:
        """Prepare a native drive transfer up to 250 MB. Download uses an item path.
        Upload with filename uses a parent path and creates a copy; omitting filename
        replaces an item and requires etag. Keep returned URLs private; the terminal
        transfers bytes without Graph authorization.
        """
        return await execute(
            lambda identity: gateway.prepare(identity, direction, path, filename, size, etag)
        )

    @mcp.tool(annotations=readonly)
    async def graph_transfer_status(
        transfer: str, completed_item_path: str | None = None
    ) -> CallToolResult:
        """Inspect a transfer; provide the completed drive item path to check upload
        size and destination drive. This does not prove byte-for-byte integrity.
        """
        return await execute(
            lambda identity: gateway.transfer_status(identity, transfer, completed_item_path)
        )

    @mcp.tool(annotations=write)
    async def graph_transfer_manage(
        transfer: str, action: Literal["cancel"] = "cancel"
    ) -> CallToolResult:
        """Cancel a native upload session at Microsoft. Download URL
        revocation/retention extension is not supported.
        """
        return await execute(lambda identity: gateway.transfer_manage(identity, transfer, action))

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
