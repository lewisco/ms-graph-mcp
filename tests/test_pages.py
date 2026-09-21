import json
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from ms_graph_mcp.auth import DelegatedAccessToken
from ms_graph_mcp.catalog import describe, resolve
from ms_graph_mcp.gateway import Gateway
from ms_graph_mcp.graph import GraphClient, GraphFailure
from ms_graph_mcp.pages import PAGE_GUIDANCE, validate_page_query

SITE = "/sites/contoso.sharepoint.com,site-id,web-id"
PAGE = SITE + "/pages/page-id/microsoft.graph.sitePage"
HORIZONTAL = PAGE + "/canvasLayout/horizontalSections"
VERTICAL = PAGE + "/canvasLayout/verticalSection"
COLUMN = HORIZONTAL + "/1/columns/1"


@pytest.fixture
def page_identity():
    return DelegatedAccessToken(
        token="mcp-token",
        graph_token=SecretStr("graph-token"),
        client_id="client",
        subject="author",
        scopes=["access_as_user"],
        claims={"tid": "tenant"},
    )


@pytest.fixture
async def page_gateway(settings):
    requests = []
    responses = []

    def handler(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield (
            Gateway(GraphClient(client, Mock()), settings, storage_http=client),
            requests,
            responses,
        )


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", SITE + "/pages/microsoft.graph.sitePage"),
        ("POST", SITE + "/pages"),
        ("GET", PAGE),
        ("PATCH", PAGE),
        ("DELETE", SITE + "/pages/page-id"),
        ("POST", PAGE + "/publish"),
        ("POST", HORIZONTAL),
        ("PATCH", HORIZONTAL + "/1"),
        ("DELETE", HORIZONTAL + "/1"),
        ("PUT", VERTICAL),
        ("PATCH", VERTICAL),
        ("DELETE", VERTICAL),
        ("GET", HORIZONTAL + "/1/columns"),
        ("GET", COLUMN),
        ("POST", COLUMN + "/webparts"),
        ("GET", PAGE + "/webparts"),
        ("PATCH", PAGE + "/webParts/webpart-id"),
        ("DELETE", COLUMN + "/webparts/0"),
        ("POST", VERTICAL + "/webparts"),
        ("GET", VERTICAL + "/webparts/0/getPositionOfWebPart"),
    ],
)
def test_page_routes_keep_write_tools_separate(method, path):
    read_only = method == "GET"
    assert resolve(method, path, read_only) == path
    with pytest.raises(ValueError):
        resolve(method, path, not read_only)


@pytest.mark.parametrize(
    "method,path",
    [
        ("PATCH", SITE + "/pages/page-id"),
        ("POST", SITE + "/pages/microsoft.graph.sitePage"),
        ("DELETE", SITE + "/pages/microsoft.graph.sitePage"),
        ("POST", PAGE + "/webparts"),
        ("POST", VERTICAL),
        ("PUT", HORIZONTAL),
        ("POST", HORIZONTAL + "/1/columns"),
        ("PATCH", COLUMN),
        ("DELETE", COLUMN),
    ],
)
def test_page_catalog_does_not_invent_crud(method, path):
    with pytest.raises(ValueError, match="catalog"):
        resolve(method, path, False)


@pytest.mark.parametrize("relative", ["sites/AIHub", "teams/AI%20Hub", "sites/AIHub/guides"])
def test_site_discovery_by_path(relative):
    path = "/sites/contoso.sharepoint.com:/" + relative
    assert resolve("GET", path, True) == path.replace("%20", " ")
    with pytest.raises(ValueError):
        resolve("PATCH", path, False)


@pytest.mark.parametrize(
    "path",
    [
        "/sites/contoso.sharepoint.com:/sites/AIHub:/permissions",
        "/sites/contoso.sharepoint.com:/sites/AIHub%3a/permissions",
        "/sites/contoso.sharepoint.com:/sites/../applications",
        "/sites/contoso.sharepoint.com:/sites/%2e%2e/applications",
        "/sites/contoso.sharepoint.com:/sites/%252e%252e/applications",
        "/sites/contoso.sharepoint.com:/sites/AIHub?$expand=permissions",
        "/sites/contoso.sharepoint.com:/sites/AIHub%3f$expand=permissions",
        "/sites/contoso.sharepoint.com:/sites/AIHub/$batch",
        "/sites/contoso.sharepoint.com:/sites//AIHub",
        "/sites/contoso.sharepoint.com:/sites/AIHub#fragment",
        "/sites/contoso.sharepoint.com:/sites/AIHub\\other",
    ],
)
def test_site_path_discovery_cannot_escape_route_catalog(path):
    with pytest.raises(ValueError):
        resolve("GET", path, True)


def test_page_discovery_supplies_executable_examples_and_guidance():
    discovery = describe(service="sites", query="pages", limit=3)
    assert len(discovery["operations"]) == 3
    assert discovery["next_offset"] == 3
    guidance = discovery["sharepoint_pages"]
    assert "Quick Links" in guidance["supported_standard_webparts"]
    assert "Hero" not in guidance["supported_standard_webparts"]
    assert "sharepoint_pages" not in describe(service="onenote")
    assert "sharepoint_pages" in describe(path=PAGE + "/publish")
    for example in guidance["examples"].values():
        path = (
            example["path"]
            .replace("{site}", "site-id")
            .replace("{page}", "page-id")
            .replace("{webpart}", "webpart-id")
        )
        assert resolve(example["method"], path, example["method"] == "GET") == path


async def test_page_draft_update_publish_and_verify(page_gateway, page_identity):
    api, requests, responses = page_gateway
    body = deepcopy(PAGE_GUIDANCE["examples"]["create_draft"]["body"])
    draft = {
        **body,
        "id": "page-id",
        "webUrl": "https://contoso.sharepoint.com/sites/AIHub/SitePages/getting-started.aspx",
        "publishingState": {"level": "checkout", "versionId": "0.1"},
    }
    responses.append(httpx.Response(201, json=draft))
    created = await api.request(page_identity, "POST", SITE + "/pages", body=body, read_only=False)
    assert created["status"] == 201 and created["data"]["id"] == "page-id"
    assert json.loads(requests[-1].content) == body

    responses.append(httpx.Response(200, json=draft))
    current = await api.request(page_identity, "GET", PAGE, query={"$expand": "canvasLayout"})
    assert requests[-1].url.params["$expand"] == "canvasLayout"
    layout = deepcopy(current["data"]["canvasLayout"])
    webpart = layout["horizontalSections"][0]["columns"][0]["webparts"][0]
    webpart["innerHtml"] = "<h2>Updated guide</h2><p>Follow these steps.</p>"
    patch = {"@odata.type": "#microsoft.graph.sitePage", "canvasLayout": layout}
    responses.append(httpx.Response(200, json={**draft, "canvasLayout": layout}))
    updated = await api.request(page_identity, "PATCH", PAGE, body=patch, read_only=False)
    assert updated["data"]["id"] == created["data"]["id"]
    assert json.loads(requests[-1].content) == patch
    assert (
        webpart["id"]
        == body["canvasLayout"]["horizontalSections"][0]["columns"][0]["webparts"][0]["id"]
    )

    responses.append(httpx.Response(204))
    published = await api.request(page_identity, "POST", PAGE + "/publish", read_only=False)
    assert published["status"] == 204 and published["data"] is None
    assert requests[-1].content == b""

    live = {**draft, "publishingState": {"level": "published", "versionId": "1.0"}}
    responses.append(httpx.Response(200, json=live))
    verified = await api.request(
        page_identity, "GET", PAGE, query={"$select": "id,webUrl,publishingState"}
    )
    assert verified["data"]["publishingState"]["level"] == "published"
    assert verified["data"]["webUrl"] == created["data"]["webUrl"]
    assert len(requests) == 5


@pytest.mark.parametrize("path", [COLUMN + "/webparts", VERTICAL + "/webparts"])
async def test_targeted_webpart_insert_preserves_position(page_gateway, page_identity, path):
    api, requests, responses = page_gateway
    body = {"@odata.type": "#microsoft.graph.textWebPart", "innerHtml": "<p>New section</p>"}
    responses.append(httpx.Response(201, json={**body, "id": "webpart-id"}))
    result = await api.request(
        page_identity, "POST", path, body=body, query={"index": "0"}, read_only=False
    )
    assert result["data"]["id"] == "webpart-id"
    assert requests[-1].url.params["index"] == "0"
    assert json.loads(requests[-1].content) == body


@pytest.mark.parametrize("value", ["-1", "1.5", "", " 1", "１", 0, None])
def test_invalid_insertion_index_rejected(value):
    with pytest.raises(ValueError, match="index"):
        validate_page_query("POST", COLUMN + "/webparts", {"index": value})


@pytest.mark.parametrize(
    "method,path",
    [("POST", SITE + "/pages"), ("GET", COLUMN + "/webparts"), ("POST", "/me/messages")],
)
async def test_insertion_index_is_not_a_general_query_escape(
    page_gateway, page_identity, method, path
):
    api, requests, _ = page_gateway
    with pytest.raises(ValueError, match="index"):
        await api.request(
            page_identity, method, path, query={"index": "0"}, read_only=method == "GET"
        )
    assert not requests


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ReadTimeout("connection lost")])
async def test_uncertain_creation_is_not_replayed(page_gateway, page_identity, failure):
    api, requests, responses = page_gateway
    responses.append(failure)
    with pytest.raises(GraphFailure) as exc:
        await api.request(
            page_identity,
            "POST",
            SITE + "/pages",
            body=PAGE_GUIDANCE["examples"]["create_draft"]["body"],
            read_only=False,
        )
    assert exc.value.code == "write_outcome_unknown"
    assert len(requests) == 1
    responses.append(httpx.Response(200, json={"value": [{"id": "page-id"}]}))
    reconciled = await api.request(
        page_identity,
        "GET",
        SITE + "/pages/microsoft.graph.sitePage",
        query={"$filter": "name eq 'getting-started.aspx'"},
    )
    assert reconciled["data"]["value"][0]["id"] == "page-id"
    assert [request.method for request in requests] == ["POST", "GET"]


async def test_publish_action_does_not_claim_approval_completion(page_gateway, page_identity):
    api, _, responses = page_gateway
    responses.extend(
        [
            httpx.Response(204),
            httpx.Response(
                200,
                json={
                    "id": "page-id",
                    "publishingState": {"level": "checkout", "versionId": "0.2"},
                },
            ),
        ]
    )
    await api.request(page_identity, "POST", PAGE + "/publish", read_only=False)
    actual = await api.request(page_identity, "GET", PAGE)
    assert actual["data"]["publishingState"]["level"] == "checkout"


async def test_page_conflict_preserves_existing_error_contract(page_gateway, page_identity):
    api, requests, responses = page_gateway
    responses.append(httpx.Response(412, headers={"request-id": "conflict-id"}))
    with pytest.raises(GraphFailure) as exc:
        await api.request(
            page_identity,
            "DELETE",
            SITE + "/pages/page-id",
            headers={"If-Match": '"old"'},
            read_only=False,
        )
    assert exc.value.code == "graph_precondition_failed"
    assert requests[-1].headers["if-match"] == '"old"'
    assert len(requests) == 1
