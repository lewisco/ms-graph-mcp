import base64
import json
import logging
from unittest.mock import Mock

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from ms_graph_mcp.app import create_app
from ms_graph_mcp.audit import LOGGER_NAME
from ms_graph_mcp.auth import EntraVerifier
from ms_graph_mcp.obo import OboClient

HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def server(settings, jwks, request):
    settings = settings.model_copy(update=getattr(request, "param", {}))
    requests = []
    upstream = [
        httpx.Response(
            200,
            json={
                "id": "signed-in-user",
                "displayName": "Test User",
                "mail": "test@example.com",
                "userPrincipalName": "test@example.com",
                "unrequested": "do-not-return",
            },
        )
    ]

    def graph(request):
        requests.append(request)
        return upstream[0]

    exchange = Mock(
        side_effect=lambda assertion: {
            "access_token": "graph-"
            + jwt.decode(assertion, options={"verify_signature": False})["oid"],
            "expires_in": 3600,
        }
    )
    obo = OboClient(settings, exchange=exchange)
    http = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    app = create_app(
        settings, obo=obo, verifier=EntraVerifier(settings, obo, jwks=jwks), graph_http=http
    )
    with TestClient(app) as client:
        yield client, requests, upstream, exchange, obo, http
    assert http.is_closed
    assert not obo._cache


@pytest.fixture
def audit_records():
    logger = logging.getLogger(LOGGER_NAME)
    saved = logger.handlers[:], logger.level, logger.propagate
    records = []
    capture = logging.Handler()
    capture.emit = lambda record: records.append(json.loads(record.getMessage()))
    logger.addHandler(capture)
    yield records
    logger.handlers[:], logger.propagate = saved[0], saved[2]
    logger.setLevel(saved[1])


def rpc(client, token, method, params=None, **headers):
    return client.post(
        "/mcp",
        headers={
            **HEADERS,
            "Authorization": f"Bearer {token}",
            **headers,
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def call_profile(client, token, arguments=None):
    return rpc(client, token, "tools/call", {"name": "graph_read", "arguments": arguments or {}})


def read_payload(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert len(result["content"]) == 1
    return result, json.loads(result["content"][0]["text"])


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("sharepoint_read", {"operation": "settings"}),
        ("sharepoint_write", {"operation": "apply_theme", "theme_name": "Brand"}),
    ],
)
def test_sharepoint_disabled_without_destination(server, token, name, arguments):
    client, requests, *_ = server
    result, payload = read_payload(
        rpc(
            client,
            token(),
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
    )
    assert result["isError"]
    assert "GRAPH_MCP_SHAREPOINT_SITE_URL" in payload["error"]["message"]
    assert not requests


@pytest.mark.parametrize("server", [{"max_request_body_bytes": 200000}], indirect=True)
def test_large_page_through_mcp_obeys_configured_limit(server, token):
    client, requests, upstream, *_ = server
    incoming = token()
    upstream[0] = httpx.Response(200, json={"id": "page"})
    # Escaped JSON on the inbound wire is much bigger than its decoded UTF-8 payload.
    body = {"@odata.type": "#microsoft.graph.sitePage", "description": "é" * 45000}
    response = client.post(
        "/mcp",
        headers={
            **HEADERS,
            "Authorization": f"Bearer {incoming}",
            "Content-Type": "application/json",
        },
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "graph_write",
                    "arguments": {
                        "method": "PATCH",
                        "path": "/sites/site/pages/page/microsoft.graph.sitePage",
                        "body": body,
                    },
                },
            }
        ),
    )
    result, _ = read_payload(response)
    assert not result.get("isError")
    assert json.loads(requests[-1].content) == body
    count = len(requests)
    body["description"] = "x" * 200000
    result, payload = read_payload(
        rpc(
            client,
            incoming,
            "tools/call",
            {
                "name": "graph_write",
                "arguments": {
                    "method": "PATCH",
                    "path": "/sites/site/pages/page/microsoft.graph.sitePage",
                    "body": body,
                },
            },
        )
    )
    assert result["isError"]
    assert "200000 bytes" in payload["error"]["message"]
    assert len(requests) == count
    _, caps = read_payload(rpc(client, incoming, "tools/call", {"name": "graph_capabilities"}))
    assert caps["limits"]["request_body_bytes"] == 200000
    assert caps["limits"]["mcp_request_bytes"] == 1265536


def test_mcp_outer_limit_rejects_oversize_before_operation(server, token, settings):
    client, requests, *_ = server
    response = client.post(
        "/mcp",
        headers={
            **HEADERS,
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
        content=b" " * (settings.max_mcp_request_bytes + 1),
    )
    assert response.status_code == 413
    assert not requests


@pytest.fixture
def site_server(settings, jwks, request):
    settings = settings.model_copy(
        update={
            "sharepoint_site_url": "https://tenant.sharepoint.com/sites/Hub",
            **getattr(request, "param", {}),
        }
    )
    requests = []
    responses = [httpx.Response(200, json={"Id": "site-guid", "Title": "Hub"})]

    def upstream(request):
        requests.append(request)
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"id": "user", "displayName": "Test User"})
        return responses[0]

    graph_exchange = Mock(return_value={"access_token": "graph-only", "expires_in": 3600})
    site_exchange = Mock(return_value={"access_token": "sharepoint-only", "expires_in": 3600})
    obo = OboClient(settings, exchange=graph_exchange)
    site_obo = OboClient(
        settings, resource_origin=settings.sharepoint_resource_origin, exchange=site_exchange
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        settings,
        obo=obo,
        verifier=EntraVerifier(settings, obo, jwks=jwks),
        graph_http=http,
        sharepoint_obo=site_obo,
    )
    with TestClient(app) as client:
        yield client, requests, responses, graph_exchange, site_exchange
    assert http.is_closed
    assert not obo._cache and not site_obo._cache


def test_sharepoint_tools_execute_with_separate_token(site_server, token):
    client, requests, responses, graph_exchange, site_exchange = site_server
    incoming = token()
    listing = rpc(client, incoming, "tools/list").json()["result"]["tools"]
    annotations = {tool["name"]: tool["annotations"] for tool in listing}
    assert annotations["sharepoint_read"]["readOnlyHint"] is True
    assert annotations["sharepoint_write"]["readOnlyHint"] is False
    site_exchange.assert_not_called()  # Optional resource auth is lazy.
    result, payload = read_payload(
        rpc(
            client,
            incoming,
            "tools/call",
            {
                "name": "sharepoint_read",
                "arguments": {"operation": "settings"},
            },
        )
    )
    assert not result.get("isError")
    assert payload["data"]["Title"] == "Hub"
    assert requests[-1].url.path == "/sites/Hub/_api/web"
    assert requests[-1].headers["authorization"] == "Bearer sharepoint-only"
    responses[0] = httpx.Response(
        201, json={"Id": 7, "Title": "Guide", "Url": "/sites/Hub/SitePages/Guide.aspx"}
    )
    result, payload = read_payload(
        rpc(
            client,
            incoming,
            "tools/call",
            {
                "name": "sharepoint_write",
                "arguments": {
                    "operation": "navigation_add",
                    "title": "Guide",
                    "url": "/sites/Hub/SitePages/Guide.aspx",
                    "location": "topnavigationbar",
                },
            },
        )
    )
    assert not result.get("isError") and payload["data"]["Id"] == 7
    assert requests[-1].url.path == "/sites/Hub/_api/web/navigation/topnavigationbar"
    assert json.loads(requests[-1].content)["Title"] == "Guide"
    read_payload(call_profile(client, incoming))
    assert requests[-1].headers["authorization"] == "Bearer graph-only"
    assert site_exchange.call_count == graph_exchange.call_count == 1


@pytest.mark.parametrize(
    "error,status,challenge",
    [
        ("consent_required", 401, False),
        ("interaction_required", 401, True),
        ("invalid_client", 503, False),
    ],
)
def test_sharepoint_consent_failure_does_not_disable_graph(
    site_server,
    token,
    error,
    status,
    challenge,
):
    client, requests, _, _, site_exchange = site_server
    site_exchange.return_value = {"error": error, "error_description": "secret-detail"}
    if challenge:
        site_exchange.return_value["claims"] = '{"access_token":{"acrs":{"value":"c1"}}}'
    incoming = token()
    result, payload = read_payload(
        rpc(
            client,
            incoming,
            "tools/call",
            {
                "name": "sharepoint_read",
                "arguments": {"operation": "settings"},
            },
        )
    )
    assert result["isError"] and payload["status"] == status
    assert payload["resource"] == "https://tenant.sharepoint.com"
    assert payload["claims_challenge_required"] == challenge
    assert "secret-detail" not in json.dumps(payload)
    assert not requests
    result, _ = read_payload(call_profile(client, incoming))
    assert not result.get("isError")
    assert requests[-1].url.host == "graph.microsoft.com"


@pytest.mark.parametrize("site_server", [{"audit_log": True}], indirect=True)
def test_audit_log_records_sharepoint_tools(audit_records, site_server, token):
    client, _, _, _, site_exchange = site_server
    call = {"name": "sharepoint_read", "arguments": {"operation": "settings"}}
    read_payload(rpc(client, token(), "tools/call", call))
    site_exchange.return_value = {"error": "consent_required", "error_description": "secret"}
    read_payload(rpc(client, token(oid="55555555-5555-4555-8555-555555555555"), "tools/call", call))
    for record in audit_records:
        assert record.pop("duration_ms") >= 0
    user = {"azp": "33333333-3333-4333-8333-333333333333"}
    assert audit_records == [
        {
            "event": "tool_call",
            "tool": "sharepoint_read",
            "outcome": "success",
            "oid": "44444444-4444-4444-8444-444444444444",
            **user,
        },
        {
            "event": "tool_call",
            "tool": "sharepoint_read",
            "outcome": "auth_error",
            "oid": "55555555-5555-4555-8555-555555555555",
            **user,
        },
    ]


def test_sharepoint_rejects_irrelevant_write_fields_before_call(site_server, token):
    client, requests, _, _, site_exchange = site_server
    result, _ = read_payload(
        rpc(
            client,
            token(),
            "tools/call",
            {
                "name": "sharepoint_write",
                "arguments": {
                    "operation": "navigation_delete",
                    "node_id": 7,
                    "theme_name": "Brand",
                },
            },
        )
    )
    assert result["isError"]
    assert not requests
    site_exchange.assert_not_called()


def test_discovery_and_missing_auth(server, settings):
    client, requests, _, exchange, _, _ = server
    for route in ("/healthz", "/readyz"):
        assert client.get(route).status_code == 200
    for route in ("/.well-known/oauth-protected-resource", settings.metadata_path):
        response = client.get(route)
        assert response.status_code == 200
        assert response.json()["resource"] == settings.resource_url
        assert response.json()["scopes_supported"] == [settings.oauth_scope]
    response = client.post("/mcp", headers=HEADERS, json={})
    assert response.status_code == 401
    assert settings.metadata_url in response.headers["www-authenticate"]
    exchange.assert_not_called()
    assert not requests


@pytest.mark.parametrize("version", ["2025-03-26", "2025-11-25"])
def test_initialize_discover_and_read(server, token, version):
    client, requests, _, exchange, _, http = server
    incoming = token()
    response = rpc(
        client,
        incoming,
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "compatibility-test", "version": "1.0"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["protocolVersion"] == version
    listing = rpc(client, incoming, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listing} == {
        "graph_read",
        "graph_describe",
        "graph_capabilities",
        "graph_write",
        "graph_continue",
        "graph_prepare_transfer",
        "graph_transfer_status",
        "graph_transfer_manage",
        "sharepoint_read",
        "sharepoint_write",
    }
    result, payload = read_payload(call_profile(client, incoming))
    assert not result.get("isError")
    assert payload["data"]["displayName"] == "Test User"
    assert "unrequested" not in payload["data"]
    assert not result.get("structuredContent")  # One model-visible copy in text.
    assert incoming not in json.dumps(result)
    assert not http.is_closed  # Resources must outlive stateless requests.
    _, selected = read_payload(call_profile(client, incoming, {"select": ["id", "id"]}))
    assert selected["data"] == {"id": "signed-in-user"}
    assert requests[-1].url.params["$select"] == "id"
    assert requests[0].url.host == "graph.microsoft.com"
    assert requests[0].headers["authorization"].startswith("Bearer graph-")
    assert incoming not in requests[0].headers["authorization"]
    assert exchange.call_count == 1


def test_two_users_never_share_graph_token(server, token):
    client, requests, _, exchange, _, _ = server
    first = token()
    second = token(oid="55555555-5555-4555-8555-555555555555")
    for incoming in (first, second, first):
        read_payload(call_profile(client, incoming))
    auths = [request.headers["authorization"] for request in requests]
    assert auths[0] == auths[2] != auths[1]
    assert exchange.call_count == 2


def test_gateway_oauth2_client_token_reaches_graph(server, token):
    client, requests, _, exchange, _, _ = server
    incoming = token(azp="66666666-6666-4666-8666-666666666666", scp="access_as_user")
    result, payload = read_payload(call_profile(client, incoming))
    assert not result.get("isError")
    assert payload["data"]["displayName"] == "Test User"
    assert requests[0].headers["authorization"].endswith("-44444444-4444-4444-8444-444444444444")
    exchange.assert_called_once_with(incoming)


def test_unlisted_oauth2_client_rejected_before_exchange(server, token, settings):
    client, requests, _, exchange, _, _ = server
    response = call_profile(client, token(azp="77777777-7777-4777-8777-777777777777"))
    assert response.status_code == 401
    assert settings.metadata_url in response.headers["www-authenticate"]
    exchange.assert_not_called()
    assert not requests


def test_audit_log_is_off_by_default(audit_records, server, token):
    client, *_ = server
    read_payload(call_profile(client, token()))
    assert audit_records == []


@pytest.mark.parametrize("server", [{"audit_log": True}], indirect=True)
def test_audit_log_records_identity_tool_and_outcome(audit_records, server, token):
    client, _, upstream, *_ = server
    incoming = token(
        azp="66666666-6666-4666-8666-666666666666", preferred_username="test@example.com"
    )
    read_payload(call_profile(client, incoming))
    upstream[0] = httpx.Response(403, text="secret-detail")
    read_payload(call_profile(client, incoming))
    read_payload(call_profile(client, token(upn="upn@example.com"), {"path": "/applications"}))
    read_payload(
        rpc(
            client,
            token(),
            "tools/call",
            {"name": "graph_continue", "arguments": {"continuation": "forged"}},
        )
    )
    rpc(client, incoming, "tools/call", {"name": "graph_capabilities"})
    assert [r.pop("duration_ms") >= 0 for r in audit_records] == [True] * 4
    identity = {"oid": "44444444-4444-4444-8444-444444444444"}
    gateway = {**identity, "username": "test@example.com"}
    gateway["azp"] = "66666666-6666-4666-8666-666666666666"
    assert audit_records == [
        {"event": "tool_call", "tool": "graph_read", "outcome": "success", **gateway},
        {
            "event": "tool_call",
            "tool": "graph_read",
            "outcome": "graph_error",
            "graph_status": 403,
            **gateway,
        },
        {
            "event": "tool_call",
            "tool": "graph_read",
            "outcome": "invalid_operation",
            **identity,
            "username": "upn@example.com",
            "azp": "33333333-3333-4333-8333-333333333333",
        },
        {
            "event": "tool_call",
            "tool": "graph_continue",
            "outcome": "invalid_operation",
            **identity,
            "azp": "33333333-3333-4333-8333-333333333333",
        },
    ]
    logged = json.dumps(audit_records)
    for secret in (incoming, "graph-4444", "Bearer", "/applications", "/me", "forged", "secret"):
        assert secret not in logged


def test_wrong_scope_and_bad_token(server, token):
    client, requests, _, exchange, _, _ = server
    assert call_profile(client, token(scp="other")).status_code == 403
    assert call_profile(client, token(aud="https://graph.microsoft.com")).status_code == 401
    assert call_profile(client, "malformed").status_code == 401
    exchange.assert_not_called()
    assert not requests


def test_obo_claims_challenge_stays_http_401(server, token, settings):
    client, requests, _, exchange, _, _ = server
    claims = '{"access_token":{"acrs":{"essential":true,"value":"c1"}}}'
    exchange.side_effect = None
    exchange.return_value = {
        "error": "interaction_required",
        "claims": claims,
        "error_description": "secret-detail",
    }
    response = call_profile(client, token())
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert settings.metadata_url in challenge
    assert base64.b64encode(claims.encode()).decode() in challenge
    assert "secret-detail" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert not requests


def test_obo_configuration_failure_is_http_503(server, token):
    client, _, _, exchange, _, _ = server
    exchange.side_effect = None
    exchange.return_value = {"error": "invalid_client", "error_description": "secret-detail"}
    response = call_profile(client, token())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert "secret-detail" not in response.text


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "https://evil.example/me"},
        {"path": "/applications"},
        {"method": "POST"},
        {"select": ["id&$expand=manager"]},
    ],
)
def test_unsupported_operation_rejected_before_graph(server, token, arguments):
    client, requests, *_ = server
    response = call_profile(client, token(), arguments)
    assert response.status_code == 200
    assert response.json()["result"]["isError"]
    assert not requests


@pytest.mark.parametrize(
    ("upstream_status", "code"),
    [
        (401, "graph_authentication_required"),
        (403, "graph_access_denied"),
        (429, "graph_throttled"),
        (500, "graph_upstream_error"),
        (302, "graph_upstream_error"),
    ],
)
def test_upstream_failures_are_tool_errors(server, token, upstream_status, code):
    client, requests, upstream, exchange, _, _ = server
    upstream[0] = httpx.Response(
        upstream_status, text="secret-detail", headers={"Location": "https://evil.example/"}
    )
    result, payload = read_payload(call_profile(client, token()))
    assert result["isError"]
    assert payload["error"]["code"] == code
    assert "secret-detail" not in json.dumps(result)
    assert len(requests) == 1  # In particular, no redirects or automatic write/retry behavior.
    if upstream_status == 401:
        read_payload(call_profile(client, token()))
        assert exchange.call_count == 2


@pytest.mark.parametrize("body", ["not JSON", "[]", '{"error":"bad"}', "x" * 65537])
def test_malformed_or_oversized_profile(server, token, body):
    client, _, upstream, *_ = server
    upstream[0] = httpx.Response(200, text=body)
    result, payload = read_payload(call_profile(client, token()))
    assert result["isError"]
    assert payload["status"] == 502


def test_untrusted_host_and_origin_rejected(server, token):
    client, requests, _, exchange, *_ = server
    response = rpc(client, token(), "tools/list", Host="evil.example")
    assert response.status_code == 421
    response = rpc(client, token(), "tools/list", Origin="https://evil.example")
    assert response.status_code == 403
    exchange.assert_not_called()
    assert not requests


@pytest.mark.parametrize("credential", ["valid", "malformed", "wrong-scope"])
def test_public_routes_ignore_bearer_without_contacting_microsoft(
    server, settings, token, jwks, credential
):
    client, requests, _, exchange, *_ = server
    jwks.get_signing_keys = Mock(side_effect=AssertionError("Public route fetched signing keys"))
    incoming = {"valid": token(), "malformed": "malformed", "wrong-scope": token(scp="other")}[
        credential
    ]
    for route in (
        "/healthz",
        "/readyz",
        "/.well-known/oauth-protected-resource",
        settings.metadata_path,
    ):
        response = client.get(route, headers={"Authorization": f"Bearer {incoming}"})
        assert response.status_code == 200
    jwks.get_signing_keys.assert_not_called()
    exchange.assert_not_called()
    assert not requests


def test_ingress_checks_run_before_signing_key_lookup(server, token, jwks):
    client, *_ = server
    jwks.get_signing_keys = Mock(side_effect=AssertionError("Rejected request fetched keys"))
    assert rpc(client, token(), "tools/list", Host="evil.example").status_code == 421
    assert rpc(client, token(), "tools/list", Origin="https://evil.example").status_code == 403
    jwks.get_signing_keys.assert_not_called()


def test_throttle_delay_reaches_model(server, token):
    client, requests, upstream, *_ = server
    upstream[0] = httpx.Response(429, headers={"Retry-After": "37"})
    result, payload = read_payload(call_profile(client, token()))
    assert result["isError"]
    assert payload["retry_after_seconds"] == 37
    assert len(requests) == 1


@pytest.mark.parametrize(
    "arguments,expected_content_type",
    [
        (
            {"method": "POST", "path": "/me/onenote/sections/s/pages", "html": "<p>Notes</p>"},
            "text/html; charset=utf-8",
        ),
        (
            {
                "method": "PATCH",
                "path": "/me/onenote/pages/p/content",
                "body": [{"target": "body", "action": "append", "content": "<p>Next</p>"}],
            },
            "application/json",
        ),
    ],
)
def test_onenote_payloads_through_mcp(server, token, arguments, expected_content_type):
    client, requests, upstream, *_ = server
    upstream[0] = httpx.Response(204)
    result, payload = read_payload(
        rpc(client, token(), "tools/call", {"name": "graph_write", "arguments": arguments})
    )
    assert not result.get("isError")
    assert payload["status"] == 204
    assert requests[-1].headers["content-type"] == expected_content_type
    if "html" in arguments:
        assert requests[-1].content.decode() == arguments["html"]
    else:
        assert json.loads(requests[-1].content) == arguments["body"]


def test_expanded_tools_discover_and_execute_via_mcp(server, token):
    client, requests, upstream, *_ = server
    incoming = token()
    tools = rpc(client, incoming, "tools/list").json()["result"]["tools"]
    annotations = {t["name"]: t["annotations"] for t in tools}
    assert annotations["graph_read"]["readOnlyHint"] is True
    assert annotations["graph_write"]["readOnlyHint"] is False
    _, capabilities = read_payload(
        rpc(client, incoming, "tools/call", {"name": "graph_capabilities"})
    )
    assert {s["name"] for s in capabilities["services"]} >= {
        "mail",
        "calendar",
        "files",
        "teams",
        "todo",
        "planner",
        "excel",
        "sites",
        "meetings",
        "copilot",
        "directory",
        "onenote",
        "presence",
    }
    assert capabilities["stage"] != "authentication"
    _, catalog = read_payload(
        rpc(
            client,
            incoming,
            "tools/call",
            {"name": "graph_describe", "arguments": {"path": "/me/sendMail"}},
        )
    )
    assert catalog["operations"][0]["method"] == "POST"
    upstream[0] = httpx.Response(200, json={"value": [{"id": "message", "subject": "Hello"}]})
    result, payload = read_payload(
        call_profile(client, incoming, {"path": "/me/messages", "query": {"$top": "5"}})
    )
    assert not result.get("isError") and payload["data"]["value"][0]["subject"] == "Hello"
    upstream[0] = httpx.Response(202)
    result, payload = read_payload(
        rpc(
            client,
            incoming,
            "tools/call",
            {
                "name": "graph_write",
                "arguments": {
                    "path": "/me/sendMail",
                    "method": "POST",
                    "body": {"message": {"subject": "fixture"}},
                },
            },
        )
    )
    assert not result.get("isError") and payload["outcome"] == "accepted"
    assert requests[-1].method == "POST"


def test_mcp_read_tool_cannot_send_mail(server, token):
    client, requests, *_ = server
    result, _ = read_payload(
        call_profile(client, token(), {"path": "/me/sendMail", "method": "POST", "body": {}})
    )
    assert result["isError"] and not requests
