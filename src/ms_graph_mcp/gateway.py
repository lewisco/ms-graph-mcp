"""Delegated Graph execution, owner-bound continuations and native drive transfers."""

import base64
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken

from ms_graph_mcp.catalog import canonical_path, resolve
from ms_graph_mcp.graph import GraphClient, GraphFailure
from ms_graph_mcp.tls import create_ssl_context

ORIGIN = "https://graph.microsoft.com"
HEADERS = {"prefer", "consistencylevel", "if-match", "if-none-match", "workbook-session-id"}
QUERY = {
    "$select",
    "$filter",
    "$search",
    "$orderby",
    "$expand",
    "$top",
    "$skip",
    "$count",
    "$skiptoken",
    "$deltatoken",
    "startDateTime",
    "endDateTime",
    "$format",
    "search",
}
MAX_RESPONSE = 2_000_000
MAX_TRANSFER = 250_000_000


class Gateway:
    def __init__(self, graph: GraphClient, settings, *, storage_http=None):
        self.graph = graph
        self.storage_http = storage_http or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            verify=create_ssl_context(settings),
        )
        # Shared settings make handles usable across replicas. Rotation expires them.
        key = hashlib.sha256(
            b"ms-graph-mcp-handles-v1\0"
            + settings.client_secret.get_secret_value().encode()
            + str(settings.tenant_id).encode()
            + str(settings.client_id).encode()
        ).digest()
        self.fernet = Fernet(base64.urlsafe_b64encode(key))

    @staticmethod
    def owner(identity):
        return [identity.claims.get("tid"), identity.subject, identity.client_id]

    def seal(self, identity, kind, data):
        handle = self.fernet.encrypt(
            json.dumps({"owner": self.owner(identity), "kind": kind, "data": data}).encode()
        ).decode()
        if len(handle) > 24000:
            raise ValueError("Continuation metadata is too large; narrow the query or headers.")
        return handle

    def open(self, identity, kind, handle):
        try:
            if len(handle) > 24000:
                raise ValueError("Oversized handle")
            value = json.loads(self.fernet.decrypt(handle.encode(), ttl=3600))
            if value["owner"] != self.owner(identity) or value["kind"] != kind:
                raise ValueError("Wrong handle owner or purpose")
            return value["data"]
        except InvalidToken, ValueError, KeyError, TypeError:
            raise ValueError("Handle is invalid, expired or belongs to another user.") from None

    @staticmethod
    def graph_link(url):
        if not isinstance(url, str) or len(url) > 12000:
            raise ValueError("Invalid or oversized Graph continuation.")
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.netloc != "graph.microsoft.com"
            or p.fragment
            or not p.path.startswith("/v1.0/")
        ):
            raise ValueError("Graph returned an unsupported continuation origin or API version.")
        resolve("GET", p.path[len("/v1.0") :], True)
        return url

    @staticmethod
    def storage_link(url):
        p = urlsplit(url)
        hosts = (
            ".sharepoint.com",
            ".sharepoint-df.com",
            ".1drv.com",
            ".storage.live.com",
            ".blob.core.windows.net",
        )
        if (
            p.scheme != "https"
            or p.username
            or p.password
            or p.fragment
            or p.port not in (None, 443)
            or not (
                p.hostname in ("onedrive.live.com", "storage.live.com")
                or any((p.hostname or "").endswith(h) for h in hosts)
            )
        ):
            raise ValueError("Unsupported Microsoft storage URL; no request was sent.")
        return url

    async def request(
        self,
        identity,
        method,
        path,
        query=None,
        body=None,
        headers=None,
        *,
        read_only=True,
        transfer=False,
        url=None,
    ):
        path = resolve(method, path, read_only)
        if path.endswith("/createUploadSession") and not transfer:
            raise ValueError("Use graph_prepare_transfer to create and track an upload session.")
        headers = headers or {}
        if sum(len(k) + len(v) for k, v in headers.items()) > 4096:
            raise ValueError("Selected request headers exceed 4096 characters.")
        if any(k.lower() not in HEADERS or "\r" in v or "\n" in v for k, v in headers.items()):
            raise ValueError(
                "Only Prefer, ConsistencyLevel, conditional and workbook "
                "session headers are allowed."
            )
        query = query or {}
        if any(k not in QUERY for k in query):
            raise ValueError(
                "Unsupported query parameter; use graph_describe and Graph query options."
            )
        if method in ("GET", "DELETE") and body is not None:
            raise ValueError("This method does not accept a request body.")
        if body is not None and len(json.dumps(body).encode()) > 60000:
            raise ValueError("JSON request too large. Use native file transfers for file content.")
        request_id = str(uuid4())
        request_headers = {
            **headers,
            "Authorization": f"Bearer {identity.graph_token.get_secret_value()}",
            "Accept": "application/json",
            "client-request-id": request_id,
            "return-client-request-id": "true",
        }
        if path.endswith("/content") or path.endswith("/$value"):
            request_headers["Accept"] = "*/*"
        target = self.graph_link(url) if url else ORIGIN + "/v1.0" + path
        try:
            async with self.graph.http.stream(
                method,
                target,
                params=query if not url else None,
                json=body,
                headers=request_headers,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                rid = response.headers.get("request-id", request_id)
                if status == 401:
                    self.graph.obo.invalidate(identity.token)
                if not 200 <= status < 300:
                    retry = response.headers.get("retry-after", "")
                    code = {
                        401: "graph_authentication_required",
                        403: "graph_access_denied",
                        404: "graph_not_found",
                        409: "graph_conflict",
                        412: "graph_precondition_failed",
                        429: "graph_throttled",
                    }.get(status, "graph_upstream_error")
                    message = {
                        403: (
                            "Graph denied access. Check delegated consent and "
                            "user/resource rights; this does not prove a missing "
                            "license."
                        ),
                        412: (
                            "The resource changed. Read its current eTag before "
                            "deciding whether to retry."
                        ),
                        429: "Graph throttled the request; wait before retrying.",
                    }.get(
                        status,
                        "Graph rejected the request. Check the operation and its parameters.",
                    )
                    if not read_only and status >= 500:
                        code = "write_outcome_unknown"
                        message = (
                            "Graph returned a server error. Inspect the "
                            "destination before retrying."
                        )
                    raise GraphFailure(
                        code,
                        message,
                        status,
                        rid,
                        int(retry)
                        if retry.isascii() and retry.isdecimal() and len(retry) < 10
                        else None,
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise GraphFailure(
                            "response_too_large",
                            (
                                "Response exceeds the 2 MB inline ceiling. "
                                "Request fewer fields/items or use a native "
                                "drive transfer. No partial result was returned."
                            ),
                            413,
                            rid,
                        )
                content_type = response.headers.get("content-type", "")
                if not raw:
                    data = None
                elif "json" in content_type:
                    try:
                        data = json.loads(raw)
                    except ValueError, UnicodeError:
                        raise GraphFailure(
                            "invalid_graph_response", "Graph returned invalid JSON.", 502, rid
                        ) from None
                    if isinstance(data, dict) and "error" in data:
                        raise GraphFailure(
                            "invalid_graph_response",
                            "Graph returned an error-shaped response.",
                            502,
                            rid,
                        )
                elif content_type.startswith("text/") or path.endswith("/$value"):
                    try:
                        data = {"text": raw.decode("utf-8"), "content_type": content_type}
                    except UnicodeError:
                        raise GraphFailure(
                            "binary_transfer_required",
                            (
                                "Binary content needs a drive transfer; inline "
                                "binary is not supported."
                            ),
                            415,
                            rid,
                        ) from None
                else:
                    raise GraphFailure(
                        "binary_transfer_required",
                        "Use graph_prepare_transfer for drive content.",
                        415,
                        rid,
                    )
                envelope = {
                    "status": status,
                    "data": data,
                    "delivery": "inline",
                    "request_id": rid,
                    "content": {"complete": True},
                    "outcome": "accepted" if status == 202 else "completed",
                }
                if not read_only:
                    envelope["operation_id"] = request_id
                    envelope["reconciliation"] = (
                        "Do not replay non-idempotent writes after uncertain failures."
                    )
                next_link = data.get("@odata.nextLink") if isinstance(data, dict) else None
                delta_link = data.get("@odata.deltaLink") if isinstance(data, dict) else None
                if next_link:
                    next_link = self.graph_link(next_link)
                    envelope["page"] = {
                        "has_more": True,
                        "continuation": self.seal(
                            identity, "page", {"url": next_link, "headers": headers}
                        ),
                    }
                else:
                    envelope["page"] = {"has_more": False}
                if delta_link:
                    envelope["delta"] = self.seal(
                        identity, "page", {"url": self.graph_link(delta_link), "headers": headers}
                    )
                if isinstance(data, dict) and isinstance(data.get("value"), list):
                    envelope["page"]["returned"] = len(data["value"])
                if not transfer:
                    envelope["data"] = self.sanitize(identity, data, headers)
                    envelope["content"]["omitted_fields"] = [
                        "Preauthenticated download/upload URLs are available via transfer tools."
                    ]
                return envelope
        except GraphFailure as exc:
            if not read_only and exc.code in (
                "invalid_graph_response",
                "response_too_large",
                "binary_transfer_required",
            ):
                raise GraphFailure(
                    "write_outcome_unknown",
                    "The write returned success but its result could not be read. "
                    "Inspect the destination before retrying.",
                    502,
                    request_id,
                ) from None
            raise
        except httpx.HTTPError:
            raise GraphFailure(
                "graph_unavailable" if read_only else "write_outcome_unknown",
                "Graph could not be reached. Retry the read."
                if read_only
                else (
                    "The write outcome is unknown. Inspect the destination "
                    "before retrying; no automatic retry was made."
                ),
                502,
                request_id,
            ) from None

    @classmethod
    def redact(cls, value):
        if isinstance(value, dict):
            return {
                k: cls.redact(v)
                for k, v in value.items()
                if k
                not in (
                    "@microsoft.graph.downloadUrl",
                    "@content.downloadUrl",
                    "uploadUrl",
                    "@odata.nextLink",
                    "@odata.deltaLink",
                )
            }
        if isinstance(value, list):
            return [cls.redact(v) for v in value]
        return value

    def sanitize(self, identity, value, headers):
        if isinstance(value, list):
            return [self.sanitize(identity, item, headers) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in ("@microsoft.graph.downloadUrl", "@content.downloadUrl", "uploadUrl"):
                continue
            if key.endswith("@odata.nextLink") or key.endswith("@odata.deltaLink"):
                # Expanded collections can carry their own continuation, independent of the page.
                result[
                    key.replace("@odata.nextLink", "mcp_next").replace(
                        "@odata.deltaLink", "mcp_delta"
                    )
                ] = self.seal(identity, "page", {"url": self.graph_link(item), "headers": headers})
            else:
                result[key] = self.sanitize(identity, item, headers)
        return result

    async def continue_page(self, identity, handle):
        state = self.open(identity, "page", handle)
        path = urlsplit(state["url"]).path[len("/v1.0") :]
        return await self.request(identity, "GET", path, headers=state["headers"], url=state["url"])

    async def prepare(self, identity, direction, path, filename=None, size=None, etag=None):
        path = canonical_path(path)
        # Only drive item metadata and parent item IDs, not arbitrary Graph resources.
        if not re.fullmatch(
            r"/(?:me/drive|users/[^/]+/drive|drives/[^/]+|sites/[^/]+/drive|groups/[^/]+/drive)/(?:items/[^/:]+|root)",
            path,
        ):
            raise ValueError(
                "Use a drive root or drive item path; other artifact types "
                "need separate staging support."
            )
        if direction == "download":
            result = await self.request(identity, "GET", path, transfer=True)
            item = result["data"]
            if (
                not isinstance(item, dict)
                or not item.get("file")
                or not isinstance(item.get("size"), int)
            ):
                raise ValueError("The destination is not a downloadable file.")
            if item["size"] > MAX_TRANSFER:
                raise ValueError("File exceeds the supported 250 MB transfer target.")
            url = self.storage_link(item.get("@microsoft.graph.downloadUrl", ""))
            state = {
                "direction": direction,
                "path": path,
                "item_id": item.get("id"),
                "size": item["size"],
                "etag": item.get("eTag"),
            }
            return {
                "transfer": self.seal(identity, "transfer", state),
                "direction": direction,
                "url": url,
                "method": "GET",
                "name": item.get("name"),
                "size": item["size"],
                "expiry": "provider-controlled; refresh on rejection",
                "authorization": "Do not attach a Graph bearer token to this URL.",
            }
        if size is None or not 0 < size <= MAX_TRANSFER:
            raise ValueError("Upload size must be between 1 and 250000000 bytes.")
        if filename:
            if (
                any(c in filename for c in "/\\:%?#")
                or filename in (".", "..")
                or any(ord(c) < 32 for c in filename)
            ):
                raise ValueError("Use a single filename without path delimiters.")
            target = path + ":/" + filename + ":/createUploadSession"
            item = {"@microsoft.graph.conflictBehavior": "rename", "name": filename}
        else:
            if not etag:
                raise ValueError(
                    "Replacing an existing item requires its current etag; "
                    "supply filename to create a copy."
                )
            target = path + "/createUploadSession"
            item = {"@microsoft.graph.conflictBehavior": "fail"}
        result = await self.request(
            identity,
            "POST",
            target,
            body={"item": item},
            headers={"If-Match": etag} if etag else None,
            read_only=False,
            transfer=True,
        )
        session = result["data"]
        url = self.storage_link(session["uploadUrl"])
        state = {
            "direction": direction,
            "path": path,
            "url": url,
            "size": size,
            "filename": filename,
            "expires_at": session.get("expirationDateTime"),
        }
        return {
            "transfer": self.seal(identity, "transfer", state),
            "direction": direction,
            "url": url,
            "method": "PUT",
            "size": size,
            "expires_at": state["expires_at"],
            "chunk_size": 10 * 1024 * 1024,
            "instructions": (
                "Upload sequential Content-Range chunks; non-final chunks must "
                "be multiples of 320 KiB. Do not attach a Graph bearer. Preserve "
                "the final driveItem response for verification."
            ),
        }

    async def transfer_status(self, identity, handle, completed_item_path=None):
        state = self.open(identity, "transfer", handle)
        if completed_item_path:
            if state["direction"] != "upload":
                raise ValueError("Completed item verification applies to uploads.")
            parent = state["path"].split("/items/")[0].removesuffix("/root")
            if not completed_item_path.startswith(parent + "/items/"):
                raise ValueError("Verify an item in the destination drive.")
            item = (await self.request(identity, "GET", completed_item_path))["data"]
            return {
                "status": "verified_size" if item.get("size") == state["size"] else "size_mismatch",
                "item": item,
                "expected_size": state["size"],
                "verification": (
                    "Size and destination drive checked; this is not a content "
                    "hash or proof of upload-session identity."
                ),
            }
        if state["direction"] == "download":
            item = (await self.request(identity, "GET", state["path"]))["data"]
            return {
                "status": "source_unchanged"
                if item.get("eTag") == state.get("etag")
                else "source_changed",
                "item": item,
                "download_completion": "unknown; verify bytes in the receiving terminal",
            }
        return await self.storage_request(state, "GET")

    async def storage_request(self, state, method):
        url = self.storage_link(state["url"])
        # A new client prevents any Graph Authorization/default headers reaching storage.
        client = self.storage_http
        try:
            async with client.stream(method, url) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise GraphFailure(
                            "invalid_transfer_response",
                            "Transfer status exceeded its size limit.",
                        )
                if method == "DELETE" and response.status_code == 204:
                    return {"status": "cancelled"}
                if response.status_code != 200:
                    return {
                        "status": "unknown",
                        "http_status": response.status_code,
                        "note": (
                            "A missing upload session can mean completion, "
                            "expiry or cancellation; verify the destination."
                        ),
                    }
                return {"status": "upload_session_active", "data": self.redact(json.loads(raw))}
        except httpx.HTTPError, ValueError:
            raise GraphFailure(
                "transfer_unavailable",
                "Transfer outcome is unknown; inspect the destination before repeating a write.",
            ) from None

    async def transfer_manage(self, identity, handle, action):
        state = self.open(identity, "transfer", handle)
        if action != "cancel" or state["direction"] != "upload":
            raise ValueError(
                "Only provider-side upload cancellation is supported. "
                "Download URLs expire at the provider; no local revocation "
                "is claimed."
            )
        return await self.storage_request(state, "DELETE")

    async def aclose(self):
        await self.storage_http.aclose()
