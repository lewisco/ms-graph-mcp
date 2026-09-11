"""Validate tenant-specific Entra v2 access tokens before performing OBO."""

from uuid import UUID

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import Field, SecretStr

from ms_graph_mcp.config import Settings
from ms_graph_mcp.errors import AuthFailure
from ms_graph_mcp.obo import OboClient
from ms_graph_mcp.signing_keys import SigningKeyCache
from ms_graph_mcp.tls import create_ssl_context


class DelegatedAccessToken(AccessToken):
    token: str = Field(repr=False, exclude=True)
    graph_token: SecretStr = Field(repr=False, exclude=True)


class EntraVerifier:
    def __init__(self, settings: Settings, obo: OboClient, *, jwks: jwt.PyJWKClient | None = None):
        self.settings = settings
        self.obo = obo
        self.jwks = jwks or jwt.PyJWKClient(
            settings.jwks_url,
            lifespan=300,
            timeout=settings.http_timeout_seconds,
            ssl_context=create_ssl_context(settings),
        )
        self._signing_keys = SigningKeyCache(self.jwks)

    async def verify_token(self, token: str) -> DelegatedAccessToken | None:
        if len(token) > 32768:
            return None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return None
            # JWKS location comes only from the configured tenant, never from jku/x5u in a JWT.
            key = await self._signing_keys.get(header["kid"])
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=str(self.settings.client_id),
                issuer=self.settings.issuer,
                options={
                    "strict_aud": True,
                    "require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "azp", "ver"],
                },
            )
            if claims["ver"] != "2.0" or claims["tid"] != str(self.settings.tenant_id):
                return None
            if any(type(claims[name]) is not int for name in ("exp", "iat", "nbf")):
                return None
            user_id = str(UUID(claims["oid"]))
            client_id = str(UUID(claims["azp"]))
            if client_id not in {str(c) for c in self.settings.allowed_client_ids}:
                return None
            scopes = claims.get("scp")
            if claims.get("idtyp") == "app" or not isinstance(scopes, str) or not scopes:
                return None
            if self.settings.scope_name not in scopes.split():
                raise AuthFailure(403, "insufficient_scope", "The MCP delegated scope is required.")
        except jwt.PyJWKClientConnectionError:
            raise AuthFailure(
                503, "temporarily_unavailable", "Microsoft signing keys are unavailable."
            ) from None
        except jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError, RecursionError:
            return None

        # Resolve OBO before opening the MCP response: consent/CA challenges can then be HTTP 401s.
        # The result is cached, but every request still validates the caller's current JWT.
        graph_token = await self.obo.acquire(token, int(claims["exp"]))
        return DelegatedAccessToken(
            token=token,
            client_id=client_id,
            subject=user_id,
            scopes=[self.settings.oauth_scope],
            expires_at=int(min(claims["exp"], graph_token.expires_at)),
            resource=self.settings.resource_url,
            claims={"iss": self.settings.issuer, "tid": str(self.settings.tenant_id)},
            graph_token=graph_token.value,
        )

    async def aclose(self) -> None:
        await self._signing_keys.aclose()
