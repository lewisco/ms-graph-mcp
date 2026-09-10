import pytest
from pydantic import ValidationError

from ms_graph_mcp.config import Settings


@pytest.mark.parametrize(
    "changes",
    [
        {"resource_url": "http://gateway.example/mcp"},
        {"resource_url": "https://gateway.example/mcp?token=secret"},
        {"resource_url": "https://user:password@gateway.example/mcp"},
        {"resource_url": "https://gateway.example/mcp/"},
        {"allowed_hosts": ["*"]},
        {"allowed_client_ids": []},
        {"allowed_origins": ["https://*.example"]},
        {"client_secret": ""},
        {"scope_name": "a b"},
    ],
)
def test_invalid_identity_or_ingress_settings(settings, changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(settings.model_dump() | changes))


def test_secret_redacted(settings):
    assert "test-secret-never-log" not in repr(settings)
    assert "test-secret-never-log" not in settings.model_dump_json()
