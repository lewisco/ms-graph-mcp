import json
from unittest.mock import Mock

import httpx
from starlette.testclient import TestClient

from ms_graph_mcp.app import create_app
from ms_graph_mcp.auth import EntraVerifier
from ms_graph_mcp.obo import OboClient


def test_cold_replica_accepts_same_user_after_other_replica_stops(settings, jwks, token):
    exchanges = []

    def replica():
        exchange = Mock(return_value={"access_token": "graph-token", "expires_in": 3600})
        exchanges.append(exchange)
        obo = OboClient(settings, exchange=exchange)
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": "same-user"})
            )
        )
        return TestClient(
            create_app(
                settings, obo=obo, verifier=EntraVerifier(settings, obo, jwks=jwks), graph_http=http
            )
        )

    incoming = token()

    def read(client):
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {incoming}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "graph_read", "arguments": {}},
            },
        )
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers
        return json.loads(response.json()["result"]["content"][0]["text"])["data"]

    with replica() as survivor:
        with replica() as first:
            assert read(first) == {"id": "same-user"}
        # No shared token cache/session store survives from the closed application.
        assert read(survivor) == {"id": "same-user"}
        assert read(survivor) == {"id": "same-user"}
    assert [exchange.call_count for exchange in exchanges] == [1, 1]
