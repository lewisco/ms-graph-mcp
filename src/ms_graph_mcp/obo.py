"""Bounded, per-assertion OBO caching; never fall back to app-only tokens."""

import asyncio
import hashlib
import json
import ssl
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import msal
import requests
from pydantic import SecretStr

from ms_graph_mcp.config import Settings
from ms_graph_mcp.errors import AuthFailure
from ms_graph_mcp.tls import EntraTLSAdapter, create_ssl_context

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


@dataclass(frozen=True)
class GraphToken:
    value: SecretStr = field(repr=False)
    expires_at: float


class _EntraSession(requests.Session):
    def __init__(self, timeout: float, context: ssl.SSLContext):
        super().__init__()
        self.timeout = timeout
        self.trust_env = False
        self.mount("https://", EntraTLSAdapter(context))

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs["timeout"] = self.timeout
        kwargs["allow_redirects"] = False
        kwargs["verify"] = True
        return super().request(method, url, **kwargs)


class OboClient:
    def __init__(
        self,
        settings: Settings,
        *,
        exchange: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self._exchange = exchange or self._exchange_sync
        self._clock = clock
        self._cache: OrderedDict[str, GraphToken] = OrderedDict()
        self._lock = asyncio.Lock()
        # Only one fixed Entra authority. This caches public discovery metadata, not tokens.
        self._http_cache: dict[str, Any] = {}
        self._ssl_context = create_ssl_context(settings)

    def _exchange_sync(self, assertion: str) -> dict[str, Any]:
        with _EntraSession(self.settings.http_timeout_seconds, self._ssl_context) as session:
            # A short-lived MSAL client avoids retaining a second, unbounded token cache.
            # Only successful access tokens enter our bounded cache; refresh tokens are discarded.
            client = msal.ConfidentialClientApplication(
                str(self.settings.client_id),
                authority=self.settings.authority,
                client_credential=self.settings.client_secret.get_secret_value(),
                http_client=session,
                http_cache=self._http_cache,
                instance_discovery=False,
                exclude_scopes=["offline_access"],
                client_capabilities=["CP1"],
                enable_pii_log=False,
            )
            return client.acquire_token_on_behalf_of(assertion, scopes=GRAPH_SCOPES)

    async def acquire(self, assertion: str, assertion_expiry: int) -> GraphToken:
        key = hashlib.sha256(assertion.encode()).hexdigest()
        # Serialize misses to avoid duplicate exchanges and races in MSAL's discovery cache.
        # This first slice is designed for modest internal concurrency.
        async with self._lock:
            now = self._clock()
            for stale in [k for k, v in self._cache.items() if v.expires_at <= now + 60]:
                del self._cache[stale]
            if cached := self._cache.get(key):
                self._cache.move_to_end(key)
                return cached
            if assertion_expiry <= now:
                raise AuthFailure(
                    401, "invalid_token", "Reconnect Microsoft; the user token expired."
                )
            try:
                result = await asyncio.to_thread(self._exchange, assertion)
            except (requests.RequestException, ValueError, OSError):
                raise AuthFailure(
                    503, "temporarily_unavailable", "Microsoft token exchange is unavailable."
                ) from None
            if not isinstance(result, dict):
                raise AuthFailure(
                    503, "temporarily_unavailable", "Invalid token exchange response."
                )
            access = result.get("access_token")
            if isinstance(access, str) and access:
                try:
                    expires = min(self._clock() + int(result["expires_in"]), assertion_expiry)
                except (KeyError, ValueError, TypeError, OverflowError):
                    raise AuthFailure(
                        503, "temporarily_unavailable", "Invalid token expiry from Microsoft."
                    ) from None
                if expires <= self._clock():
                    raise AuthFailure(
                        401, "invalid_token", "Reconnect Microsoft; the token expired."
                    )
                token = GraphToken(SecretStr(access), expires)
                self._cache[key] = token
                while len(self._cache) > self.settings.obo_cache_entries:
                    self._cache.popitem(last=False)
                return token
            error = result.get("error")
            if not isinstance(error, str):
                raise AuthFailure(
                    503, "temporarily_unavailable", "Invalid token exchange response."
                )
            if error in {
                "invalid_grant",
                "interaction_required",
                "consent_required",
                "login_required",
            }:
                claims = result.get("claims")
                if isinstance(claims, str) and len(claims) <= 4096:
                    try:
                        claims = json.dumps(json.loads(claims), separators=(",", ":"))
                    except (ValueError, TypeError):
                        claims = None
                else:
                    claims = None
                raise AuthFailure(
                    401,
                    "invalid_token",
                    "Reconnect Microsoft to satisfy consent or sign-in policy.",
                    claims=claims,
                )
            if error in {"invalid_client", "unauthorized_client", "invalid_scope"}:
                raise AuthFailure(
                    503,
                    "configuration_error",
                    "Check the MCP Entra registration and Graph consent.",
                )
            raise AuthFailure(503, "temporarily_unavailable", "Microsoft token exchange failed.")

    def invalidate(self, assertion: str) -> None:
        self._cache.pop(hashlib.sha256(assertion.encode()).hexdigest(), None)

    def clear(self) -> None:
        self._cache.clear()
        self._http_cache.clear()
