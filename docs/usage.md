# Microsoft 365 tools

The server exposes eight MCP tools for delegated Microsoft 365 access. Mail, shared mailboxes, calendars, contacts, Outlook settings, Teams/chats, meetings and insights, files, SharePoint, Planner, To Do, Excel, OneNote, presence, people and basic directory discovery are available through the route catalog. Graph consent, user rights and service eligibility determine which calls succeed.

## Discover and use operations

| Tool | Purpose |
| --- | --- |
| `graph_capabilities` | Service families, operation counts and actual implementation limits |
| `graph_describe` | Search route templates and permission guidance by `service`, `query`, `path` or `method`; page with `offset` and `limit` |
| `graph_read` | Cataloged GET operations and explicitly read-only POST searches/availability queries |
| `graph_write` | Cataloged mutations, including creating drafts, sending, editing, deleting and sharing |
| `graph_continue` | Follow a returned continuation/delta handle as the same user |
| `graph_prepare_transfer` | Prepare native drive downloads and resumable uploads up to 250,000,000 bytes |
| `graph_transfer_status` | Inspect a native transfer or check a completed upload's destination drive and size |
| `graph_transfer_manage` | Cancel a native upload session at Microsoft |

Connect Microsoft in WebUI, then call `graph_capabilities` and `graph_describe(service="mail")`. Availability is reported as `supported_unverified` because exposing an operation does not establish your consent or resource access. The server does not enumerate licenses or grant permissions.

Use Graph-relative paths without `/v1.0` or a hostname. Put query options in `query` and JSON payloads in `body`. OneNote page creation instead takes `html` on `graph_write`; OneNote content PATCH takes an array of command objects in `body`. Discovery reports `body_format`. See [OneNote/presence examples](rollout-0.3.0.md). Query values are strings. Supported headers are `Prefer`, `ConsistencyLevel`, `If-Match`, `If-None-Match` and `workbook-session-id`; authentication headers cannot be supplied by callers. Unknown routes and writes through `graph_read` are rejected before Graph is called.

### Read examples

Profile (the previous interface still works):

```json
{"path":"/me","select":["displayName","userPrincipalName"]}
```

Recent mail, using `graph_read`:

```json
{"path":"/me/messages","query":{"$top":"10","$select":"id,subject,from,receivedDateTime,bodyPreview","$orderby":"receivedDateTime desc"}}
```

Calendar occurrences in a time window:

```json
{"path":"/me/calendarView","query":{"startDateTime":"2026-09-14T00:00:00Z","endDateTime":"2026-09-21T00:00:00Z","$top":"20"}}
```

Drive discovery: `{"path":"/me/drive"}`. Files: `{"path":"/drives/DRIVE_ID/root/children"}`. User tasks: `{"path":"/me/planner/tasks"}` or discover To Do lists with `{"path":"/me/todo/lists"}`. Teams: `{"path":"/me/joinedTeams"}`.

Microsoft Search is a read even though it uses POST:

```json
{"path":"/search/query","method":"POST","body":{"requests":[{"entityTypes":["driveItem"],"query":{"queryString":"quarterly report"},"from":0,"size":10}]}}
```

Use `graph_describe` for the specific route before constructing an operation in another service. Permission strings are guidance, not an applied Entra manifest or a promise of minimum permissions for every field. [Graph search](https://learn.microsoft.com/en-us/graph/api/resources/search-api-overview?view=graph-rest-1.0)

### Write examples

Create a draft with `graph_write`:

```json
{"method":"POST","path":"/me/messages","body":{"subject":"Draft for review","body":{"contentType":"Text","content":"Draft text"}}}
```

Send the chosen draft with `POST /me/messages/MESSAGE_ID/send`. Sending is an actual side effect; perform it only when the user requests it. A 202 result means accepted for processing, not delivered. [Graph send semantics](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0)

Planner updates use a current `If-Match` eTag. Excel operations can carry `workbook-session-id`. Preserve Graph IDs, recurrence/timezone settings and conflict behavior rather than inventing replacements. Generic writes affect the explicit destination; copy a workbook first when the requested workflow should preserve the original.

## Pagination and results

Tools return one JSON text block with status, Graph data and request ID. Large collections are not traversed automatically. Pass `page.continuation` to `graph_continue`; `delta` handles support later delta reads where the catalog permits them. Expanded collections can include `mcp_next`/`mcp_delta` handles. Microsoft Search uses its own body `from`/`size` pagination when no next link is returned.

Handles are encrypted, bound to tenant/user/client and expire after one hour. Replicas sharing the same tenant/client credentials can use them without sticky sessions. Credential rotation invalidates existing handles. The caller must still authenticate on every use; handles contain no Graph bearer token.

Raw download/upload capability URLs are omitted from ordinary results. The transfer preparation tool returns them deliberately. Keep those URLs out of final answers and logs.

The inline response ceiling is 2 MB; oversized responses return an explicit error, never a successful truncated response. Reduce page size/fields or use a native drive transfer. There is no shared artifact staging service yet. Message MIME and transcript text can be retrieved inline within the ceiling; binary attachments/recordings without native drive URLs require a separate transfer implementation. This is not a claim of full Graph API or full document-workflow coverage.

## Native file transfers

Download arguments for `graph_prepare_transfer`:

```json
{"direction":"download","path":"/drives/DRIVE_ID/items/ITEM_ID"}
```

Upload a new file into a parent folder (name collisions rename the new file):

```json
{"direction":"upload","path":"/drives/DRIVE_ID/items/PARENT_ID","filename":"updated-report.docx","size":123456}
```

To replace an existing item, omit `filename`, use its item path and supply its current `etag`. The terminal uploads bytes to the returned URL in sequential Content-Range chunks; the suggested 10 MiB chunk is a multiple of 320 KiB. **Never send a Graph bearer token to a preauthenticated storage URL.** Preserve the final driveItem response, then use `graph_transfer_status` with its item path to check destination drive and size. This check is not a content-hash comparison or proof that the item came from that session. [Upload protocol](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0)

`graph_transfer_manage` cancels upload sessions. Download URLs cannot be revoked by this server. Provider expiry and the one-hour handle lifetime are separate. Extension/release of staged artifact retention is not implemented. Word/PowerPoint content editing and document rendering remain the terminal's responsibility.

## Errors

- `graph_access_denied`: check the operation's delegated permissions and the user's rights. A 403 alone does not prove a missing license.
- `graph_precondition_failed`: read the latest eTag and reconcile edits.
- `graph_throttled`: wait the returned delay; no automatic retry is performed.
- `write_outcome_unknown`: inspect the destination before retrying a mutation.
- `invalid_operation`: use the catalog; arbitrary URLs, administration, beta and batch requests are excluded.

An MCP HTTP 200 can contain `isError: true`. Authentication/consent failures may occur before tool execution. Use request IDs for troubleshooting and never include tokens or private content in logs. See [setup](setup.md) and [deployment](kubernetes.md).
