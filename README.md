# Microsoft Graph MCP

An internal Streamable HTTP MCP server designed for Open WebUI and LiteLLM, with delegated Microsoft Graph access and planned file transfers to an Open Terminal workspace.

**Status: authentication scaffold implemented and locally tested. Live Entra/LiteLLM/WebUI integration is pending. No app registration or cluster deployment has been created.**

Implemented: tenant-specific Entra JWT validation, delegated OBO through MSAL, bounded per-assertion token caching, OAuth resource metadata, health endpoints, and `graph_read` for selected `/me` profile fields. `graph_capabilities` and `graph_describe` expose this initial coverage.

File transfers, other Microsoft 365 operations, full read/write coverage, artifact cleanup, and terminal document rendering remain planned. The agreed 250 MB and visual document workflows are not implemented yet.

## Run the first slice

Follow the [Entra, server, LiteLLM, and WebUI setup guide](docs/setup.md). Start with delegated `User.Read` and prove the connection before adding more permissions.

```sh
uv sync --locked --no-editable
cp .env.example .env
# Replace example settings before starting the server.
uv run --no-editable uvicorn ms_graph_mcp.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

Local verification:

```sh
uv run --no-editable pytest -q
uv run --no-editable ruff check src tests
uv run --no-editable ruff format --check src tests
```

Dependencies are locked in `uv.lock` for Python 3.12. A non-root [Dockerfile](Dockerfile) and [LiteLLM example](deploy/litellm.example.yaml) are included. Tests replace Microsoft network calls; they do not establish real tenant access.

## Deploy on Kubernetes

Use the [Helm chart](charts/ms-graph-mcp/values.yaml) and [Kubernetes/manual build guide](docs/kubernetes.md). Replicas default to **two** and are configurable at installation. The chart includes node spread, a disruption budget, rolling updates, private Service routing and existing Secret references. Kubernetes 1.30+ is required by the default topology configuration.

Enterprise CA bundles can be mounted from an existing ConfigMap or Secret; the application adds them to public trust for Graph, Entra OBO and signing-key retrieval. Manual builds support Harbor/GHCR, amd64/arm64, optional build CA trust and mirrored base images. CI is optional. Container builds, image publication and live cluster HA verification remain pending.

## Read the specification

- [Architecture](docs/architecture.md): requirements, authentication, tools, response handling, transfers, and deployment boundaries.
- [Service coverage and permissions](docs/service-coverage.md): intended operations, delegated scope candidates, and API limitations.
- [Terminal and document contract](docs/terminal-contract.md): document tooling, templates, image feedback, and style preservation.
- [Acceptance and validation](docs/acceptance.md): compatibility gates and evidence required before declaring the system usable.

The design uses Open WebUI 0.11.3, LiteLLM 1.100.0, and Open Terminal 0.11.34 as the initial compatibility baseline. Runtime/package versions supplied by the user are inputs, not installation results.

The proposed default is a separate Microsoft connection in Open WebUI, LiteLLM `oauth_delegate` routing, and an Entra OBO exchange in the Graph MCP. This avoids coupling Graph access to Open WebUI's existing SSO registration.

Document creation and rendering run in the terminal. The Graph MCP remains a small gateway with explicit controls over returned content and temporary file transfer.
