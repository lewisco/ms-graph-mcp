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
        {"obo_max_concurrent_exchanges": 0},
        {"obo_max_concurrent_exchanges": 33},
        {"max_request_body_bytes": 59999},
        {"max_request_body_bytes": 2000001},
    ],
)
def test_invalid_identity_or_ingress_settings(settings, changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(settings.model_dump() | changes))


def test_secret_redacted(settings):
    assert "test-secret-never-log" not in repr(settings)
    assert "test-secret-never-log" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "site",
    [
        "http://tenant.sharepoint.com/sites/Hub",
        "https://tenant.sharepoint.com.evil.test/sites/Hub",
        "https://user:password@tenant.sharepoint.com/sites/Hub",
        "https://tenant.sharepoint.com:443/sites/Hub",
        "https://tenant.sharepoint.com/sites/../other",
        "https://tenant.sharepoint.com/sites/%2e%2e/other",
        "https://tenant.sharepoint.com/sites/Hub?x=1",
        "https://tenant.sharepoint.com/sites/Hub#fragment",
        "https://tenant.sharepoint.com/sites//Hub",
        "https://tenant.sharepoint.com/sites/Hub\n",
        "https://evil.test/sites/Hub",
    ],
)
def test_sharepoint_site_is_a_fixed_https_destination(settings, site):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(settings.model_dump() | {"sharepoint_site_url": site}))


def test_sharepoint_and_payload_settings(settings):
    assert settings.sharepoint_site_url is None
    assert settings.sharepoint_resource_origin is None
    configured = Settings(
        _env_file=None,
        **(
            settings.model_dump()
            | {
                "sharepoint_site_url": "https://Tenant.sharepoint.com/sites/AI-Hub/",
                "max_request_body_bytes": 1000000,
            }
        ),
    )
    assert configured.sharepoint_site_url == "https://tenant.sharepoint.com/sites/AI-Hub"
    assert configured.sharepoint_resource_origin == "https://tenant.sharepoint.com"
    assert configured.max_mcp_request_bytes == 6065536
    root = Settings(
        _env_file=None,
        **(
            settings.model_dump()
            | {
                "sharepoint_site_url": "https://tenant.sharepoint.com/",
            }
        ),
    )
    assert root.sharepoint_site_url == "https://tenant.sharepoint.com"
