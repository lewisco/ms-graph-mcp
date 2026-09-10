import asyncio
from unittest.mock import Mock

import pytest
import requests

from ms_graph_mcp.errors import AuthFailure
from ms_graph_mcp.obo import GRAPH_SCOPES, OboClient


async def test_cache_isolated_bounded_and_expires(settings):
    settings.obo_cache_entries = 2
    now = [1000]
    exchange = Mock(
        side_effect=lambda assertion: {
            "access_token": f"graph-{assertion}",
            "expires_in": 3600,
        }
    )
    client = OboClient(settings, exchange=exchange, clock=lambda: now[0])
    a, again, b = await asyncio.gather(
        client.acquire("user-a", 5000),
        client.acquire("user-a", 5000),
        client.acquire("user-b", 5000),
    )
    assert a == again
    assert a.value.get_secret_value() != b.value.get_secret_value()
    assert exchange.call_count == 2
    await client.acquire("user-a-rotated", 1500)
    assert len(client._cache) == 2
    assert all("user-" not in key for key in client._cache)
    await client.acquire("user-a", 5000)
    assert exchange.call_count == 4  # Oldest assertion was evicted.
    now[0] = 4541
    await client.acquire("user-a", 5000)
    assert exchange.call_count == 5  # Refresh within 60 seconds of expiry.
    client.invalidate("user-a")
    await client.acquire("user-a", 5000)
    assert exchange.call_count == 6
    client.clear()
    assert not client._cache


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"error": "invalid_grant", "error_description": "secret detail"}, 401),
        ({"error": "interaction_required"}, 401),
        ({"error": "invalid_client"}, 503),
        ({"error": "invalid_scope"}, 503),
        ({"error": []}, 503),
        ([], 503),
        ({"access_token": "secret-access", "expires_in": "bad"}, 503),
        ({"access_token": "secret-access", "expires_in": 0}, 401),
    ],
)
async def test_failures_never_cached_or_leaked(settings, result, status):
    client = OboClient(settings, exchange=lambda _: result, clock=lambda: 1000)
    with pytest.raises(AuthFailure) as failure:
        await client.acquire("user-assertion", 5000)
    assert failure.value.status == status
    assert "secret" not in str(failure.value)
    assert not client._cache


async def test_expired_assertion_never_exchanged(settings):
    exchange = Mock()
    with pytest.raises(AuthFailure):
        await OboClient(settings, exchange=exchange, clock=lambda: 1000).acquire("a", 999)
    exchange.assert_not_called()


async def test_network_failure_sanitized(settings):
    client = OboClient(settings, exchange=Mock(side_effect=requests.Timeout("secret")))
    with pytest.raises(AuthFailure) as failure:
        await client.acquire("assertion", 9999999999)
    assert failure.value.status == 503
    assert "secret" not in str(failure.value)


async def test_msal_uses_obo_and_only_graph_scope(settings, monkeypatch):
    application = Mock()
    application.acquire_token_on_behalf_of.return_value = {
        "access_token": "graph",
        "expires_in": 3600,
    }
    constructor = Mock(return_value=application)
    monkeypatch.setattr("ms_graph_mcp.obo.msal.ConfidentialClientApplication", constructor)
    await OboClient(settings).acquire("user-assertion", 9999999999)
    application.acquire_token_on_behalf_of.assert_called_once_with(
        "user-assertion", scopes=GRAPH_SCOPES
    )
    assert constructor.call_args.kwargs["authority"] == settings.authority
    assert constructor.call_args.kwargs["exclude_scopes"] == ["offline_access"]
    application.acquire_token_for_client.assert_not_called()
