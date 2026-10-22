# Roll out gateway-managed OAuth and audit logging — 0.5.0

The chart and application versions are 0.5.0. Build and deploy a new immutable image using the existing Trivy release procedure in [0.2.0 rollout](rollout-0.2.0.md). This source change does not itself publish an image or change the work cluster.

## What changed

- **No authentication change.** Tokens minted for LiteLLM's Entra client by `auth_type: oauth2` with `oauth2_flow: authorization_code` already validate; only `azp` differs from the legacy WebUI client. Tests now cover an allowed gateway client, an unlisted client, refreshed tokens, extra scopes and CAE claims. The gateway-managed setup can be adopted on 0.4.0 by adding the client ID to `allowedClientIds`.
- **Optional audit log.** `config.auditLog` (`GRAPH_MCP_AUDIT_LOG`), default `false`, writes one JSON line per Microsoft-facing tool call with user object ID, username, client ID, tool and outcome. See [configuration](configuration.md#audit-logging).
- **Documentation.** [Setup §5.1](setup.md#51-gateway-managed-oauth-default) describes the LiteLLM client registration and `mcp_servers` entry; `oauth_delegate` is documented as legacy.

## Work-side procedure

1. Register or choose LiteLLM's Entra client: Web redirect URI `https://<litellm-host>/callback`, delegated `api://<MCP_CLIENT_ID>/access_as_user` and Graph `offline_access` with admin consent, and a client secret.
2. Add its client ID to `config.allowedClientIds`, keeping the WebUI tools client until every user has moved. Optionally set `config.auditLog: true` with this chart version.
3. Change LiteLLM's `msgraph` entry as in [the example](../deploy/litellm.example.yaml). Leave `issuer` and `upstream_resource` unset.
4. Retire any shared-key per-server msgraph connection in WebUI; users reach LiteLLM with their own identity.
5. Connect Microsoft on LiteLLM's connect page and call `graph_read` with `{"path":"/me"}`. With audit logging on, confirm the line shows your object ID and the LiteLLM client ID.
6. About an hour after connecting, call a tool again without reconnecting. LiteLLM's refresh grant omits `scope`, which Entra documents as required; if refresh fails, users must reconnect hourly until LiteLLM changes it.
7. Once no audit lines show the WebUI tools client, remove it from `allowedClientIds`.
