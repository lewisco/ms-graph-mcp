# Authentication scaffold setup

Status: local scaffold implemented; real Entra → Open WebUI → LiteLLM → Graph authorization remains unverified. The only Graph operation is `GET /v1.0/me`. This is gate G1's starting point, not the complete Microsoft 365 gateway.

If your administrator has already configured the connection, start with the [user guide](usage.md). For setting names, defaults and limits, use the [configuration reference](configuration.md).

## 1. Entra registrations

Use the existing WebUI SSO registration for WebUI login. Create these two separate registrations in the same tenant:

| Registration | Configure | Secret owner |
| --- | --- | --- |
| Graph MCP API | Single tenant; expose `api://<MCP_CLIENT_ID>/access_as_user`; Graph **delegated** `User.Read`; client secret for OBO | MCP backend |
| WebUI Microsoft tools | Single tenant; confidential Web client; actual WebUI MCP OAuth callback registered as a Web redirect URI; delegated permission to the MCP scope | WebUI backend |

On the **Graph MCP API**, set `api.requestedAccessTokenVersion` to `2` in the Microsoft Graph-format application manifest. The verifier deliberately accepts v2 access tokens only; merely using Entra's `/v2.0` token endpoint does not determine the resource's token version. [Microsoft API application properties](https://learn.microsoft.com/en-us/graph/api/resources/apiapplication?view=graph-rest-1.0)

Grant the MCP API's delegated Graph `User.Read` consent and the WebUI tools client's MCP scope consent under your tenant's consent policy. OBO cannot open a consent prompt itself, so provision downstream consent before this test. Do not use application permissions. [Microsoft OBO and consent](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)

Register the exact callback used by your WebUI MCP connection; it is distinct from WebUI's login callback and depends on its externally visible URL. Do not guess it or reuse a login callback merely because both use Entra. Put the tools client's UUID into `GRAPH_MCP_ALLOWED_CLIENT_IDS`.

Only `User.Read` is needed for this slice. Broader agreed service permissions are added with their implementations; see [service coverage](service-coverage.md).

## 2. Resource and token mapping

| Value | Example / expected value |
| --- | --- |
| Public MCP resource | `https://litellm.example.com/msgraph/mcp` |
| Internal server endpoint | `https://ms-graph-mcp.ai.svc:8000/mcp` with the default Helm TLS mode |
| MCP API App ID URI | `api://<MCP_CLIENT_ID>` |
| Requested OAuth scope | `api://<MCP_CLIENT_ID>/access_as_user` |
| Incoming access-token `aud` | MCP API client UUID, **not** Graph or the public URL |
| Incoming `iss` | `https://login.microsoftonline.com/<TENANT_ID>/v2.0` |
| Incoming `tid` / `ver` | Configured tenant UUID / `2.0` |
| Incoming `azp` | WebUI tools OAuth client UUID |
| Incoming `scp` | Includes `access_as_user` |
| Incoming `oid` | Connecting user's tenant object UUID |
| Graph scope requested by MSAL | `https://graph.microsoft.com/.default` |

The public resource URL is explicitly mapped to the validated Entra API audience. The server does not derive trusted identity from `Host`, forwarding headers, or an arbitrary token issuer. Only the acquired Graph token is sent to Graph.

## 3. Run locally

Install Python 3.14 and `uv`, or let `uv sync` install Python from `.python-version`.

```sh
uv sync --locked --no-editable
cp .env.example .env
```

Replace the example IDs, MCP secret, resource URL, and host list in `.env`. Keep `127.0.0.1:8000` in the allowed hosts for these local checks.

```sh
uv run --no-editable uvicorn ms_graph_mcp.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

In another terminal:

```sh
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/.well-known/oauth-protected-resource
uv run --no-editable pytest -q
uv run --no-editable ruff check src tests
uv run --no-editable ruff format --check src tests
```

The commands use a regular wheel installation. On this iCloud-backed workspace, macOS marked the editable-install `.pth` file hidden and Python skipped it; a non-editable install avoids that failure. Re-run `uv sync --locked --no-editable --reinstall-package ms-graph-mcp` after source edits when running executables directly from `.venv/bin`.

The process loads required settings at startup. Health/readiness and metadata ignore bearer headers and never perform signing-key retrieval or token exchange; they do not prove user authorization. Protected MCP calls still require valid authorization. Tests use generated RSA keys and mocked Microsoft network responses. Pytest imports the current `src` tree even when the installed wheel has not yet been refreshed.

## 4. Container and cluster wiring

```sh
docker login dhi.io
# Development build only; use scripts/release_image.py for the release scan gate.
docker build -t ms-graph-mcp:0.1.0 .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 ms-graph-mcp:0.1.0
```

The runtime is non-root and contains the server dependencies, without document tooling. The terminal image is a separate later deliverable. Inject `GRAPH_MCP_CLIENT_SECRET` via your Kubernetes secret mechanism. The MCP needs no writable persistent volume for this slice; token caches are bounded process memory and disappear on restart. Deployment requires at least two replicas for the agreed HA requirement; the [Helm chart and manual build guide](kubernetes.md) provide configurable replicas, enterprise CAs, and Harbor/GHCR instructions.

Local build attempt on 2026-09-10 stopped at registry metadata timeouts for Docker Hub/GHCR, before build steps ran. The Dockerfile is provided but the image build and container startup are unverified. The packaged server did start successfully outside Docker.

Expose port 8000 privately to LiteLLM. Helm enables a NetworkPolicy requiring explicit gateway selectors and HTTPS using `transportSecurity.existingSecret`. Configure LiteLLM to verify the certificate issuer and Service DNS name. HTTP is appropriate only for the loopback development commands or the explicit `transportSecurity.mode: mesh` option with enforced mesh mTLS. Publish the client-facing LiteLLM URL with HTTPS. Configure outbound access to `login.microsoftonline.com` and `graph.microsoft.com`. Graph and OBO clients ignore HTTP proxy environment variables; JWKS uses urllib's proxy behavior. A unified explicit proxy configuration is not implemented. Outbound enterprise TLS trust is configured separately using `GRAPH_MCP_EXTRA_CA_FILE`.

Allow the exact `Host` LiteLLM uses in `GRAPH_MCP_ALLOWED_HOSTS`. For cross-namespace Kubernetes service URLs, include that actual service DNS name and port. The server rejects unexpected Host and Origin headers. An empty origins list accepts requests with no Origin and rejects requests with one; add the exact HTTPS origin if your proxy sends it. Health routes are simple unauthenticated process probes, so network policy remains relevant.

No ingress timeout or buffering settings have been validated in AKS/RKE2. The `/mcp` route uses stateless Streamable HTTP with JSON responses. Clients should send `Accept: application/json, text/event-stream`. This is neither legacy HTTP+SSE nor an unprotected REST proxy.

## 5. LiteLLM and WebUI

Merge [the example LiteLLM entry](../deploy/litellm.example.yaml) into your configuration. Use `oauth_delegate` for this design, with OBO owned by the MCP. LiteLLM documents the separate gateway admission header and upstream bearer. [LiteLLM OAuth routing](https://docs.litellm.ai/docs/mcp_oauth_passthrough)

Configure the WebUI MCP connection:

| Setting | Value |
| --- | --- |
| MCP URL | `https://<litellm-host>/msgraph/mcp` |
| Authentication | OAuth with the static **WebUI tools** client ID and secret |
| Authorization endpoint | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/authorize` |
| Token endpoint | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token` |
| Scopes | `openid profile offline_access api://<MCP_CLIENT_ID>/access_as_user` |
| Custom header | `x-litellm-api-key: Bearer <gateway-key>` |

WebUI supplies the per-user OAuth access token as `Authorization: Bearer …`; do not put a fixed Microsoft token in custom headers. The tools client secret belongs in WebUI, the MCP API secret belongs in the MCP, and neither belongs in the LiteLLM MCP entry.

If WebUI's OAuth client sends the RFC `resource` parameter and Entra rejects it, test WebUI's **Omit resource** option while retaining the explicit MCP scope. Record the result in G1; changing the verifier to accept Graph or ID tokens is not an interoperability fix.

Ensure the public metadata challenge can be followed. For the example public resource, the server advertises:

```text
https://litellm.example.com/.well-known/oauth-protected-resource/msgraph/mcp
```

The MCP serves that same path internally and also `/.well-known/oauth-protected-resource`. Configure the ingress/proxy so the advertised public metadata URL reaches the correct document. A `/msgraph/mcp` route alone does not prove metadata forwarding works. This public routing is an explicit live compatibility check.

## 6. First live check and known boundaries

Connect Microsoft in WebUI, discover the tools, and ask it to call `graph_read` with `{"path":"/me"}`. Confirm the returned profile is yours. Repeat with another user's connection and complete [G1 in the acceptance plan](acceptance.md) using the detailed checklist there.

Implemented locally:

- Tenant-specific signature, audience, issuer, time, scope, user, and allowed-client validation.
- OBO via MSAL; per-assertion SHA-256 cache keys, expiry-aware eviction, no refresh-token persistence, no app-only fallback. Cache hits run independently of exchanges. Up to four distinct exchanges run concurrently by default; repeated requests for one assertion share an exchange, and sanitized failures have a bounded ten-second backoff.
- Signing keys have a five-minute TTL and a global thirty-second refresh cooldown, including failed refreshes. Cached-key validation proceeds independently of network refresh. A newly rotated key may require a retry after the cooldown. See [security controls](security.md).
- Authentication/consent failures before MCP processing become HTTP errors. Valid JSON claims challenges from OBO can be included in `WWW-Authenticate`.
- Selected `/me` fields returned as one compact JSON text result; no duplicate full structured representation.
- Graph errors remain tool errors. Graph 401 invalidates the cached downstream token and asks for one retry/reconnection. No transparent Graph claims challenge recovery is implemented yet.
- Graph 429 returns the numeric `Retry-After` value as `retry_after_seconds` when supplied. The server does not retry; the caller must wait. General retry orchestration is pending.

The 64 KiB request/profile guards protect this tiny profile-only API. They are **not** the future document-size limit or generic pagination design. There are no file transfer routes yet. Error bodies avoid raw Microsoft response text; ordinary logs should stay at INFO and proxy logs must exclude credentials and bodies.

Real WebUI/LiteLLM login, refresh, consent, Conditional Access, metadata forwarding, gateway-header stripping, container deployment, and Graph access remain pending until run against your tenant. No registration, message, file, or cluster was modified by the local tests.
