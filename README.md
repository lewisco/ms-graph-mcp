# Microsoft Graph MCP

An internal Streamable HTTP MCP server designed for Open WebUI and LiteLLM, with delegated Microsoft Graph access and planned file transfers to an Open Terminal workspace.

**Status: delegated Microsoft 365 tools implemented; the user has verified the WebUI → LiteLLM → MCP connection. Expanded service operations require work-side consent and live validation.**

Eight MCP tools expose cataloged reads/writes for mail, calendars, contacts, Teams/chats, meetings, files, SharePoint, tasks, Excel, people and basic directory discovery, plus owner-bound pagination and native drive transfers. Use `graph_capabilities` and `graph_describe` to discover the deployed catalog.

Native drive transfers target 250 MB. Shared staging for large transcripts/JSON, non-drive binary transfers, document editing/rendering and full workflow acceptance remain incomplete. Tenant administration, beta and batch APIs are excluded. See the [tool guide](docs/usage.md) for exact limits.

## Documentation

- [User guide](docs/usage.md): connect Microsoft, use the available tools, and understand errors.
- [Version 0.2.0 rollout](docs/rollout-0.2.0.md): rebuild, deploy, consent and refresh tool discovery.
- [Setup guide](docs/setup.md): configure Entra registrations, the server, LiteLLM, and Open WebUI.
- [Configuration reference](docs/configuration.md): environment variables, Helm settings, and security limits.
- [Kubernetes deployment](docs/kubernetes.md): build images, configure certificates and gateway access, and install or upgrade.
- [Security controls](docs/security.md): authentication protections and deployment verification.

## Run the server

Follow the [Entra, server, LiteLLM, and WebUI setup guide](docs/setup.md). Verify `/me`, then configure delegated consent for the intended services.

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

Dependencies are locked in `uv.lock` for Python 3.14. A non-root [Dockerfile](Dockerfile) and [LiteLLM example](deploy/litellm.example.yaml) are included. Tests replace Microsoft network calls; they do not establish real tenant access.

## Deploy on Kubernetes

Use the [Helm chart](charts/ms-graph-mcp/values.yaml) and [Kubernetes/manual build guide](docs/kubernetes.md). Replicas default to **two** and are configurable at installation. The chart includes node spread, a disruption budget, rolling updates, private Service routing and existing Secret references. Kubernetes 1.30+ is required by the default topology configuration.

Helm enables gateway ingress restrictions and HTTPS by default. Supply the actual LiteLLM pod/namespace selectors and an existing Service TLS Secret; missing values fail chart rendering. An existing mesh with enforced mTLS can use `transportSecurity.mode: mesh`. See the [security controls and verification notes](docs/security.md).

Enterprise CA bundles can be mounted from an existing ConfigMap or Secret; the application adds them to public trust for Graph, Entra OBO and signing-key retrieval. Manual builds support Harbor/GHCR, amd64/arm64, optional build CA trust and mirrored base images. CI is optional. Local container builds have been verified; image publication and live cluster HA verification remain pending.

## Read the specification

- [Architecture](docs/architecture.md): requirements, authentication, tools, response handling, transfers, and deployment boundaries.
- [Service coverage and permissions](docs/service-coverage.md): intended operations, delegated scope candidates, and API limitations.
- [Terminal and document contract](docs/terminal-contract.md): document tooling, templates, image feedback, and style preservation.
- [Acceptance and validation](docs/acceptance.md): compatibility gates and evidence required before declaring the system usable.

The design uses Open WebUI 0.11.3, LiteLLM 1.100.0, and Open Terminal 0.11.34 as the initial compatibility baseline. Runtime/package versions supplied by the user are inputs, not installation results.

The proposed default is a separate Microsoft connection in Open WebUI, LiteLLM `oauth_delegate` routing, and an Entra OBO exchange in the Graph MCP. This avoids coupling Graph access to Open WebUI's existing SSO registration.

Document creation and rendering run in the terminal. The Graph MCP remains a small gateway with explicit controls over returned content and temporary file transfer.

## Hardened runtime and release gate

The Dockerfile uses DHI Python 3.14 on Debian 13, with separate development and runtime stages. The [manual release gate](scripts/release_image.py) builds one platform, scans the final image with a freshly downloaded Trivy database, and permits publication only with zero findings outside the reviewed, expiring DHI OS baseline. All Python findings block release; `--strict` restores the zero-unfiltered-findings requirement. It checks OS/Python scan coverage and records scan, database and image evidence. See [build and release instructions](docs/kubernetes.md). No CI service is required.

The local amd64 Python 3.14.7 build and restricted runtime smoke passed on 2026-09-10 using runtime digest `sha256:ee0154c1c675e1f51f361c239061128719c06cc7130c6fae0a8362b0ba777267`. Docker Scout reported zero vulnerabilities with full build provenance and SBOM. Trivy 0.74.0 reported 47 unfiltered OS-package findings and no Python-package findings; with the DHI VEX repository it retained 14 findings and suppressed 33. The strict mode remains blocked; the active Trivy-only policy accepts the exact known OS baseline. No image has been published. See [validation evidence](docs/acceptance.md).
