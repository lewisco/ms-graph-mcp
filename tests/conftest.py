import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ms_graph_mcp.config import Settings

TENANT = "11111111-1111-4111-8111-111111111111"
RESOURCE = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"
OTHER_USER = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        tenant_id=TENANT,
        client_id=RESOURCE,
        client_secret="test-secret-never-log",
        allowed_client_ids=[CLIENT],
        resource_url="https://gateway.example/msgraph/mcp",
        allowed_hosts=["testserver", "mcp.internal:8000"],
    )


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(signing_key):
    # Only network key retrieval is replaced; actual JWT signature/claim checks still run.
    key = SimpleNamespace(key=signing_key.public_key(), key_id="test-key")
    return SimpleNamespace(get_signing_keys=lambda *, refresh=False: [key])


@pytest.fixture
def token(settings, signing_key):
    def make(*, omit=(), **overrides):
        now = int(time.time())
        claims = {
            "iss": settings.issuer,
            "aud": RESOURCE,
            "tid": TENANT,
            "azp": CLIENT,
            "oid": USER,
            "ver": "2.0",
            "scp": "access_as_user",
            "iat": now - 10,
            "nbf": now - 10,
            "exp": now + 3600,
        }
        claims.update(overrides)
        for name in omit:
            claims.pop(name, None)
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})

    return make
