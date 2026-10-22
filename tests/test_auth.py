import time
from unittest.mock import AsyncMock

import jwt
import pytest
from pydantic import SecretStr

from ms_graph_mcp.auth import EntraVerifier
from ms_graph_mcp.errors import AuthFailure
from ms_graph_mcp.obo import GraphToken

GATEWAY_CLIENT = "66666666-6666-4666-8666-666666666666"
UNLISTED_CLIENT = "77777777-7777-4777-8777-777777777777"


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
    "extra",
    [
        {},
        # Entra omits OIDC scopes from resource tokens, but extra scopes must not matter.
        {"scp": "access_as_user offline_access"},
        # Clients that declare CP1 or satisfy CA auth contexts get these claims.
        {"xms_cc": ["CP1"], "acrs": ["c1"]},
        {"name": "Test User", "preferred_username": "test@example.com", "uti": "x", "rh": "y"},
    ],
)
async def test_gateway_oauth2_client_token_accepted(settings, jwks, token, obo, extra):
    incoming = token(azp=GATEWAY_CLIENT, **extra)
    identity = await EntraVerifier(settings, obo, jwks=jwks).verify_token(incoming)
    assert identity.client_id == GATEWAY_CLIENT
    assert identity.subject == "44444444-4444-4444-8444-444444444444"
    obo.acquire.assert_awaited_once()
    assert obo.acquire.await_args.args[0] == incoming


async def test_gateway_refreshed_token_is_exchanged_again(settings, jwks, token, obo):
    verifier = EntraVerifier(settings, obo, jwks=jwks)
    first = token(azp=GATEWAY_CLIENT)
    now = int(time.time())
    refreshed = token(azp=GATEWAY_CLIENT, iat=now, nbf=now, exp=now + 4000)
    for incoming in (first, refreshed):
        assert (await verifier.verify_token(incoming)).client_id == GATEWAY_CLIENT
    assert [call.args[0] for call in obo.acquire.await_args_list] == [first, refreshed]


async def test_unlisted_oauth2_client_rejected(settings, jwks, token, obo):
    verifier = EntraVerifier(settings, obo, jwks=jwks)
    assert await verifier.verify_token(token(azp=UNLISTED_CLIENT)) is None
    obo.acquire.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://graph.microsoft.com"},
        {"aud": ["22222222-2222-4222-8222-222222222222"]},
        # v1-form audience/issuer, as issued when the API manifest is not set to v2 tokens.
        {"aud": "api://22222222-2222-4222-8222-222222222222"},
        {"iss": "https://sts.windows.net/11111111-1111-4111-8111-111111111111/"},
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
