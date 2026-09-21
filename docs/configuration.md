# Configuration reference

The server loads `GRAPH_MCP_` environment variables and an optional `.env` file in its working directory. Environment variables take precedence over `.env`. Use [.env.example](../.env.example) for local development and [deploy/values.example.yaml](../deploy/values.example.yaml) for Helm. Examples contain placeholders that must be replaced before startup or deployment.

## Application settings

List-valued environment variables use JSON arrays, such as `["127.0.0.1:8000"]`. In Helm, provide ordinary YAML lists; the chart serializes them for the application.

| Environment variable | Helm value or source | Default / requirement |
| --- | --- | --- |
| `GRAPH_MCP_TENANT_ID` | `config.tenantId` | Required tenant UUID |
| `GRAPH_MCP_CLIENT_ID` | `config.clientId` | Required MCP API application UUID |
| `GRAPH_MCP_CLIENT_SECRET` | Existing Secret named by `credentials.existingSecret`, key `credentials.clientSecretKey` | Required nonempty credential; default Secret key is `client-secret` |
| `GRAPH_MCP_ALLOWED_CLIENT_IDS` | `config.allowedClientIds` | Required nonempty list of allowed OAuth-client UUIDs |
| `GRAPH_MCP_RESOURCE_URL` | `config.resourceUrl` | Required public HTTPS MCP URL, including a path; no trailing slash, query, fragment or user credentials |
| `GRAPH_MCP_SCOPE_NAME` | `config.scopeName` | `access_as_user` |
| `GRAPH_MCP_ALLOWED_HOSTS` | Generated Service DNS names plus `config.extraAllowedHosts` | Required nonempty list outside Helm; exact `host[:port]` values, no wildcards |
| `GRAPH_MCP_ALLOWED_ORIGINS` | `config.allowedOrigins` | `[]`; accepts absent Origin and rejects an Origin not on the list; configured entries must be exact HTTPS origins |
| `GRAPH_MCP_HTTP_TIMEOUT_SECONDS` | `config.httpTimeoutSeconds` | `15`; range 1–120 seconds for Microsoft network operations; not a complete tool-call deadline |
| `GRAPH_MCP_OBO_CACHE_ENTRIES` | `config.oboCacheEntries` | `128`; range 1–4096; bounds successful-token and sanitized-failure caches separately, per replica |
| `GRAPH_MCP_OBO_MAX_CONCURRENT_EXCHANGES` | `config.oboMaxConcurrentExchanges` | `4`; range 1–32 distinct simultaneous exchanges, per replica |
| `GRAPH_MCP_EXTRA_CA_FILE` | Generated mount from `enterpriseCA` | Unset; optional existing PEM file that augments public roots for outbound Microsoft TLS |
| `GRAPH_MCP_MAX_REQUEST_BODY_BYTES` | `config.maxRequestBodyBytes` | `60000`; range 60,000–2,000,000; maximum outbound compact UTF-8 JSON or HTML payload |
| `GRAPH_MCP_SHAREPOINT_SITE_URL` | `config.sharepointSiteUrl` | Unset/empty disables site settings; otherwise one HTTPS `tenant.sharepoint.com` site URL with no credentials, port, query or fragment; path segments use ASCII letters, digits, `.`, `_`, `~`, `-` and cannot be `.` or `..` |

For the public URL `https://litellm.example.com/msgraph/mcp`, use that entire URL as `resourceUrl`. The incoming Entra token's audience is still the MCP API's client UUID. The local `/mcp` endpoint and the public gateway URL have different roles; see the [resource mapping](setup.md#2-resource-and-token-mapping).

The application validates settings at startup. Changing environment variables, credentials or CA files requires a process restart; use a rolling restart on Kubernetes. Keep real credentials in `.env` only for local development or in your deployment's secret management system. Never put secret values into Helm values files.

## Deployment settings

| Helm value | Default | Meaning |
| --- | --- | --- |
| `transportSecurity.mode` | `tls` | Uvicorn serves HTTPS. `mesh` is the only alternative and requires an existing enforced mesh mTLS policy. |
| `transportSecurity.existingSecret` | Empty; required in TLS mode | Existing TLS Secret with `tls.crt` and `tls.key` for the Service DNS name used by LiteLLM |
| `networkPolicy.enabled` | `true` | Restricts incoming connections to configured peers; the CNI must support and enforce it |
| `networkPolicy.ingressFrom` | `[]`; explicit peers required when enabled | Gateway pod/namespace selectors, plus ingress-controller peers if used for metadata |
| `replicaCount` | `2` | At least two replicas; node-spread defaults require two eligible nodes |
| `service.port` | `8000` | Internal Service port; container port remains 8000 |
| `image.tag` / `image.digest` | Empty | Supply a built image reference; digest takes precedence over tag |
| `enterpriseCA.enabled` | `false` | Mounts additional outbound CA trust from exactly one existing ConfigMap or Secret |
| `metadataIngress.enabled` | `false` | Optional direct route for the path-specific OAuth metadata document; does not expose `/mcp` |

TLS mode requires LiteLLM to verify the MCP certificate's issuer and DNS name. `enterpriseCA` configures the MCP's outgoing connections and does not configure LiteLLM's trust. Metadata ingress also needs controller-specific backend TLS and certificate-verification settings. See the [Kubernetes guide](kubernetes.md).

## Fixed authentication limits

Signing-key sets have a 300-second lifetime and a global 30-second refresh cooldown per replica. Failed OBO exchanges have a ten-second backoff per assertion. These durations are currently fixed in the application. Expired signing keys are not trusted after a failed refresh.

Requests sharing an assertion share one exchange; cached tokens are served without waiting for other exchanges. When the distinct-exchange limit is full, additional misses receive HTTP 503 with `Retry-After: 10`. Raising concurrency increases upstream work, so size it with your gateway's request limits and expected cold-start traffic.

Incoming bearer tokens are limited to 32 KiB. Selected profile responses retain a 64 KiB guard. The MCP request envelope is bounded at six times `maxRequestBodyBytes` plus 65,536 bytes, allowing JSON escaping and RPC/query/header overhead; outbound JSON/HTML still has its own smaller bound. These limits do not govern native file transfers. See [security controls](security.md) for cancellation, cache-isolation and failure behavior.

When SharePoint site settings are configured, OBO token and failure caches and exchange-concurrency budgets apply independently to Graph and SharePoint. The SharePoint origin is derived only from the configured site URL. Optional SharePoint consent failure does not prevent Graph tools from running. See [SharePoint setup and limitations](sharepoint-publishing.md).
