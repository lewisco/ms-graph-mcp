# Service coverage and delegated permissions

Status: service scope and permission guidance for the implemented route catalog, not an applied Entra manifest. The executable catalog is `src/ms_graph_mcp/catalog.py`; native drive transfers are implemented, while shared artifact staging and full workflow acceptance remain pending. All scopes below are **delegated** unless explicitly discussing an excluded application-only API. No service account or application-permission fallback is proposed.

The user's Microsoft access and the app's consent both matter. Broadly named delegated scopes do not grant access beyond the signed-in user's resource rights. The exact operation catalog must bind each supported route to Microsoft's current permission table and a fixture. Scope candidates may overlap; remove redundant scopes where the endpoint tables establish that a granted scope is sufficient. [Permission model](https://learn.microsoft.com/en-us/entra/identity-platform/permissions-consent-overview)

## 1. Coverage map

| Area | Intended operations and important paths | Delegated scope candidates | Qualification |
| --- | --- | --- | --- |
| Own mail | Messages, folders, search, drafts, attachments, reply/forward/send, move, delete under `/me/messages` and `/me/mailFolders` | `Mail.ReadWrite`, `Mail.Send` | Read/write mail permission does not itself authorize sending |
| Shared mail | Equivalent supported operations under `/users/{mailbox-id}/messages` and mailbox folders | `Mail.ReadWrite.Shared`, `Mail.Send.Shared` | Mailbox delegation and Send As/Send on Behalf rights remain necessary |
| Calendar | Calendar discovery, calendar view, events, recurrence, availability, invites, responses, cancellation | `Calendars.ReadWrite`, `Calendars.ReadWrite.Shared` | Prefer calendar view for occurrences in a date window; preserve timezone and recurrence semantics |
| Contacts | Contact folders, contacts, create/edit/delete | `Contacts.ReadWrite`, `Contacts.ReadWrite.Shared` when shared contacts are used | Separate from relevant-people and directory search |
| Outlook settings | Rules, categories, automatic replies, timezone/preferences where Graph exposes them | `MailboxSettings.ReadWrite` where required | Verify each settings route; not every Outlook setting has an API |
| Teams discovery | `/me/joinedTeams`, `/teams/{id}/channels`, channel metadata | `Team.ReadBasic.All`, `Channel.ReadBasic.All` | Shared/private channel membership may alter what appears |
| Teams chats | Chat discovery/creation, messages, replies, edits, supported deletes and reactions | `Chat.ReadWrite`, `Chat.Create`, `ChatMessage.Send` as required by route | Content edits/deletes remain subject to ownership and service policy |
| Teams channels | Messages, replies, sending, supported edits/deletes | `ChannelMessage.Read.All`, `ChannelMessage.Send`, `ChannelMessage.ReadWrite` | Do not use legacy group-wide scopes merely as a shortcut |
| Meeting metadata | Resolve meeting from calendar/join URL, read supported settings, create/update supported meetings | `OnlineMeetings.ReadWrite` | Calendar events and onlineMeeting objects have distinct identifiers |
| Meeting transcripts | List transcript artifacts and retrieve content | `OnlineMeetingTranscript.Read.All` | Hosted and attended meetings where API/resource access permits |
| Meeting recordings | List/retrieve recording artifacts where available | `OnlineMeetingRecording.Read.All` | Large recordings can exceed the initial 250 MB target; report that explicitly |
| Copilot meeting insights | List and read `/copilot/users/{userId}/onlineMeetings/{id}/aiInsights` | `OnlineMeetingAiInsight.Read.All` | Optional per-user feature; Copilot license and API eligibility required |
| OneDrive and libraries | Find, list, download, upload, create folders, copy/move, rename, delete, versions, supported sharing | `Files.ReadWrite.All`; retain `Files.ReadWrite` where workbook endpoint tables require it | Sharing operations can grant access; preserve their explicit semantics |
| SharePoint | Site discovery, drives, lists/list items, pages and supported publishing | `Sites.ReadWrite.All` | Graph page APIs support a subset of page/web-part capabilities |
| Planner basic | User tasks, plans, buckets, assignments, task details, checklist/references and updates | `Tasks.ReadWrite` | Discovery may need additional group-read access; verify chosen path before adding a scope |
| To Do | Lists, tasks, checklist items, linked resources and supported task attachments | `Tasks.ReadWrite` | Respect the actual To Do endpoint model rather than reusing Planner payloads |
| Excel | Workbook sessions, worksheets, ranges, tables, formulas, charts and supported calculations | `Files.ReadWrite` | Sessions and operations have endpoint-specific requirements |
| Word/PowerPoint | File operations through drives; document content editing in terminal | File/library scopes above | No invented paragraph/slide-editing Graph endpoints |
| Me/People | `/me`, relevant people, basic coworker lookup | `User.Read`, `People.Read`, `User.ReadBasic.All` | Own-profile edits can add `User.ReadWrite`; broader directory administration is outside scope |

Mail/shared-mail/settings/contact/profile scope meanings are documented in the [Graph permission reference](https://learn.microsoft.com/en-us/graph/permissions-reference). This map groups proposed workflows; it is not a claim that each candidate is needed for every operation.

Endpoint-specific references for the other groups:

- Teams: [discover teams](https://learn.microsoft.com/en-us/graph/api/user-list-joinedteams?view=graph-rest-1.0), [discover channels](https://learn.microsoft.com/en-us/graph/api/channel-list?view=graph-rest-1.0), [send chat messages](https://learn.microsoft.com/en-us/graph/api/chat-post-messages?view=graph-rest-1.0), [send channel messages](https://learn.microsoft.com/en-us/graph/api/channel-post-messages?view=graph-rest-1.0), [edit messages](https://learn.microsoft.com/en-us/graph/api/chatmessage-update?view=graph-rest-1.0), [soft delete](https://learn.microsoft.com/en-us/graph/api/chatmessage-softdelete?view=graph-rest-1.0).
- Meetings: [resolve onlineMeeting](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-get?view=graph-rest-1.0), [list transcripts](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0), [read transcript](https://learn.microsoft.com/en-us/graph/api/calltranscript-get?view=graph-rest-1.0), [AI Insights permissions](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/meeting-insights/onlinemeeting-list-aiinsights).
- Files/pages: [file model](https://learn.microsoft.com/en-us/graph/api/resources/onedrive?view=graph-rest-1.0), [create a page](https://learn.microsoft.com/en-us/graph/api/sitepage-create?view=graph-rest-1.0).
- Tasks: [Planner create](https://learn.microsoft.com/en-us/graph/api/planner-post-tasks), [Planner list](https://learn.microsoft.com/en-us/graph/api/plannerplan-list-tasks?view=graph-rest-1.0), [To Do lists](https://learn.microsoft.com/en-us/graph/api/todo-post-lists?view=graph-rest-1.0).
- Excel: [workbook API](https://learn.microsoft.com/en-us/graph/api/resources/excel?view=graph-rest-1.0), [session permissions](https://learn.microsoft.com/en-us/graph/api/workbook-createsession?view=graph-rest-1.0).

Core consent should cover the agreed ordinary workflows. Optional meeting-artifact and Copilot features should be separately identifiable in configuration and capability reporting. Do not request directory license-enumeration permissions just to preflight Copilot availability.

## 2. Graph semantics to preserve

### Outlook

Expose full supported actions, including sending, with clear read/write tool classification. Do not infer that a message was delivered just because send was accepted. Preserve shared-mailbox identities, delegated sender semantics, attachments, original message IDs, timezones, and recurrence.

Use metadata/preview for search and list results; retrieve bodies deliberately. Retain raw HTML or MIME when relevant, with optional explicit plain-text views. Microsoft supports message-body formatting preferences on applicable operations. [Get message](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0)

For shared mailbox search, choose endpoint-specific search/filter paths. Do not promise that Microsoft Search queries every shared mailbox identically to the signed-in mailbox. Draft+attachment+send is a workflow with observable intermediate state, not a transaction.

### Teams and meetings

Meeting context can come from four distinct resource types: calendar event, onlineMeeting, meeting chat, and stored files. Resolve and retain their IDs separately. A join URL is useful for lookup but is not itself an onlineMeeting ID. Calendar discovery of an attended meeting does not prove artifact access.

Retrieve artifacts by availability and the user's request. Do not always retrieve chat history, recordings, full transcripts, and Copilot notes together. Use calendar metadata to disambiguate recurring occurrences, then select the relevant transcript/insight by call and time. Distinguish source timestamps from retrieval timestamps.

The following are separate content sources:

| Source | Access path | Content label |
| --- | --- | --- |
| Transcript | Graph transcript API or accessible stored file | Original transcript |
| Microsoft meeting insights | Copilot AI Insights API | Microsoft-generated summary/action items |
| Summary pasted into chat | Chat message APIs | Shared message, with author/date |
| Summary shared through Outlook | Mail APIs | Shared email, with sender/date |
| Notes/shared document | Accessible file or page | Document/page, with provenance |

Recap UI placement does not imply ordinary chat-message storage. Microsoft documents recap features in Teams calendar/chat, while recordings and transcripts use OneDrive/SharePoint storage. [Recap behavior](https://learn.microsoft.com/en-us/microsoftteams/intelligent-recap-calls-meetings), [artifact storage](https://learn.microsoft.com/en-us/microsoftteams/tmr-meeting-recording-change)

Microsoft's AI Insights API requires a Copilot license for the accessing user. It does not currently support channel meetings, and insights may arrive hours after a meeting. The endpoint documentation includes v1.0 examples, but also retains beta warning text; verify the intended route in the target tenant and record that documentation inconsistency. [Meeting Insights](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights), [endpoint reference](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/meeting-insights/onlinemeeting-list-aiinsights)

A user without Copilot continues using core tools. A failed optional request should identify the observed failure without turning all 403s into “no license.” Capability states are `supported_unverified`, `available`, `unavailable`, and `unknown`, with reason/source/time. Missing scope, no artifact, unsupported meeting, processing delay, resource denial, and license restriction are different states. Do not claim “license missing” unless the response or independently authorized evidence establishes it.

A transcript that was never retained is not recoverable merely because a summary exists. Tenant policies can also disable Graph transcript access. Preserve those outcomes instead of fabricating a transcript or falling back to an application token. [Transcript constraints](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0)

### Search

Use Graph resource-specific query options and Microsoft Search where appropriate. Preserve entity-specific limitations, matching snippets, and source IDs. Microsoft Search does not support arbitrary mixtures of entity types; Teams message search is a separate query and its count semantics are not necessarily a total across all matches. [Search API](https://learn.microsoft.com/en-us/graph/api/resources/search-api-overview?view=graph-rest-1.0), [Teams search](https://learn.microsoft.com/en-us/graph/search-concept-chat-messages)

Do not implement an internal semantic index as part of this gateway. Supported query syntax should be discoverable through `graph_describe`, including which endpoint supports `$search` versus `$filter`.

### Tasks and Office files

Only basic Planner plans are required. Premium plans are an explicit API gap, not a reason to add Dataverse integration now. Planner edits need current eTags and conflict handling. [Planner model](https://learn.microsoft.com/en-us/graph/api/resources/planner-overview?view=graph-rest-1.0)

Excel can use Graph sessions for supported live workbook operations or terminal file editing. Choose deliberately per task. An operation performed in a persistent Graph session writes to that workbook; to preserve the document-copy default, copy the workbook first unless the user requested an in-place edit. Local Python libraries do not substitute for Excel calculation; read formula and cached-value state separately.

Word/PowerPoint editability and rendering are a [terminal contract](terminal-contract.md). Graph moves files and manages their Microsoft 365 identity, sharing, and versions.

## 3. Explicit coverage boundaries

Not assumed in the initial scope: Planner premium/Dataverse, tenant/user/group administration, compliance export/eDiscovery, application-only message export, live meeting media bots, or autonomous background subscriptions/crawling. Team/channel provisioning and membership administration need a concrete workflow and endpoint scope review; ordinary team/channel discovery and content interaction are included.

Protection labels, rights management, passwords, file locks, unsupported Office elements, and foreign-tenant permissions can constrain a specific operation. Report the actual limitation; do not claim that downloadable content can always be edited by the chosen libraries.

Full coverage is measured by the operation fixtures in [acceptance](acceptance.md), not by having a long permissions list. Additional Graph-native operations within the agreed families can be added to the catalog without proliferating top-level MCP tools.
