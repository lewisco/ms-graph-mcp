import json
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from ms_graph_mcp.auth import DelegatedAccessToken
from ms_graph_mcp.graph import GraphFailure
from ms_graph_mcp.obo import GraphToken
from ms_graph_mcp.sharepoint import SharePointClient

SITE = "https://contoso.sharepoint.com/sites/AIHub"
NODE = {"Id": 17, "Title": "Guides", "Url": "/sites/AIHub/SitePages/Guides.aspx"}
THEME = {"name": "BTIG", "themeJson": '{"palette":{"themePrimary":"#0050A4"}}'}


@pytest.fixture
def identity():
    return DelegatedAccessToken(
        token="user-assertion",
        graph_token=SecretStr("graph-only-token"),
        client_id="client",
        subject="user",
        scopes=["access_as_user"],
        claims={"tid": "tenant"},
        expires_at=int(time.time()) + 3600,
    )


@pytest.fixture
async def sharepoint(settings):
    requests = []
    responses = []

    def handler(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    obo = Mock()
    obo.resource_origin = "https://contoso.sharepoint.com"
    obo.acquire = AsyncMock(
        return_value=GraphToken(SecretStr("sharepoint-token"), time.time() + 3600)
    )
    configured = settings.model_copy(
        update={"sharepoint_site_url": SITE, "max_request_body_bytes": 1_000_000}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield SharePointClient(http, obo, configured), requests, responses, obo


async def test_settings_exchanges_the_assertion_for_sharepoint_only(sharepoint, identity):
    api, requests, responses, obo = sharepoint
    responses.append(httpx.Response(200, json={"Id": "web-id", "Title": "AI Hub"}))
    result = await api.read_settings(identity)
    obo.acquire.assert_awaited_once_with(identity.token, identity.expires_at)
    assert requests[0].headers["authorization"] == "Bearer sharepoint-token"
    assert str(requests[0].url).startswith(SITE + "/_api/web?")
    assert result["site_url"] == SITE
    assert "graph-only-token" not in json.dumps(result)
    assert "sharepoint-token" not in json.dumps(result)
    assert result["outcome"] == "read"


async def test_disabled_feature_makes_no_exchange_or_http_call(sharepoint, identity):
    api, requests, _, obo = sharepoint
    api.settings.sharepoint_site_url = None
    with pytest.raises(GraphFailure) as caught:
        await api.read_settings(identity)
    assert caught.value.code == "sharepoint_not_configured"
    assert not requests
    obo.acquire.assert_not_awaited()


@pytest.mark.parametrize("location", ["quicklaunch", "topnavigationbar"])
async def test_navigation_reads_root_and_explicit_child_collections(sharepoint, identity, location):
    api, requests, responses, _ = sharepoint
    responses.extend(
        [
            httpx.Response(200, json={"value": [NODE]}),
            httpx.Response(200, json={"d": {"results": []}}),
        ]
    )
    result = await api.read_navigation(identity, location=location)
    children = await api.read_navigation(identity, parent_id=17)
    assert requests[0].url.path.endswith("/navigation/" + location)
    assert requests[1].url.path.endswith("/GetNodeById(17)/children")
    assert result["data"]["nodes"] == [NODE]
    assert result["data"]["complete"] is True
    assert result["data"]["children_included"] is False
    assert children["data"]["nodes"] == []


@pytest.mark.parametrize("key", ["@odata.nextLink", "odata.nextLink", "__next"])
async def test_pagination_is_not_silently_truncated_or_followed(sharepoint, identity, key):
    api, requests, responses, _ = sharepoint
    responses.append(httpx.Response(200, json={"value": [NODE], key: "https://evil.test"}))
    with pytest.raises(GraphFailure) as caught:
        await api.read_navigation(identity)
    assert caught.value.code == "sharepoint_navigation_incomplete"
    assert len(requests) == 1


async def test_navigation_collection_limit_is_explicit(sharepoint, identity):
    api, _, responses, _ = sharepoint
    responses.append(httpx.Response(200, json={"value": [NODE] * 501}))
    with pytest.raises(GraphFailure, match="No partial result"):
        await api.read_navigation(identity)


async def test_navigation_create_update_delete_use_fixed_routes(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    responses.extend([httpx.Response(201, json=NODE), httpx.Response(204), httpx.Response(204)])
    created = await api.write_navigation(
        identity, "add", parent_id=9, title=NODE["Title"], url=NODE["Url"]
    )
    await api.write_navigation(identity, "update", node_id=17, title="Updated")
    await api.write_navigation(identity, "delete", node_id=17)
    assert created["data"]["Id"] == 17
    assert requests[0].url.path.endswith("/GetNodeById(9)/children")
    assert json.loads(requests[0].content) == {
        "Title": NODE["Title"],
        "Url": NODE["Url"],
        "IsVisible": True,
        "IsExternal": False,
    }
    assert requests[1].url.path.endswith("/GetNodeById(17)")
    assert requests[1].headers["X-HTTP-Method"] == "MERGE"
    assert json.loads(requests[1].content) == {"Title": "Updated"}
    assert requests[2].method == "DELETE"
    assert not requests[2].content


@pytest.mark.parametrize(
    "options",
    [
        {"action": "get"},
        {"action": "add", "title": "New", "url": "javascript:alert(1)"},
        {"action": "add", "title": "New", "url": "//evil.test"},
        {"action": "add", "title": "New", "url": "https://user:pass@evil.test"},
        {"action": "add", "title": "New", "url": "/sites/AIHub/%0d%0aevil"},
        {"action": "add", "title": "New", "url": "/ok", "node_id": 2},
        {"action": "add", "title": "", "url": "/ok"},
        {"action": "add", "title": "New", "url": "/ok", "location": "../lists"},
        {"action": "add", "title": "New", "url": "/ok", "parent_id": -1},
        {"action": "update", "title": "New", "node_id": True},
        {"action": "update", "node_id": 17},
        {"action": "update", "node_id": 17, "title": "New", "parent_id": 9},
        {"action": "delete", "node_id": "17)/lists"},
        {"action": "delete", "node_id": 17, "title": "New"},
    ],
)
async def test_invalid_navigation_never_sends_request(sharepoint, identity, options):
    api, requests, _, obo = sharepoint
    with pytest.raises(ValueError):
        await api.write_navigation(identity, **options)
    assert not requests
    obo.acquire.assert_not_awaited()


@pytest.mark.parametrize("verbose", [False, True])
async def test_theme_listing_and_apply_use_existing_definition(sharepoint, identity, verbose):
    api, requests, responses, _ = sharepoint
    listing = {"themePreviews": [THEME], "hideDefaultThemes": True}
    if verbose:
        listing["themePreviews"] = {"results": [THEME]}
        listing = {"d": {"GetTenantThemingOptions": listing}}
    responses.extend(
        [
            httpx.Response(200, json=listing),
            httpx.Response(200, json=listing),
            httpx.Response(200, json={"value": True}),
        ]
    )
    available = await api.read_themes(identity)
    result = await api.apply_theme(identity, "BTIG")
    assert available["data"] == {"themes": [{"name": "BTIG"}], "hide_default_themes": True}
    assert requests[0].method == "POST"  # Read-only SharePoint service operation.
    assert requests[0].url.path.endswith("/GetTenantThemingOptions")
    assert requests[2].url.path.endswith("/ApplyTheme")
    assert json.loads(requests[2].content) == THEME
    assert result["data"] == {"theme_name": "BTIG", "applied": True}


async def test_unknown_theme_cannot_be_created(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    responses.append(httpx.Response(200, json={"themePreviews": [THEME]}))
    with pytest.raises(ValueError, match="exact, unambiguous"):
        await api.apply_theme(identity, "Another theme")
    assert len(requests) == 1


async def test_home_page_checks_existing_file_and_merges_root_folder(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    page = "SitePages/AI's home.aspx"
    responses.extend(
        [
            httpx.Response(200, json={"Exists": True, "ServerRelativeUrl": "/sites/AIHub/" + page}),
            httpx.Response(204),
        ]
    )
    result = await api.set_home_page(identity, page)
    assert requests[0].url.path.endswith("/GetFileByServerRelativePath(decodedUrl=@page)")
    assert requests[0].url.params["@page"] == "'/sites/AIHub/SitePages/AI''s home.aspx'"
    assert requests[1].url.path.endswith("/web/RootFolder")
    assert requests[1].headers["X-HTTP-Method"] == "MERGE"
    assert json.loads(requests[1].content) == {"WelcomePage": page}
    assert result["data"] == {"home_page": page}


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/Home.aspx",
        "../Home.aspx",
        "/SitePages/Home.aspx",
        "SitePages/../Other.aspx",
        "SitePages/%2e%2e/Other.aspx",
        "SitePages/",
        "SitePages/X.aspx?x=y",
        "SitePages/Home.aspx#fragment",
        "SitePages/Home.txt",
        "SitePages\\Home.aspx",
        "SitePages//Home.aspx",
        "SitePages/ Home.aspx",
    ],
)
async def test_home_page_cannot_escape_configured_site(sharepoint, identity, path):
    api, requests, _, obo = sharepoint
    with pytest.raises(ValueError):
        await api.set_home_page(identity, path)
    assert not requests
    obo.acquire.assert_not_awaited()


async def test_missing_home_page_stops_before_mutation(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    responses.append(httpx.Response(200, json={"Exists": False}))
    with pytest.raises(ValueError, match="does not exist"):
        await api.set_home_page(identity, "SitePages/Missing.aspx")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(403, text="secret upstream response"), "sharepoint_access_denied"),
        (httpx.Response(500, text="secret upstream response"), "sharepoint_write_outcome_unknown"),
        (
            httpx.Response(302, headers={"Location": "https://evil.test"}),
            "sharepoint_write_outcome_unknown",
        ),
        (httpx.Response(201, json={}), "sharepoint_write_outcome_unknown"),
        (httpx.Response(201, text="not JSON"), "sharepoint_write_outcome_unknown"),
        (
            httpx.Response(201, json={"error": "secret upstream response"}),
            "sharepoint_write_outcome_unknown",
        ),
        (httpx.ReadTimeout("secret upstream response"), "sharepoint_write_outcome_unknown"),
    ],
)
async def test_failed_writes_are_sanitized_never_retried(sharepoint, identity, response, code):
    api, requests, responses, _ = sharepoint
    responses.append(response)
    with pytest.raises(GraphFailure) as caught:
        await api.write_navigation(identity, "add", title="Guides", url=NODE["Url"])
    assert caught.value.code == code
    assert "secret" not in str(caught.value)
    assert len(requests) == 1


async def test_401_invalidates_only_sharepoint_obo_cache(sharepoint, identity):
    api, _, responses, obo = sharepoint
    responses.append(httpx.Response(401))
    with pytest.raises(GraphFailure) as caught:
        await api.read_settings(identity)
    assert caught.value.code == "sharepoint_authentication_required"
    obo.invalidate.assert_called_once_with(identity.token)


async def test_throttling_preserves_delay(sharepoint, identity):
    api, _, responses, _ = sharepoint
    responses.append(httpx.Response(429, headers={"Retry-After": "15"}))
    with pytest.raises(GraphFailure) as caught:
        await api.read_settings(identity)
    assert caught.value.retry_after_seconds == 15


async def test_request_limit_checked_before_token_or_http(sharepoint, identity):
    api, requests, _, obo = sharepoint
    api.settings.max_request_body_bytes = 16
    with pytest.raises(ValueError, match="request body limit"):
        await api.write_navigation(identity, "add", title="Guides", url=NODE["Url"])
    assert not requests
    obo.acquire.assert_not_awaited()


async def test_response_size_bound(sharepoint, identity, monkeypatch):
    api, _, responses, _ = sharepoint
    monkeypatch.setattr("ms_graph_mcp.sharepoint.MAX_RESPONSE_BYTES", 32)
    responses.append(httpx.Response(200, json={"Id": "web-id", "Title": "X" * 100}))
    with pytest.raises(GraphFailure) as caught:
        await api.read_settings(identity)
    assert caught.value.code == "sharepoint_response_too_large"


def test_graph_obo_cannot_be_used_for_sharepoint(sharepoint):
    api, _, _, obo = sharepoint
    obo.resource_origin = "https://graph.microsoft.com"
    with pytest.raises(ValueError, match="resource origin"):
        SharePointClient(api.http, obo, api.settings)


async def test_external_navigation_links_have_external_flag(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    responses.append(httpx.Response(201, json={**NODE, "Url": "https://example.org/guide%20one"}))
    await api.write_navigation(
        identity, "add", title="External", url="https://example.org/guide%20one"
    )
    assert json.loads(requests[0].content)["IsExternal"] is True


@pytest.mark.parametrize("action", ["add", "update"])
@pytest.mark.parametrize(
    "site,url,expected_url,external",
    [
        (
            SITE,
            SITE + "/SitePages/Guide.aspx?mode=read#start",
            "/sites/AIHub/SitePages/Guide.aspx?mode=read#start",
            False,
        ),
        (SITE, SITE, "/sites/AIHub", False),
        (
            SITE,
            "https://contoso.sharepoint.com/sites/AIHubOther/SitePages/Guide.aspx",
            "https://contoso.sharepoint.com/sites/AIHubOther/SitePages/Guide.aspx",
            True,
        ),
        (
            SITE,
            "https://example.org/Guide.aspx?mode=read#start",
            "https://example.org/Guide.aspx?mode=read#start",
            True,
        ),
        (
            "https://contoso.sharepoint.com",
            "https://contoso.sharepoint.com/SitePages/Home.aspx?mode=read#start",
            "/SitePages/Home.aspx?mode=read#start",
            False,
        ),
        (
            "https://contoso.sharepoint.com",
            "https://contoso.sharepoint.com?mode=read#start",
            "/?mode=read#start",
            False,
        ),
        (
            "https://contoso.sharepoint.com",
            "https://example.org/SitePages/Home.aspx",
            "https://example.org/SitePages/Home.aspx",
            True,
        ),
    ],
)
async def test_navigation_normalizes_only_absolute_same_site_links(
    sharepoint, identity, action, site, url, expected_url, external
):
    api, requests, responses, _ = sharepoint
    api.settings.sharepoint_site_url = site
    responses.append(httpx.Response(201, json=NODE) if action == "add" else httpx.Response(204))
    await api.write_navigation(
        identity, action, node_id=17 if action == "update" else None, title="Guide", url=url
    )
    body = json.loads(requests[0].content)
    assert body["Url"] == expected_url
    assert body["IsExternal"] is external


async def test_theme_payload_is_rejected_before_write_if_malformed(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    responses.append(
        httpx.Response(200, json={"themePreviews": [{"name": "BTIG", "themeJson": "not JSON"}]})
    )
    with pytest.raises(GraphFailure) as caught:
        await api.apply_theme(identity, "BTIG")
    assert caught.value.code == "invalid_sharepoint_response"
    assert len(requests) == 1


async def test_invalid_theme_success_is_not_reported_as_applied(sharepoint, identity):
    api, requests, responses, _ = sharepoint
    responses.extend(
        [
            httpx.Response(200, json={"themePreviews": [THEME]}),
            httpx.Response(200, json={"value": False}),
        ]
    )
    with pytest.raises(GraphFailure) as caught:
        await api.apply_theme(identity, "BTIG")
    assert caught.value.code == "sharepoint_write_outcome_unknown"
    assert len(requests) == 2


@pytest.mark.parametrize("null_key", ["odata.null", "@odata.null"])
@pytest.mark.parametrize("verbose", [False, True])
async def test_missing_navigation_update_is_not_reported_as_completed(
    sharepoint, identity, null_key, verbose
):
    api, requests, responses, _ = sharepoint
    data = {null_key: True}
    responses.append(httpx.Response(200, json={"d": data} if verbose else data))
    with pytest.raises(GraphFailure) as caught:
        await api.write_navigation(identity, "update", node_id=17, title="Updated")
    assert caught.value.code == "sharepoint_not_found"
    assert caught.value.status == 404
    assert len(requests) == 1


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("site", [SITE, "https://contoso.sharepoint.com"])
async def test_apply_theme_accepts_generated_site_catalog_path(sharepoint, identity, verbose, site):
    api, requests, responses, _ = sharepoint
    api.settings.sharepoint_site_url = site
    prefix = "/sites/AIHub" if site == SITE else ""
    path = prefix + "/_catalogs/theme/Themed/BFCFD35C"
    response = {"d": {"ApplyTheme": path}} if verbose else {"value": path}
    responses.extend(
        [
            httpx.Response(200, json={"themePreviews": [THEME]}),
            httpx.Response(200, json=response),
        ]
    )
    result = await api.apply_theme(identity, "BTIG")
    assert result["data"] == {"theme_name": "BTIG", "applied": True, "theme_path": path}
    assert len(requests) == 2


@pytest.mark.parametrize(
    "value",
    [
        "",
        "true",
        "https://evil.test/theme",
        "/sites/Other/_catalogs/theme/Themed/123",
        "/sites/AIHub/_catalogs/theme/Themed/../123",
        "/sites/AIHub/_catalogs/theme/Themed/",
        "/sites/AIHub/_catalogs/theme/Themed/123?secret=value",
        {"applied": True},
        None,
    ],
)
async def test_apply_theme_rejects_unsupported_success_values(sharepoint, identity, value):
    api, requests, responses, _ = sharepoint
    responses.extend(
        [
            httpx.Response(200, json={"themePreviews": [THEME]}),
            httpx.Response(200, json={"value": value}),
        ]
    )
    with pytest.raises(GraphFailure) as caught:
        await api.apply_theme(identity, "BTIG")
    assert caught.value.code == "sharepoint_write_outcome_unknown"
    assert len(requests) == 2
