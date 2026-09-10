"""The first Graph operation, with a fixed origin and selected profile fields."""

import json
from typing import Literal
from uuid import uuid4

import httpx

from ms_graph_mcp.auth import DelegatedAccessToken
from ms_graph_mcp.obo import OboClient

ProfileField = Literal[
    "id",
    "displayName",
    "givenName",
    "surname",
    "mail",
    "userPrincipalName",
    "jobTitle",
    "officeLocation",
    "preferredLanguage",
]
DEFAULT_FIELDS = ["id", "displayName", "mail", "userPrincipalName"]
ME_URL = "https://graph.microsoft.com/v1.0/me"


class GraphFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 502,
        request_id: str | None = None,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


class GraphClient:
    def __init__(self, http: httpx.AsyncClient, obo: OboClient):
        self.http = http
        self.obo = obo

    async def me(self, identity: DelegatedAccessToken, select: list[str]) -> dict:
        request_id = str(uuid4())
        try:
            async with self.http.stream(
                "GET",
                ME_URL,
                params={"$select": ",".join(select)},
                headers={
                    "Authorization": f"Bearer {identity.graph_token.get_secret_value()}",
                    "Accept": "application/json",
                    "client-request-id": request_id,
                    "return-client-request-id": "true",
                },
                follow_redirects=False,
            ) as response:
                if response.status_code == 401:
                    self.obo.invalidate(identity.token)
                    raise GraphFailure(
                        "graph_authentication_required",
                        "Graph rejected the delegated token. Retry once; "
                        "reconnect Microsoft if it persists.",
                        401,
                        request_id,
                    )
                if response.status_code == 403:
                    raise GraphFailure(
                        "graph_access_denied",
                        "Graph denied access; check User.Read consent and policy.",
                        403,
                        request_id,
                    )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = (
                        int(retry_after)
                        if retry_after.isascii()
                        and retry_after.isdecimal()
                        and len(retry_after) <= 10
                        else None
                    )
                    raise GraphFailure(
                        "graph_throttled",
                        "Graph throttled this request. Wait retry_after_seconds before retrying."
                        if delay is not None
                        else "Graph throttled this request. Retry later.",
                        429,
                        request_id,
                        retry_after_seconds=delay,
                    )
                if response.status_code != 200:
                    raise GraphFailure(
                        "graph_upstream_error",
                        "Graph did not return a profile.",
                        response.status_code,
                        request_id,
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    # A selected /me profile should be small. This is an upstream safety check,
                    # not the future generic response budget or a file-transfer limit.
                    if len(raw) > 65536:
                        raise GraphFailure(
                            "unexpected_profile_size",
                            "Graph returned an unexpectedly large profile.",
                            request_id=request_id,
                        )
                try:
                    profile = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise GraphFailure(
                        "invalid_graph_response",
                        "Graph returned invalid JSON.",
                        request_id=request_id,
                    ) from None
                if not isinstance(profile, dict) or "error" in profile:
                    raise GraphFailure(
                        "invalid_graph_response",
                        "Graph returned an invalid profile.",
                        request_id=request_id,
                    )
        except httpx.HTTPError:
            raise GraphFailure(
                "graph_unavailable",
                "Graph could not be reached. Retry later.",
                request_id=request_id,
            ) from None
        return {
            "status": 200,
            "data": {key: profile[key] for key in select if key in profile},
            "selection": {"fields": select},
            "delivery": "inline",
            "request_id": request_id,
        }
