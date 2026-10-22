"""Explicit tenant/resource configuration; no tenant or credential auto-discovery."""

import re
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, FilePath, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRAPH_MCP_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    tenant_id: UUID
    client_id: UUID
    client_secret: SecretStr
    allowed_client_ids: list[UUID] = Field(min_length=1)
    resource_url: str
    scope_name: str = "access_as_user"
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_origins: list[str] = Field(default_factory=list)
    http_timeout_seconds: float = Field(default=15, ge=1, le=120)
    obo_cache_entries: int = Field(default=128, ge=1, le=4096)
    obo_max_concurrent_exchanges: int = Field(default=4, ge=1, le=32)
    audit_log: bool = False
    extra_ca_file: FilePath | None = None
    max_request_body_bytes: int = Field(default=60000, ge=60000, le=2000000)
    sharepoint_site_url: str | None = None

    @field_validator("sharepoint_site_url")
    @classmethod
    def canonical_sharepoint_site(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        parts = urlsplit(value)
        hostname = parts.hostname or ""
        path = parts.path.rstrip("/")
        if (
            parts.scheme != "https"
            or not parts.netloc.isascii()
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.sharepoint\.com", hostname)
            or parts.netloc.lower() != hostname
            or parts.query
            or parts.fragment
            or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", path)
            or any(segment in (".", "..") for segment in path.split("/"))
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError(
                "sharepoint_site_url must be an HTTPS tenant.sharepoint.com site URL, "
                "with an unescaped site path and no credentials, port, query or fragment."
            )
        return f"https://{hostname}{path}"

    @property
    def sharepoint_resource_origin(self) -> str | None:
        if self.sharepoint_site_url is None:
            return None
        return "https://" + urlsplit(self.sharepoint_site_url).netloc

    @property
    def max_mcp_request_bytes(self) -> int:
        # Allow JSON escaping (up to six bytes per HTML character) plus the RPC
        # envelope/query/headers. The decoded outbound payload has its own bound.
        return self.max_request_body_bytes * 6 + 65536

    @field_validator("resource_url")
    @classmethod
    def canonical_resource(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.path
            or parts.path.endswith("/")
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError(
                "resource_url must be a canonical HTTPS MCP URL, without query or slash suffix"
            )
        return value

    @field_validator("client_secret")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("client_secret is required")
        return value

    @field_validator("scope_name")
    @classmethod
    def simple_scope(cls, value: str) -> str:
        if not value or not all(c.isascii() and (c.isalnum() or c in "_.-") for c in value):
            raise ValueError("scope_name must be a simple Entra delegated scope name")
        return value

    @model_validator(mode="after")
    def no_wildcard_ingress(self) -> Settings:
        for host in self.allowed_hosts:
            if not host or "*" in host or "/" in host or any(c.isspace() for c in host):
                raise ValueError("allowed_hosts must contain explicit host[:port] values")
        for origin in self.allowed_origins:
            parts = urlsplit(origin)
            if (
                parts.scheme != "https"
                or not parts.netloc
                or parts.path
                or parts.query
                or parts.fragment
                or parts.username
                or "*" in origin
            ):
                raise ValueError("allowed_origins must contain exact HTTPS origins")
        return self

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def issuer(self) -> str:
        return f"{self.authority}/v2.0"

    @property
    def jwks_url(self) -> str:
        return f"{self.authority}/discovery/v2.0/keys"

    @property
    def oauth_scope(self) -> str:
        return f"api://{self.client_id}/{self.scope_name}"

    @property
    def metadata_path(self) -> str:
        return "/.well-known/oauth-protected-resource" + urlsplit(self.resource_url).path

    @property
    def metadata_url(self) -> str:
        parts = urlsplit(self.resource_url)
        return f"{parts.scheme}://{parts.netloc}{self.metadata_path}"
