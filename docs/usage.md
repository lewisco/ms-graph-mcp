# User guide

Microsoft Graph MCP currently reads selected fields from your own Microsoft profile. It cannot read mail, messages, calendars or files, and cannot create, edit, send or delete anything. Those features in the architecture documents are planned coverage.

Your administrator must first complete the [setup guide](setup.md) and verify your deployment's Open WebUI → LiteLLM → Microsoft connection. The repository's local tests do not establish that a live connection is ready.

## Connect and check your identity

1. Use your organization's configured Microsoft-tools connection in Open WebUI and complete the Microsoft sign-in/consent flow. This connection is separate from signing into Open WebUI itself.
2. In a chat with that connection available, ask: “Use graph_capabilities to show which Microsoft operations are available.”
3. Ask: “Use graph_read to show my Microsoft display name and sign-in name.” Check that the returned profile belongs to the account you connected.

All three tools below require the Microsoft connection. Authentication includes a delegated token exchange, so missing consent can prevent tool discovery and capability calls as well as profile reads. Your administrator configures the MCP API scope and delegated Graph `User.Read` permission.

## Available tools

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `graph_capabilities` | Lists implemented operations; currently `GET /me` | None |
| `graph_describe` | Describes `/me`, its permission, and default fields | Optional `path`, which must be `/me` |
| `graph_read` | Reads selected fields from your connected account's profile | Optional `path`, `method`, and `select` as shown below |

Example `graph_read` arguments for the default profile:

```json
{"path":"/me"}
```

The defaults return `id`, `displayName`, `mail`, and `userPrincipalName` when present. To request fewer fields:

```json
{"path":"/me","method":"GET","select":["displayName","userPrincipalName"]}
```

Supported selections are `id`, `displayName`, `givenName`, `surname`, `mail`, `userPrincipalName`, `jobTitle`, `officeLocation`, and `preferredLanguage`. Duplicate field names are removed. Omitting `select`, setting it to `null`, or using an empty list selects the defaults. A field can be missing or null in Microsoft's response; the server does not invent a value.

Paths other than `/me`, methods other than `GET`, arbitrary URLs, and unsupported fields are rejected. To change which person's profile is read, reconnect the correct Microsoft account; this tool has no argument for selecting another user.

## Results and errors

A successful read returns one JSON text block containing `status`, `data`, `selection`, `delivery`, and `request_id`. `data` contains the requested profile fields. There is no duplicate `structuredContent` payload.

Authentication failures are HTTP errors before tool processing. A failure from Graph is returned as an MCP tool error with `isError: true`; its JSON text contains the upstream status and a sanitized error code. A transport HTTP 200 therefore does not by itself mean the Graph read succeeded.

| Symptom | Next action |
| --- | --- |
| HTTP 401 / `invalid_token`, or a reconnect message | Reconnect Microsoft. If it persists, ask the administrator to check token audience, tenant, client, expiry, consent and sign-in policy. A newly rotated signing key can require a retry after the 30-second refresh cooldown. |
| HTTP 403 / `insufficient_scope` | Ask the administrator to check consent for the MCP API's delegated scope. |
| HTTP 503 / `temporarily_unavailable` | Follow `Retry-After` (currently 10 seconds). The server may be busy or unable to reach Microsoft. Avoid repeated immediate retries. |
| HTTP 503 / `configuration_error` | Ask the administrator to check the MCP app registration, credential and Graph consent. |
| `graph_authentication_required` | Retry the read once; reconnect Microsoft if it persists. |
| `graph_access_denied` | Ask the administrator to check delegated `User.Read` consent and access policy. |
| `graph_throttled` | Wait `retry_after_seconds` if supplied; otherwise retry later. The server does not automatically retry. |
| Invalid arguments / unsupported operation | Use only `/me`, `GET`, and the supported profile fields above. |

For certificate failures, a connection timeout, or a rejected Host/Origin header, ask the administrator to follow the [deployment troubleshooting](kubernetes.md#7-troubleshooting) guide. Share the error code and `request_id` when available; omit bearer tokens, client secrets and unnecessary personal profile data.
