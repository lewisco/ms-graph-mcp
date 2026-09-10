import time
from unittest.mock import AsyncMock

import jwt
import pytest
from pydantic import SecretStr

from ms_graph_mcp.auth import EntraVerifier
from ms_graph_mcp.errors import AuthFailure
from ms_graph_mcp.obo import GraphToken


@pytest.fixture
def obo():
    return AsyncMock(
        acquire=AsyncMock(
            return_value=GraphToken(SecretStr("downstream-token"), time.time() + 3600)
        )
    )


async def test_valid_delegated_token(settings, jwks, token, obo):
    incoming = token()
    identity = await EntraVerifier(settings, obo, jwks=jwks).verify_token(incoming)
    assert identity.scopes == [settings.oauth_scope]
    assert identity.resource == settings.resource_url
    assert identity.graph_token.get_secret_value() == "downstream-token"
    assert incoming not in repr(identity)
    assert "downstream-token" not in identity.model_dump_json()
    obo.acquire.assert_awaited_once()


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://graph.microsoft.com"},
        {"aud": ["22222222-2222-4222-8222-222222222222"]},
        {"iss": "https://login.microsoftonline.com/common/v2.0"},
        {"tid": "55555555-5555-4555-8555-555555555555"},
        {"azp": "55555555-5555-4555-8555-555555555555"},
        {"oid": "not-a-user-id"},
        {"ver": "1.0"},
        {"scp": "", "roles": ["access_as_user"]},
        {"idtyp": "app"},
        {"exp": 1},
        {"exp": "9999999999"},
        {"nbf": 9999999999},
        {"iat": 9999999999},
    ],
)
async def test_invalid_identity_never_exchanges(settings, jwks, token, obo, overrides):
    assert await EntraVerifier(settings, obo, jwks=jwks).verify_token(token(**overrides)) is None
    obo.acquire.assert_not_called()


@pytest.mark.parametrize("missing", ["exp", "iat", "nbf", "oid", "tid", "azp", "scp"])
async def test_missing_claims(settings, jwks, token, obo, missing):
    assert await EntraVerifier(settings, obo, jwks=jwks).verify_token(token(omit=[missing])) is None
    obo.acquire.assert_not_called()


async def test_wrong_scope_is_forbidden(settings, jwks, token, obo):
    with pytest.raises(AuthFailure) as failure:
        await EntraVerifier(settings, obo, jwks=jwks).verify_token(token(scp="other"))
    assert failure.value.status == 403
    obo.acquire.assert_not_called()


@pytest.mark.parametrize(
    "incoming",
    [
        "broken",
        "x" * 33000,
        jwt.encode(
            {"scp": "access_as_user"}, "x" * 32, algorithm="HS256", headers={"kid": "test-key"}
        ),
    ],
)
async def test_bad_token_format(settings, jwks, obo, incoming):
    assert await EntraVerifier(settings, obo, jwks=jwks).verify_token(incoming) is None
    obo.acquire.assert_not_called()


async def test_wrong_signature(settings, jwks, obo, token, signing_key):
    payload = jwt.decode(token(), options={"verify_signature": False})
    payload["oid"] = "55555555-5555-4555-8555-555555555555"
    # Mutate the signed payload without changing the signature.
    original = token()
    mutated = jwt.encode(payload, signing_key, algorithm="RS256", headers={"kid": "test-key"})
    incoming = ".".join([*mutated.split(".")[:2], original.split(".")[2]])
    assert await EntraVerifier(settings, obo, jwks=jwks).verify_token(incoming) is None
    obo.acquire.assert_not_called()


async def test_jwks_outage_is_retryable(settings, jwks, obo, token):
    def fail(**kwargs):
        raise jwt.PyJWKClientConnectionError("internal network detail")

    jwks.get_signing_keys = fail
    with pytest.raises(AuthFailure) as failure:
        await EntraVerifier(settings, obo, jwks=jwks).verify_token(token())
    assert failure.value.status == 503
    assert "internal network" not in str(failure.value)
    obo.acquire.assert_not_called()
