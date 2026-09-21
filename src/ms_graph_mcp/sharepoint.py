"""Site-scoped SharePoint REST operations using a separate delegated OBO token.

Only the deployment-configured site is addressable. No caller-supplied REST path,
headers, tenant administration endpoint or continuation URL is accepted.

Endpoint references:
https://learn.microsoft.com/sharepoint/dev/declarative-customization/site-theming/sharepoint-site-theming-rest-api
https://github.com/pnp/pnpjs/blob/version-4/packages/sp/navigation/types.ts
https://github.com/pnp/pnpjs/blob/version-4/packages/sp/folders/types.ts
"""

import json
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx

from ms_graph_mcp.auth import DelegatedAccessToken
from ms_graph_mcp.config import Settings
from ms_graph_mcp.graph import GraphFailure
from ms_graph_mcp.obo import OboClient

MAX_RESPONSE_BYTES = 2_000_000
MAX_NAVIGATION_NODES = 500
NAVIGATION_LOCATIONS = {"quicklaunch", "topnavigationbar"}
NODE_FIELDS = ("Id", "Title", "Url", "IsExternal", "IsVisible")


def _text(value: str, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"{name} must be nonempty text of at most {maximum} characters.")
    return value


def _node_id(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError("Navigation IDs must be positive 32-bit integers.")
    return value


def _location(value: str) -> str:
    if value not in NAVIGATION_LOCATIONS:
        raise ValueError("location must be quicklaunch or topnavigationbar.")
    return value


def _link(value: str) -> str:
    value = _text(value, "url", 2048)
    decoded = unquote(value)
    parts = urlsplit(value)
    if (
        any(c.isspace() for c in value)
        or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in decoded)
        or parts.username
        or parts.password
        or decoded.startswith("//")
        or not (
            (parts.scheme == "https" and parts.hostname)
            or (not parts.scheme and not parts.netloc and value.startswith("/"))
        )
    ):
        raise ValueError("url must be an HTTPS link or a server-relative path.")
    return value


def _invalid_response(request_id: str, *, write: bool = False) -> GraphFailure:
    return GraphFailure(
        "sharepoint_write_outcome_unknown" if write else "invalid_sharepoint_response",
        "SharePoint returned an invalid response. The write may have completed; "
        "read current state before retrying."
        if write
        else "SharePoint returned an invalid or unsupported response.",
        request_id=request_id,
    )


class SharePointClient:
    def __init__(self, http: httpx.AsyncClient, obo: OboClient | None, settings: Settings):
        if obo is not None and obo.resource_origin != settings.sharepoint_resource_origin:
            raise ValueError("SharePoint OBO must target the configured site's resource origin.")
        self.http = http
        self.obo = obo
        self.settings = settings

    def _site(self) -> str:
        if not self.settings.sharepoint_site_url or self.obo is None:
            raise GraphFailure(
                "sharepoint_not_configured",
                "Set GRAPH_MCP_SHAREPOINT_SITE_URL and consent delegated SharePoint "
                "permissions to enable site settings operations.",
                503,
            )
        return self.settings.sharepoint_site_url

    def _navigation_link(self, url: str) -> dict:
        url = _link(url)
        site = urlsplit(self._site())
        link = urlsplit(url)
        site_path = site.path.rstrip("/")
        link_path = link.path or "/"
        external = bool(
            (link.netloc and link.netloc.lower() != site.netloc)
            or not (link_path == site_path or link_path.startswith(site_path + "/"))
        )
        if not external and link.netloc:
            # SharePoint expects a relative Url when IsExternal is false.
            url = link._replace(scheme="", netloc="", path=link_path).geturl()
        return {"Url": url, "IsExternal": external}

    async def _request(
        self,
        identity: DelegatedAccessToken,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        write: bool = False,
        merge: bool = False,
        empty_response: bool = False,
    ) -> dict:
        site = self._site()
        payload = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            if body is not None
            else None
        )
        if payload is not None and len(payload) > self.settings.max_request_body_bytes:
            raise ValueError("SharePoint request exceeds the configured request body limit.")
        if type(identity.expires_at) is not int:
            raise ValueError("SharePoint requires a delegated identity with an expiry.")
        # The supplied OBO client must target this SharePoint origin, never Graph.
        token = await self.obo.acquire(identity.token, identity.expires_at)
        request_id = str(uuid4())
        headers = {
            "Authorization": f"Bearer {token.value.get_secret_value()}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata;charset=utf-8",
            "client-request-id": request_id,
            "return-client-request-id": "true",
        }
        if merge:
            headers["X-HTTP-Method"] = "MERGE"
        try:
            async with self.http.stream(
                method,
                site + "/_api" + path,
                params=query,
                content=payload,
                headers=headers,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status == 401:
                    self.obo.invalidate(identity.token)
                if not 200 <= status < 300:
                    unknown = write and (status >= 500 or status == 408 or 300 <= status < 400)
                    code = {
                        401: "sharepoint_authentication_required",
                        403: "sharepoint_access_denied",
                        404: "sharepoint_not_found",
                        409: "sharepoint_conflict",
                        412: "sharepoint_precondition_failed",
                        429: "sharepoint_throttled",
                    }.get(status, "sharepoint_upstream_error")
                    retry = response.headers.get("Retry-After", "")
                    delay = (
                        int(retry)
                        if retry.isascii() and retry.isdecimal() and len(retry) <= 10
                        else None
                    )
                    message = {
                        401: "SharePoint rejected its delegated token. Reconnect Microsoft "
                        "if this persists.",
                        403: "SharePoint denied access. Check SharePoint delegated consent "
                        "and the signed-in user's site permissions.",
                        429: "SharePoint throttled this request. Wait before retrying.",
                    }.get(status, "SharePoint rejected the request.")
                    if unknown:
                        code = "sharepoint_write_outcome_unknown"
                        message = "The SharePoint write may have completed. Read current "
                        message += "state before retrying; the server did not retry it."
                    raise GraphFailure(code, message, status, request_id, delay)
                if status not in (200, 201, 204):
                    raise _invalid_response(request_id, write=write)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        if write:
                            raise _invalid_response(request_id, write=True)
                        raise GraphFailure(
                            "sharepoint_response_too_large",
                            "SharePoint response exceeds the safe response limit.",
                            request_id=request_id,
                        )
                if not raw and empty_response:
                    data = None
                else:
                    try:
                        data = json.loads(raw)
                    except ValueError, UnicodeError, RecursionError:
                        raise _invalid_response(request_id, write=write) from None
                    if not isinstance(data, dict) or "error" in data or "odata.error" in data:
                        raise _invalid_response(request_id, write=write)
                    if "d" in data:
                        data = data["d"]
                    if not isinstance(data, dict) or "error" in data:
                        raise _invalid_response(request_id, write=write)
                    if data.get("odata.null") is True or data.get("@odata.null") is True:
                        raise GraphFailure(
                            "sharepoint_not_found",
                            "SharePoint reports that the requested resource does not exist. "
                            "Read current state before retrying.",
                            404,
                            request_id,
                        )
        except httpx.HTTPError:
            raise GraphFailure(
                "sharepoint_write_outcome_unknown" if write else "sharepoint_unavailable",
                "The SharePoint write may have completed. Read current state before "
                "retrying; the server did not retry it."
                if write
                else "SharePoint could not be reached. Retry later.",
                request_id=request_id,
            ) from None
        return {
            "status": status,
            "data": data,
            "site_url": site,
            "delivery": "inline",
            "request_id": request_id,
            "outcome": "completed" if write else "read",
        }

    async def read_settings(self, identity: DelegatedAccessToken) -> dict:
        result = await self._request(
            identity,
            "GET",
            "/web",
            query={
                "$select": "Id,Title,Url,ServerRelativeUrl,WebTemplate,SiteLogoUrl,"
                "RootFolder/WelcomePage",
                "$expand": "RootFolder",
            },
        )
        if not isinstance(result["data"].get("Id"), str):
            raise _invalid_response(result["request_id"])
        return result

    async def read_navigation(
        self,
        identity: DelegatedAccessToken,
        location: str = "quicklaunch",
        parent_id: int | None = None,
    ) -> dict:
        location = _location(location)
        path = f"/web/navigation/{location}"
        if parent_id is not None:
            path = f"/web/navigation/GetNodeById({_node_id(parent_id)})/children"
        result = await self._request(
            identity,
            "GET",
            path,
            query={"$select": ",".join(NODE_FIELDS), "$top": str(MAX_NAVIGATION_NODES + 1)},
        )
        data = result["data"]
        nodes = data.get("value", data.get("results"))
        if not isinstance(nodes, list) or any(
            not isinstance(node, dict) or type(node.get("Id")) is not int for node in nodes
        ):
            raise _invalid_response(result["request_id"])
        if len(nodes) > MAX_NAVIGATION_NODES or any(
            data.get(key) for key in ("@odata.nextLink", "odata.nextLink", "__next")
        ):
            raise GraphFailure(
                "sharepoint_navigation_incomplete",
                "The navigation collection exceeds the supported single-collection limit. "
                "No partial result was returned; read a smaller child collection by parent_id.",
                request_id=result["request_id"],
            )
        result["data"] = {
            "nodes": [{key: node[key] for key in NODE_FIELDS if key in node} for node in nodes],
            "location": location if parent_id is None else None,
            "parent_id": parent_id,
            "complete": True,
            "children_included": False,
        }
        return result

    async def write_navigation(
        self,
        identity: DelegatedAccessToken,
        action: str,
        location: str = "quicklaunch",
        node_id: int | None = None,
        parent_id: int | None = None,
        title: str | None = None,
        url: str | None = None,
    ) -> dict:
        location = _location(location)
        if action not in ("add", "update", "delete"):
            raise ValueError("Navigation action must be add, update or delete.")
        if action == "add":
            if node_id is not None:
                raise ValueError("Navigation add does not accept node_id.")
            body = {"Title": _text(title, "title", 255), "IsVisible": True}
            body.update(self._navigation_link(url))
            path = f"/web/navigation/{location}"
            if parent_id is not None:
                path = f"/web/navigation/GetNodeById({_node_id(parent_id)})/children"
            result = await self._request(identity, "POST", path, body=body, write=True)
            if type(result["data"].get("Id")) is not int:
                raise _invalid_response(result["request_id"], write=True)
            result["data"] = {
                key: result["data"][key] for key in NODE_FIELDS if key in result["data"]
            }
            return result
        if parent_id is not None:
            raise ValueError("Navigation update/delete address node_id; omit parent_id.")
        path = f"/web/navigation/GetNodeById({_node_id(node_id)})"
        if action == "delete":
            if title is not None or url is not None:
                raise ValueError("Navigation delete does not accept title or url.")
            return await self._request(identity, "DELETE", path, write=True, empty_response=True)
        body = {}
        if title is not None:
            body["Title"] = _text(title, "title", 255)
        if url is not None:
            body.update(self._navigation_link(url))
        if not body:
            raise ValueError("Navigation update requires title or url.")
        return await self._request(
            identity, "POST", path, body=body, write=True, merge=True, empty_response=True
        )

    async def _themes(self, identity: DelegatedAccessToken) -> tuple[dict, list[dict]]:
        result = await self._request(identity, "POST", "/thememanager/GetTenantThemingOptions")
        data = result["data"].get("GetTenantThemingOptions", result["data"])
        if not isinstance(data, dict):
            raise _invalid_response(result["request_id"])
        themes = data.get("themePreviews")
        if isinstance(themes, dict):
            if themes.get("__next"):
                raise _invalid_response(result["request_id"])
            themes = themes.get("results")
        if not isinstance(themes, list) or any(
            not isinstance(theme, dict)
            or not isinstance(theme.get("name"), str)
            or not theme["name"].strip()
            or not isinstance(theme.get("themeJson"), str)
            for theme in themes
        ):
            raise _invalid_response(result["request_id"])
        result["data"] = {
            "themes": [{"name": theme["name"]} for theme in themes],
            "hide_default_themes": data.get("hideDefaultThemes"),
        }
        return result, themes

    async def read_themes(self, identity: DelegatedAccessToken) -> dict:
        result, _ = await self._themes(identity)
        return result

    async def apply_theme(self, identity: DelegatedAccessToken, theme_name: str) -> dict:
        theme_name = _text(theme_name, "theme_name", 255)
        listing, themes = await self._themes(identity)
        matches = [theme for theme in themes if theme["name"] == theme_name]
        if len(matches) != 1:
            raise ValueError("Choose one exact, unambiguous theme name returned by read_themes.")
        try:
            theme = json.loads(matches[0]["themeJson"])
        except ValueError, RecursionError:
            raise _invalid_response(listing["request_id"]) from None
        if not isinstance(theme, dict) or not isinstance(theme.get("palette"), dict):
            raise _invalid_response(listing["request_id"])
        result = await self._request(
            identity,
            "POST",
            "/thememanager/ApplyTheme",
            body={"name": theme_name, "themeJson": matches[0]["themeJson"]},
            write=True,
        )
        applied = result["data"].get("value", result["data"].get("ApplyTheme"))
        # Depending on the SharePoint service version, ApplyTheme returns true
        # or the generated theme's server-relative catalog path (PnP CLI).
        theme_path = None
        if isinstance(applied, str) and len(applied) <= 2048:
            prefix = urlsplit(self._site()).path + "/_catalogs/theme/Themed/"
            if (
                applied.casefold().startswith(prefix.casefold())
                and len(applied) > len(prefix)
                and all(c.isascii() and (c.isalnum() or c in "/._~-") for c in applied)
                and all(segment not in (".", "..", "") for segment in applied.split("/")[1:])
            ):
                theme_path = applied
        if applied is not True and theme_path is None:
            raise _invalid_response(result["request_id"], write=True)
        result["data"] = {"theme_name": theme_name, "applied": True}
        if theme_path is not None:
            result["data"]["theme_path"] = theme_path
        return result

    async def set_home_page(self, identity: DelegatedAccessToken, page_path: str) -> dict:
        page_path = _text(page_path, "page_path", 400)
        segments = page_path.split("/")
        if (
            len(segments) < 2
            or segments[0] != "SitePages"
            or not segments[-1].lower().endswith(".aspx")
            or any(segment in ("", ".", "..") or segment != segment.strip() for segment in segments)
            or any(c in page_path for c in "\\%?#:")
        ):
            raise ValueError("page_path must be a web-relative SitePages/*.aspx path, unencoded.")
        site = self._site()
        server_path = urlsplit(site).path + "/" + page_path
        existing = await self._request(
            identity,
            "GET",
            "/web/GetFileByServerRelativePath(decodedUrl=@page)",
            query={
                "@page": "'" + server_path.replace("'", "''") + "'",
                "$select": "Exists,ServerRelativeUrl",
            },
        )
        if existing["data"].get("Exists") is not True:
            raise ValueError("The proposed home page does not exist in this site's SitePages.")
        if existing["data"].get("ServerRelativeUrl") != server_path:
            raise _invalid_response(existing["request_id"])
        result = await self._request(
            identity,
            "POST",
            "/web/RootFolder",
            body={"WelcomePage": page_path},
            write=True,
            merge=True,
            empty_response=True,
        )
        result["data"] = {"home_page": page_path}
        return result
