"""Bound JWKS refreshes without holding cached-key lookups behind network I/O."""

import asyncio
import time
from collections.abc import Callable

import jwt


class SigningKeyCache:
    def __init__(self, client: jwt.PyJWKClient, *, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._next_refresh = 0.0
        self._refresh_task: asyncio.Task[None] | None = None
        self._unavailable = False
        self._closed = False

    async def get(self, kid: str) -> jwt.PyJWK | None:
        if self._closed:
            raise jwt.PyJWKClientConnectionError("Signing-key cache is shutting down.")
        now = self._clock()
        fresh = now < self._expires_at
        if fresh and (key := self._keys.get(kid)):
            return key

        task = self._refresh_task
        if task is not None and fresh:
            # Unknown IDs cannot accumulate waiters behind a rotation refresh.
            return None
        if task is None and now >= self._next_refresh:
            self._next_refresh = now + 30
            task = self._refresh_task = asyncio.create_task(self._refresh())
        if task is not None:
            # A disconnected caller must not cancel a refresh or free its slot early.
            await asyncio.shield(task)
        if self._clock() >= self._expires_at or self._unavailable:
            raise jwt.PyJWKClientConnectionError("Signing keys are temporarily unavailable.")
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        try:
            # Use our TTL/cooldown, never PyJWT's per-unknown-kid automatic refresh.
            keys = await asyncio.to_thread(self.client.get_signing_keys, refresh=True)
            replacement = {key.key_id: key for key in keys if key.key_id}
            if not replacement:
                raise ValueError("No signing keys")
            self._keys = replacement
            self._expires_at = self._clock() + 300
            self._unavailable = False
        except jwt.PyJWTError, ValueError, TypeError, OSError, RecursionError:
            # Keep previously trusted keys only until their original TTL expires.
            self._unavailable = True
        finally:
            self._next_refresh = self._clock() + 30
            self._refresh_task = None

    async def aclose(self) -> None:
        self._closed = True
        if self._refresh_task is not None:
            await asyncio.shield(self._refresh_task)
        self._keys.clear()
        self._expires_at = 0.0
