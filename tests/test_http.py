import base64
import json
from unittest.mock import Mock

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from ms_graph_mcp.app import create_app
from ms_graph_mcp.auth import EntraVerifier
from ms_graph_mcp.obo import OboClient

HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def server(settings, jwks):
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
