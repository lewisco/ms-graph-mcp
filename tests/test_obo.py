import asyncio
import threading
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
async def test_failures_never_enter_token_cache_or_leak(settings, result, status):
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


@pytest.fixture
def blocked_exchange():
    entered, release = threading.Event(), threading.Event()

    def exchange(assertion):
        if assertion.startswith("blocked"):
            entered.set()
            assert release.wait(2)
        if assertion == "blocked-denied":
            return {"error": "interaction_required", "error_description": "private detail"}
        return {"access_token": f"graph-{assertion}", "expires_in": 3600}

    try:
        yield Mock(side_effect=exchange), entered, release
    finally:
        release.set()


async def test_failed_user_does_not_block_cached_or_new_users(settings, blocked_exchange):
    exchange, entered, release = blocked_exchange
    client = OboClient(settings, exchange=exchange, clock=lambda: 1000)
    cached = await client.acquire("cached-user", 5000)
    denied = asyncio.create_task(client.acquire("blocked-denied", 5000))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await asyncio.wait_for(client.acquire("cached-user", 5000), 0.5) is cached
        other = await asyncio.wait_for(client.acquire("new-user", 5000), 0.5)
        assert other.value != cached.value
        assert not denied.done()
    finally:
        release.set()
    with pytest.raises(AuthFailure):
        await denied
    await client.aclose()
    assert not client._cache and not client._failures


async def test_single_flight_and_limit_survive_caller_cancellation(settings, blocked_exchange):
    settings.obo_max_concurrent_exchanges = 1
    exchange, entered, release = blocked_exchange
    client = OboClient(settings, exchange=exchange, clock=lambda: 1000)
    first = asyncio.create_task(client.acquire("blocked-ok", 5000))
    assert await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(client.acquire("blocked-ok", 5000))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    try:
        with pytest.raises(AuthFailure, match="busy") as failure:
            await client.acquire("another-user", 5000)
        assert failure.value.status == 503
        assert len(client._pending) == 1
        assert exchange.call_count == 1
    finally:
        release.set()
    assert (await second).value.get_secret_value() == "graph-blocked-ok"
    assert exchange.call_count == 1
    await client.aclose()


async def test_failure_backoff_is_bounded_and_preserves_claims(settings):
    settings.obo_cache_entries = 2
    now = [1000]
    claims = '{"access_token":{"acrs":{"value":"c1"}}}'
    exchange = Mock(
        return_value={
            "error": "interaction_required",
            "claims": claims,
            "error_description": "private detail",
        }
    )
    client = OboClient(settings, exchange=exchange, clock=lambda: now[0])
    for _ in range(5):
        with pytest.raises(AuthFailure) as failure:
            await client.acquire("assertion-a", 5000)
        assert failure.value.status == 401
        assert failure.value.claims == claims
    assert exchange.call_count == 1
    assert "private detail" not in repr(client._failures)
    now[0] += 10
    with pytest.raises(AuthFailure):
        await client.acquire("assertion-a", 5000)
    assert exchange.call_count == 2
    for assertion in ("assertion-b", "assertion-c"):
        with pytest.raises(AuthFailure):
            await client.acquire(assertion, 5000)
    assert len(client._failures) == 2
    assert all("assertion" not in key for key in client._failures)
    client.invalidate("assertion-c")
    with pytest.raises(AuthFailure):
        await client.acquire("assertion-c", 5000)
    assert exchange.call_count == 5


async def test_concurrent_msal_clients_do_not_share_mutable_caches(settings, monkeypatch):
    application = Mock()
    application.acquire_token_on_behalf_of.return_value = {
        "access_token": "graph",
        "expires_in": 3600,
    }
    constructor = Mock(return_value=application)
    monkeypatch.setattr("ms_graph_mcp.obo.msal.ConfidentialClientApplication", constructor)
    client = OboClient(settings, clock=lambda: 1000)
    await asyncio.gather(client.acquire("a", 5000), client.acquire("b", 5000))
    caches = [call.kwargs["http_cache"] for call in constructor.call_args_list]
    assert len(caches) == 2 and caches[0] is not caches[1]
