import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from ms_graph_mcp.auth import DelegatedAccessToken
from ms_graph_mcp.catalog import OPERATIONS, describe, resolve
from ms_graph_mcp.gateway import Gateway
from ms_graph_mcp.graph import GraphClient, GraphFailure


@pytest.fixture
def identity():
    return DelegatedAccessToken(
        token="mcp-token",
        graph_token=SecretStr("graph-token"),
        client_id="client",
        subject="user",
        scopes=["access_as_user"],
        claims={"tid": "tenant"},
    )


@pytest.fixture
def gateway(settings):
    requests = []
    responses = [httpx.Response(200, json={"value": [{"id": "one"}]})]

    def handler(request):
        requests.append(request)
        return responses[0]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Gateway(GraphClient(client, Mock()), settings, storage_http=storage), requests, responses


@pytest.mark.parametrize(
    "method,path,read",
    [
        ("GET", "/me/messages", True),
        ("POST", "/me/sendMail", False),
        ("GET", "/users/shared@example.com/mailFolders/inbox/messages", True),
        ("POST", "/me/events", False),
        ("POST", "/me/calendar/getSchedule", True),
        ("PATCH", "/me/contacts/contact-id", False),
        ("GET", "/me/mailboxSettings", True),
        ("GET", "/teams/team/channels/channel/messages", True),
        ("POST", "/chats/chat/messages", False),
        ("POST", "/me/onlineMeetings", False),
        ("GET", "/me/onlineMeetings/meeting/transcripts/transcript/content", True),
        ("GET", "/copilot/users/user/onlineMeetings/meeting/aiInsights", True),
        ("PATCH", "/planner/tasks/task/details", False),
        ("POST", "/me/todo/lists/list/tasks", False),
        ("GET", "/drives/drive/root/children", True),
        ("POST", "/sites/site/lists/list/items", False),
        ("POST", "/drives/drive/items/item/workbook/createSession", False),
        (
            "PATCH",
            "/drives/drive/items/item/workbook/worksheets/sheet/range(address='A1:B2')",
            False,
        ),
        ("GET", "/users", True),
        ("POST", "/search/query", True),
    ],
)
def test_catalog_services(method, path, read):
    assert resolve(method, path, read) == path


@pytest.mark.parametrize(
    "method,path,read",
    [
        ("POST", "/me/sendMail", True),
        ("POST", "/search/query", False),
        ("DELETE", "/users/user", False),
        ("PATCH", "/groups/group", False),
        ("GET", "https://evil.test/me", True),
        ("GET", "//evil.test/me", True),
        ("GET", "/me/../applications", True),
        ("GET", "/me/%2e%2e/applications", True),
        ("GET", "/me/%252e%252e/applications", True),
        ("GET", "/me/messages?x=y", True),
        ("GET", "/me/messages%3fx=y", True),
        ("GET", "/me\\messages", True),
        ("POST", "/$batch", False),
        ("GET", "/beta/me/messages", True),
        ("POST", "/teams/team/members", False),
        ("GET", "/applications", True),
    ],
)
def test_unsafe_or_misclassified_operations_rejected(method, path, read):
    with pytest.raises(ValueError):
        resolve(method, path, read)


def test_discovery_is_paged_and_honest():
    first = describe(service="mail", limit=5)
    assert len(first["operations"]) == 5 and first["next_offset"] == 5
    assert first["availability"] == "supported_unverified"
    assert describe(path="/me/sendMail")["operations"][0]["read_only"] is False
    assert len({(o.method, o.path) for o in OPERATIONS}) == len(OPERATIONS)


async def test_write_accepted_and_no_token_in_result(gateway, identity):
    api, requests, responses = gateway
    responses[0] = httpx.Response(202)
    result = await api.request(
        identity, "POST", "/me/sendMail", body={"message": {"subject": "test"}}, read_only=False
    )
    assert result["outcome"] == "accepted" and result["status"] == 202
    assert requests[0].headers["authorization"] == "Bearer graph-token"
    assert json.loads(requests[0].content)["message"]["subject"] == "test"
    assert "graph-token" not in json.dumps(result) and "mcp-token" not in json.dumps(result)


async def test_query_and_selected_headers_preserved(gateway, identity):
    api, requests, _ = gateway
    await api.request(
        identity,
        "GET",
        "/me/messages",
        query={"$filter": "isRead eq false", "$top": "5"},
        headers={"Prefer": 'outlook.body-content-type="text"'},
    )
    assert requests[0].url.params["$filter"] == "isRead eq false"
    assert requests[0].headers["prefer"] == 'outlook.body-content-type="text"'


@pytest.mark.parametrize(
    "header", ["Authorization", "Host", "Cookie", "Location", "Proxy-Authorization"]
)
async def test_untrusted_headers_never_sent(gateway, identity, header):
    api, requests, _ = gateway
    with pytest.raises(ValueError):
        await api.request(identity, "GET", "/me/messages", headers={header: "bad"})
    assert not requests


async def test_continuation_owner_replica_and_origin(gateway, identity, settings):
    api, requests, responses = gateway
    url = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque%2Btoken"
    responses[0] = httpx.Response(
        200,
        json={
            "value": [{"id": "one", "@microsoft.graph.downloadUrl": "secret"}],
            "@odata.nextLink": url,
        },
    )
    result = await api.request(identity, "GET", "/me/messages")
    handle = result["page"]["continuation"]
    assert "opaque" not in handle
    assert "@odata.nextLink" not in result["data"]
    assert "@microsoft.graph.downloadUrl" not in result["data"]["value"][0]
    replica = Gateway(api.graph, settings, storage_http=api.storage_http)
    responses[0] = httpx.Response(200, json={"value": [{"id": "two"}]})
    await replica.continue_page(identity, handle)
    assert str(requests[-1].url) == url
    other = identity.model_copy(update={"subject": "other"})
    with pytest.raises(ValueError):
        await replica.continue_page(other, handle)
    with pytest.raises(ValueError):
        await replica.continue_page(identity, handle[:-5] + "xxxxx")
    bad = api.seal(identity, "page", {"url": "https://evil.test/v1.0/me/messages", "headers": {}})
    with pytest.raises(ValueError):
        await api.continue_page(identity, bad)
    assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 409, 412, 429, 500, 302])
async def test_errors_do_not_retry_or_leak_response(gateway, identity, status):
    api, requests, responses = gateway
    responses[0] = httpx.Response(
        status,
        text="private-upstream-data",
        headers={"Location": "https://evil.test/", "Retry-After": "10"},
    )
    with pytest.raises(GraphFailure) as exc:
        await api.request(identity, "POST", "/me/sendMail", body={}, read_only=False)
    assert len(requests) == 1 and "private-upstream-data" not in str(exc.value)
    if status == 429:
        assert exc.value.retry_after_seconds == 10


async def test_unknown_write_outcome_is_not_retried(settings, identity):
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout("private", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        api = Gateway(GraphClient(client, Mock()), settings, storage_http=client)
        with pytest.raises(GraphFailure, match="outcome is unknown") as exc:
            await api.request(identity, "POST", "/me/sendMail", body={}, read_only=False)
        assert exc.value.code == "write_outcome_unknown" and len(requests) == 1


async def test_download_descriptor_is_explicit_and_owner_bound(gateway, identity):
    api, _, responses = gateway
    responses[0] = httpx.Response(
        200,
        json={
            "id": "item",
            "name": "file.docx",
            "file": {"mimeType": "docx"},
            "size": 100,
            "eTag": "v1",
            "@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/file?token=secret",
        },
    )
    result = await api.prepare(identity, "download", "/drives/drive/items/item")
    assert result["url"].endswith("token=secret") and result["method"] == "GET"
    state = api.open(identity, "transfer", result["transfer"])
    assert state["item_id"] == "item" and "url" not in state


async def test_upload_copy_and_explicit_overwrite(gateway, identity):
    api, requests, responses = gateway
    responses[0] = httpx.Response(
        200,
        json={
            "uploadUrl": "https://tenant.sharepoint.com/upload?token=secret",
            "expirationDateTime": "2027-01-01T00:00:00Z",
        },
    )
    result = await api.prepare(identity, "upload", "/drives/drive/root", "new.docx", 250000000)
    assert result["chunk_size"] % (320 * 1024) == 0
    assert json.loads(requests[0].content)["item"]["@microsoft.graph.conflictBehavior"] == "rename"
    with pytest.raises(ValueError):
        await api.prepare(identity, "upload", "/drives/drive/items/item", size=100)
    await api.prepare(identity, "upload", "/drives/drive/items/item", size=100, etag="v1")
    assert requests[-1].headers["if-match"] == "v1"
    assert len(requests) == 2


async def test_storage_requests_never_include_graph_bearer(gateway, identity):
    api, requests, responses = gateway
    state = {"direction": "upload", "url": "https://tenant.sharepoint.com/upload?token=secret"}
    handle = api.seal(identity, "transfer", state)
    responses[0] = httpx.Response(200, json={"nextExpectedRanges": ["0-"]})
    assert (await api.transfer_status(identity, handle))["status"] == "upload_session_active"
    assert "authorization" not in requests[0].headers
    responses[0] = httpx.Response(204)
    assert (await api.transfer_manage(identity, handle, "cancel"))["status"] == "cancelled"
    assert requests[-1].method == "DELETE" and "authorization" not in requests[-1].headers


@pytest.mark.parametrize(
    "url",
    [
        "http://tenant.sharepoint.com/a",
        "https://evil.test/a",
        "https://sharepoint.com.evil.test/a",
        "https://user:password@tenant.sharepoint.com/a",
        "https://127.0.0.1/a",
    ],
)
def test_transfer_url_host_boundary(url):
    with pytest.raises(ValueError):
        Gateway.storage_link(url)


async def test_success_with_unreadable_write_result_is_uncertain(gateway, identity):
    api, requests, responses = gateway
    responses[0] = httpx.Response(
        201, content=b"not-json", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(GraphFailure) as exc:
        await api.request(identity, "POST", "/me/messages", body={}, read_only=False)
    assert exc.value.code == "write_outcome_unknown" and len(requests) == 1


async def test_expanded_collection_keeps_its_continuation(gateway, identity):
    api, _, responses = gateway
    responses[0] = httpx.Response(
        200,
        json={
            "value": [
                {
                    "id": "message",
                    "attachments@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages/message/attachments?$skip=1",
                }
            ]
        },
    )
    result = await api.request(identity, "GET", "/me/messages")
    handle = result["data"]["value"][0]["attachmentsmcp_next"]
    state = api.open(identity, "page", handle)
    assert "/attachments?$skip=1" in state["url"]


async def test_expired_and_wrong_purpose_handles_fail(gateway, identity):
    import time

    api, requests, _ = gateway
    raw = json.dumps({"owner": api.owner(identity), "kind": "page", "data": {}}).encode()
    handle = api.fernet.encrypt_at_time(raw, int(time.time()) - 3601).decode()
    with pytest.raises(ValueError):
        await api.continue_page(identity, handle)
    with pytest.raises(ValueError):
        await api.continue_page(identity, api.seal(identity, "transfer", {}))
    assert not requests
