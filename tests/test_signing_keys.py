import asyncio
import io
import json
import threading
from unittest.mock import Mock
from urllib.error import URLError

import jwt
import pytest

from ms_graph_mcp.signing_keys import SigningKeyCache


@pytest.fixture
def key_cache(monkeypatch, signing_key):
    key = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    key["kid"] = "known"
    body = {"keys": [key]}
    now = [1000.0]
    network = Mock(side_effect=lambda *a, **kw: io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr("jwt.jwks_client.urllib.request.urlopen", network)
    cache = SigningKeyCache(jwt.PyJWKClient("https://keys.example/jwks"), clock=lambda: now[0])
    return cache, now, body, network


async def test_unknown_ids_share_one_refresh_cooldown(key_cache):
    cache, now, _, network = key_cache
    assert await cache.get("known")
    for index in range(100):
        assert await cache.get(f"unknown-{index}") is None
    assert network.call_count == 1
    now[0] += 30
    results = await asyncio.gather(*(cache.get(f"unknown-{i}") for i in range(100)))
    assert results == [None] * 100
    assert network.call_count == 2


async def test_cached_keys_do_not_wait_for_unknown_key_network_refresh(key_cache):
    cache, now, body, network = key_cache
    known = await cache.get("known")
    now[0] += 30
    entered, release = threading.Event(), threading.Event()

    def slow_response(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return io.BytesIO(json.dumps(body).encode())

    network.side_effect = slow_response
    attacker = asyncio.create_task(cache.get("unknown"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await asyncio.wait_for(cache.get("known"), 0.5) is known
        assert await asyncio.wait_for(cache.get("another-unknown"), 0.5) is None
        assert not attacker.done()
    finally:
        release.set()
        await attacker


async def test_failed_refresh_preserves_unexpired_keys_and_backs_off(key_cache):
    cache, now, _, network = key_cache
    known = await cache.get("known")
    network.side_effect = URLError("private upstream detail")
    now[0] += 30
    with pytest.raises(jwt.PyJWKClientConnectionError):
        await cache.get("unknown")
    assert await cache.get("known") is known
    now[0] = 1301  # A failed fetch must never extend trust in expired signing keys.
    for _ in range(10):
        with pytest.raises(jwt.PyJWKClientConnectionError, match="temporarily unavailable"):
            await cache.get("known")
    assert network.call_count == 3


async def test_rotation_replaces_keys_after_cooldown(key_cache):
    cache, now, body, network = key_cache
    assert await cache.get("known")
    body["keys"][0]["kid"] = "rotated"
    now[0] += 29
    assert await cache.get("rotated") is None
    now[0] += 1
    assert (await cache.get("rotated")).key_id == "rotated"
    assert await cache.get("known") is None
    assert network.call_count == 2


async def test_cancelled_caller_does_not_duplicate_cold_refresh(key_cache):
    cache, _, body, network = key_cache
    entered, release = threading.Event(), threading.Event()

    def slow_response(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return io.BytesIO(json.dumps(body).encode())

    network.side_effect = slow_response
    first = asyncio.create_task(cache.get("known"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(cache.get("known"))
        await asyncio.sleep(0)
        assert not second.done()
        assert network.call_count == 1
    finally:
        release.set()
    assert (await second).key_id == "known"
    assert network.call_count == 1
    await cache.aclose()
    with pytest.raises(jwt.PyJWKClientConnectionError, match="shutting down"):
        await cache.get("known")


async def test_malformed_key_response_is_sanitized_and_throttled(key_cache):
    cache, _, _, network = key_cache
    network.side_effect = lambda *a, **kw: io.BytesIO(b'{"keys":[]}')
    for _ in range(2):
        with pytest.raises(jwt.PyJWKClientConnectionError, match="temporarily unavailable"):
            await cache.get("known")
    assert network.call_count == 1
