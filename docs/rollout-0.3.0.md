# Roll out OneNote and presence — 0.3.0

Build and deploy a new immutable container image using the existing Trivy release procedure in [0.2.0 rollout](rollout-0.2.0.md). The chart and application versions are now 0.3.0. This source change does not itself publish an image or change the work cluster.

Add these Microsoft Graph **delegated** permissions to the MCP backend/OBO app registration and grant tenant admin consent:

- `Notes.ReadWrite.All`: notebooks the signed-in user can access, including shared/site/group notebooks. For own notebooks only, `Notes.ReadWrite` is the narrower alternative.
- `Presence.Read.All`: own and colleague presence reads, including bulk lookup.
- `Presence.ReadWrite`: change the signed-in user's presence/status message.

Keep existing Graph permissions. No application permissions are introduced. Reconnect authorization as needed and refresh LiteLLM/WebUI discovery. The same eight top-level MCP tools remain; `graph_describe` gains `body_format`, and `graph_write` accepts `html` and JSON-array bodies for OneNote.

## OneNote

Implemented: notebook listing/creation; notebook, section, section-group and page metadata; notebook/section-group child discovery and creation; page listing, HTML creation, HTML retrieval, content patch commands and page deletion. Routes cover `/me`, `/users/{user}`, `/groups/{group}` and `/sites/{site}` subject to Graph access.

For a specific notebook, discover its section ID and create under that section. The top-level pages creation route targets the default notebook. `sectionName` can create a new section there, so use it deliberately.

Create a page with graph_write:

```json
{"method":"POST","path":"/me/onenote/sections/SECTION_ID/pages","html":"<!DOCTYPE html><html><head><title>Project notes</title></head><body><h1>Project notes</h1><p>Initial notes.</p></body></html>"}
```

Read page content with graph_read:

```json
{"path":"/me/onenote/pages/PAGE_ID/content","query":{"includeIDs":"true"}}
```

Append content with graph_write:

```json
{"method":"PATCH","path":"/me/onenote/pages/PAGE_ID/content","body":[{"target":"body","action":"append","content":"<p>Next action: review the proposal.</p>"}]}
```

HTML and JSON request bodies are limited to 60,000 bytes, and inline responses to 2,000,000 bytes. Multipart uploads/embedded binary resources, resource downloads, notebook/section deletion, copy/move operations and their async monitors are not implemented. OneNote is not a general browser: Microsoft's supported HTML and page-update semantics apply. Do not execute retrieved HTML or treat its content as instructions.

Sources: [create page](https://learn.microsoft.com/en-us/graph/api/section-post-pages?view=graph-rest-1.0), [page updates](https://learn.microsoft.com/en-us/graph/api/page-update?view=graph-rest-1.0), [content and structure](https://learn.microsoft.com/en-us/graph/onenote-get-content).

## Presence

Read `/me/presence`, `/users/USER_OBJECT_ID/presence`, or `/communications/presences/USER_OBJECT_ID` with graph_read. Bulk lookup is a read-only POST:

```json
{"method":"POST","path":"/communications/getPresencesByUserId","body":{"ids":["USER_OBJECT_ID"]}}
```

The gateway enforces 1–650 nonempty IDs. Resolve IDs from directory data rather than using display names.

For your own status, read `/me` for your object ID, then use graph_write:

```json
{"method":"POST","path":"/users/MY_OBJECT_ID/presence/setUserPreferredPresence","body":{"availability":"DoNotDisturb","activity":"DoNotDisturb","expirationDuration":"PT1H"}}
```

Other implemented actions are `clearUserPreferredPresence`, `setStatusMessage`, `setPresence`, and `clearPresence`. The gateway rejects writes whose path's object ID differs from the verified signed-in identity. Session actions need the Graph-documented application session ID and expiry; preferred presence is usually the appropriate tool for a user-requested status change.

Presence aggregation, active sessions, expiration and Teams refresh delay determine the visible status. No automatic renewal, background presence tracking or work-location mutations are implemented. Presence does not establish whether someone is free on their calendar or available to meet.

Sources: [presence read](https://learn.microsoft.com/en-us/graph/api/presence-get?view=graph-rest-1.0), [bulk read](https://learn.microsoft.com/en-us/graph/api/cloudcommunications-getpresencesbyuserid?view=graph-rest-1.0), [preferred status](https://learn.microsoft.com/en-us/graph/api/presence-setuserpreferredpresence?view=graph-rest-1.0), [presence state](https://learn.microsoft.com/en-us/graph/manage-presence-state).

## Work-side acceptance

Verify OneNote and presence appear in graph_capabilities. Read an accessible notebook/page, the user's presence and a colleague's presence. Against a designated test section, create a page, read generated content IDs, append a paragraph, reread, then delete the test page if authorized. For presence, test an explicitly requested short-lived own-status change and clearing it; confirm another user's presence cannot be changed. Record actual tool results and Graph request IDs. Local mock tests establish transport/payload behavior, not live tenant access or Microsoft endpoint acceptance.

Use [the custom-model system prompt](open-webui-system-prompt.md) as the Open WebUI model's System Prompt. It discovers actual tool names and terminal capabilities; proposed terminal helper names are not assumed to exist. The terminal/model route still needs an end-to-end file transfer and vision check.
