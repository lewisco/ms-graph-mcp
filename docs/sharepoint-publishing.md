# SharePoint publishing and 0.4.0 rollout

The MCP supports modern page authoring through Graph and optional site settings through SharePoint REST. Local tests use mocked Microsoft responses. Live BTIG AI Hub access, permissions, page rendering and publication still need work-side acceptance.

## What is implemented

The existing Graph tools now cover typed page listing, create/read/update/delete/publish, horizontal section operations, vertical section operations, column reads, and individual web-part operations. `graph_describe(service="sites")` includes `sharepoint_pages` with supported web parts, request examples and publishing guidance. `graph_describe` with a concrete page path narrows the route listing.

Columns are read through their section; change their arrangement through the section layout. Vertical section creation uses `PUT`. Individual page-level web parts use IDs; nested section/column web parts use position indexes. Only nested web-part creation accepts the optional `index` query parameter. Unsupported methods are rejected locally. [Graph page model](https://learn.microsoft.com/en-us/graph/api/resources/sitepage?view=graph-rest-1.0), [web-part creation](https://learn.microsoft.com/en-us/graph/api/sitepage-create-webpart?view=graph-rest-1.0)

Two additional tools manage one deployment-configured site:

| Tool / operation | Inputs and behavior |
| --- | --- |
| `sharepoint_read`, `settings` | Site metadata and `RootFolder.WelcomePage` |
| `sharepoint_read`, `navigation` | Root nodes at `quicklaunch` or `topnavigationbar`; supply `parent_id` to read a node's children |
| `sharepoint_read`, `themes` | Names of themes returned by SharePoint |
| `sharepoint_write`, `navigation_add` | `title`, `url`, optional `location` and `parent_id`; returns created node ID |
| `sharepoint_write`, `navigation_update` | `node_id` and `title` and/or `url` |
| `sharepoint_write`, `navigation_delete` | `node_id`; deletion is an actual mutation |
| `sharepoint_write`, `apply_theme` | Exact `theme_name` from the theme read; applies its existing palette, without creating a tenant theme |
| `sharepoint_write`, `set_home_page` | `page_path` such as `SitePages/Home.aspx`; verifies the file exists before assigning it |

Navigation URLs must be HTTPS or server-relative paths. The initial operations cover link creation, title/URL edits, and deletion; explicit reordering, moving nodes, audience targeting, global/hub navigation and custom theme creation are not implemented. Navigation collections are limited to 500 nodes at a time; an oversized or continued collection is an error rather than a successful truncated result. Read child collections separately.

## Configuration and permissions

SharePoint publishing remains part of the existing MCP deployment and OpenWebUI connection, alongside the other Microsoft 365 tools. **Microsoft Graph delegated `Sites.Selected`** can be consented on the existing MCP API registration; it does not require a separate MCP. Retain `User.Read` and the permissions required by the other enabled services. An administrator must separately grant that application the `write` role on the AI Hub site collection. Consent alone grants no selected-site access; delegated access also remains bounded by the signed-in user's rights. Use a separate administrative context for the one-time site grant, without adding `Sites.FullControl.All` to the runtime app for provisioning. [Delegated Selected support and assignment](https://devblogs.microsoft.com/microsoft365dev/sharepoint-now-supports-delegated-sites-selected-authentication/), [site grant permissions](https://learn.microsoft.com/en-us/graph/api/site-post-permissions?view=graph-rest-1.0)

The server requests Graph `/.default`. Adding `Sites.Selected` alongside broader grants does not narrow them: Microsoft honors every scope in the token. `Sites.ReadWrite.All`, `Sites.FullControl.All` and `Files.ReadWrite.All` can preserve broader write access; broad read scopes likewise preserve broader reads. Inventory the existing file/site consent before changing it, because removing a grant can break existing document workflows. If broader grants remain necessary, Selected alone cannot enforce an AI Hub boundary: the shared MCP must enforce its publishing restrictions across all routes capable of reaching that content. Mail and calendar grants are separate from the site/file grants being reviewed. Removing a configured API permission is not proof that existing consent or cached tokens have been revoked; verify effective consent and acquire fresh tokens. [Selected consent behavior](https://learn.microsoft.com/en-us/graph/permissions-selected-overview)

**Publishing without broad site-write scopes is a deployment acceptance gate, not a locally verified capability.** Microsoft's page-creation permission table lists `Sites.ReadWrite.All`, without explicitly listing Selected. Test page listing, create, edit, section/web-part changes, publish and asset uploads with the intended effective scopes. Verify that mutations on an ungranted test site are denied; reads there remain expected if `Sites.Read.All` is retained. Include an explicit negative publish test: the publish endpoint separately lists delegated `Files.ReadWrite`, so its permission table alone does not establish a selected-site boundary. Supply the AI Hub site ID directly if broader discovery/search is unavailable. Do not restore broad scopes automatically after a 403. [Create permissions](https://learn.microsoft.com/en-us/graph/api/sitepage-create?view=graph-rest-1.0), [publish permissions](https://learn.microsoft.com/en-us/graph/api/sitepage-publish?view=graph-rest-1.0)

### Current registration review — 2026-09-21

The supplied portal screenshots show the following relevant delegated Graph entries on the existing MCP API registration. They are a configuration snapshot, not an inspection of effective token scopes or all user-specific consent grants.

| Permission | Visible status | Proposed action for the restricted publishing test |
| --- | --- | --- |
| `Files.ReadWrite` | Granted for BTIG | Retain for the user's files; `Files.ReadWrite.All` is not shown. |
| `Sites.Read.All` | Granted for BTIG | Retain the existing broader SharePoint read capability. |
| `Sites.ReadWrite.All` | Listed without a visible grant status | Remove from requested permissions; verify and revoke any existing consent for this scope before acquiring fresh tokens. |
| `Sites.Selected` | Not shown | Add delegated consent and grant this application `write` on AI Hub only. |

The distinction between `Files.ReadWrite` and `Files.ReadWrite.All` matters: Microsoft's OneDrive reference associates the former with the user's OneDrive and the latter with files shared with the user. Do not treat the visible permission as if it were the `.All` grant. Preserve the other service permissions while testing the publishing change; for example, existing OneNote write permissions remain independent and are not restricted by the AI Hub grant. [OneDrive permission reference](https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/permissions_reference?view=odsp-graph-online)

The screenshots also show Agent Tools and Work IQ permissions for separate resource APIs. This implementation requests Graph tokens and, when enabled, SharePoint tokens; it does not request Agent Tools or Work IQ tokens. Those entries do not substitute for the separate SharePoint REST consent discussed below. No SharePoint API permission group is visible in the supplied screenshots.

### Separate SharePoint REST consent

The optional navigation/theme/homepage tools use a SharePoint-audience token inside the same MCP. Graph `Sites.Selected` does not authorize these calls. Microsoft's current SharePoint webhook sample explicitly says `Sites.Selected` is unavailable as a **SharePoint delegated** permission, while documenting application-mode Selected separately. Do not assume Graph delegated support establishes SharePoint REST OBO support. Leave `GRAPH_MCP_SHAREPOINT_SITE_URL` unset while validating the initial Graph permissions. Application-mode SharePoint authentication would require a separate implementation; this server has no such fallback. [Microsoft sample](https://learn.microsoft.com/en-us/samples/azure-samples/azd-functions-sharepoint-webhooks/azd-functions-sharepoint-webhooks/)

The earlier blanket `AllSites.FullControl` recommendation is withdrawn. Evidence for narrower delegated SharePoint grants is operation-specific:

| Operation | Evidence and remaining validation |
| --- | --- |
| Homepage assignment | PnP CLI documents `AllSites.Write` for page updates including homepage promotion. [Command permissions](https://pnp.github.io/cli-microsoft365/cmd/spo/page/page-set/) |
| Navigation | PnP's migration guide says `AllSites.Manage` covers navigation updates; validate the exact add/update/delete calls exposed here. [Migration permissions](https://pnp.github.io/pnpassessment/classic/migrate-minimal-permissions.html) |
| Existing theme application | The site theming REST documentation does not specify a minimum delegated scope. Do not infer one from unrelated tenant theme administration commands. [REST reference](https://learn.microsoft.com/en-us/sharepoint/dev/declarative-customization/site-theming/sharepoint-site-theming-rest-api) |

`AllSites.Write` and `AllSites.Manage` still cover the sites the user can access, rather than selecting AI Hub. The fixed REST destination is a server control, not a site-specific Entra consent. Validate the selected deployment model before enabling these tools with any broader SharePoint grant; the user's site rights also need to permit each action.

Enable optional site settings with:

```dotenv
GRAPH_MCP_SHAREPOINT_SITE_URL=https://tenant.sharepoint.com/sites/AIHub
GRAPH_MCP_MAX_REQUEST_BODY_BYTES=1000000
```

Equivalent Helm values:

```yaml
config:
  sharepointSiteUrl: https://tenant.sharepoint.com/sites/AIHub
  maxRequestBodyBytes: 1000000
```

Replace the example URL with the actual site. Empty/unset `sharepointSiteUrl` disables site-setting execution while keeping the tool schemas discoverable. Graph page tools work independently. The URL supports public-cloud `tenant.sharepoint.com` hosts and simple unescaped ASCII site-path segments; root sites are supported. National clouds and escaped site-path names are not currently configured by this adapter.

The server exchanges the same validated MCP-audience user assertion for `https://tenant.sharepoint.com/.default` only when a site-settings tool runs. Graph and SharePoint use separate OBO instances with isolated caches, failures and concurrency limits. No application-permission fallback or Graph-token forwarding to SharePoint exists. This optional consent does not change the incoming MCP audience or WebUI tools-client scope. The configured site boundary applies to `sharepoint_*`; the existing Graph tools continue to access cataloged resources under their usual delegated permissions.

Allow server egress to the configured SharePoint hostname as well as Graph/Entra. Existing enterprise CA settings apply to the shared HTTP transport and both OBO clients. Missing SharePoint consent returns a tool error without disabling Graph. A `claims_challenge_required` result indicates that Conditional Access needs an authentication round trip; this implementation does not relay that challenge into an automatic host sign-in flow, and a simple reconnect may not resolve it. [Microsoft OBO claims challenges](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)

### Controls still needed within AI Hub

A selected-site `write` grant still permits harmful edits within that site. This version has no publisher-specific owned-page allowlist, deletion prohibition or per-plan change limit. The Graph catalog includes file sharing and destructive content operations; prompts and repository conventions are not authorization enforcement. A restricted publishing policy would need to cover Graph page, list-item and drive aliases, transfer entry points and continuation handles. Draft inspection and retained prior content provide additional recovery and review controls.

Playwright authenticates separately. A browser signed in with a broadly privileged account can bypass the MCP app's selected-site boundary. Use an appropriately restricted browser identity/session for inspection, and keep privileged Graph credentials out of the model's terminal environment.

## Content handoff and on-demand publishing

GitHub, terminal and browser tools are coordinated by the model in OpenWebUI. This server does not call another MCP, clone a repository, compile Markdown, own a content manifest, or run a scheduler. The separate content repository should track Markdown, layout templates, assets, source-to-page/asset IDs and the published source revision.

1. Resolve the site using `graph_read` with a path such as `/sites/tenant.sharepoint.com:/sites/AIHub`. Reconcile the repository manifest with typed page listing before creating a new page.
2. Compile Markdown in the terminal into page JSON. Text content belongs in `textWebPart.innerHtml` within `body`; `graph_write.html` is specifically for OneNote. Inspect an existing page with `$expand=canvasLayout` before updating it.
3. Upload assets through the native transfer tools and rewrite links to their final SharePoint URLs. Reuse existing asset IDs: new-file upload sessions rename on collision; replacing an existing file uses its ID and eTag.
4. Pass the generated JSON to `graph_write`. A terminal-local filename is not readable by this MCP. Compact UTF-8 JSON/HTML defaults to a 60,000-byte outbound bound, configurable up to 2,000,000. The outer MCP bound accounts for JSON escaping and envelope overhead. Inspect the actual limits through `graph_capabilities`; ingress/gateway/model limits still apply. For larger content, use smaller supported section/web-part changes. Native drive transfers do not transfer page JSON into an API request.
5. Open the draft in an independently authenticated Playwright browser. Verify that the selected model actually receives screenshots, inspect links/images/layout, and correct the draft.
6. When the user's request authorizes publication, call the page's `/publish` action without a body. Read back `publishingState` and `webUrl`, then verify the rendered page. A configured approval flow can delay publication despite a successful publish call. [Publish behavior](https://learn.microsoft.com/en-us/graph/api/sitepage-publish?view=graph-rest-1.0)
7. Record the confirmed page/asset IDs and source revision in the content repository. After an uncertain write, read current state before retrying; creation is not an upsert. Repeating an unchanged publish plan should not create another page or asset.

Full `canvasLayout` updates replace the layout. Use supported web parts and preserve existing content that the repository does not own. Applying a `home` page layout does not assign the site homepage; use `sharepoint_write(set_home_page)` for that separate action. [Update semantics](https://learn.microsoft.com/en-us/graph/api/sitepage-update?view=graph-rest-1.0)

## Rollout and acceptance

Build the 0.4.0 image using the existing [release process](kubernetes.md), deploy its immutable digest with the updated chart, and refresh LiteLLM/OpenWebUI tool discovery. There are ten tools. Confirm `graph_capabilities.sharepoint.enabled` and the configured site URL before using site-setting writes. Update the model's [system prompt](open-webui-system-prompt.md).

On a designated pilot page, demonstrate create → draft inspection → publish → edit → republish → rerun without duplicates, including an image and a link to another page. Check the final `published` state; report pending approval if it remains checked out. In a designated test navigation location, add a link, read it back, update it, and remove the test node. Validate theme application and homepage assignment only when those actual site changes are requested. Verify an existing-theme read, separate Graph/SharePoint auth, missing-consent handling and screenshot delivery in the real tool chain.

Local tests cover the page lifecycle, method/query boundaries, unknown write outcomes, payload limits through HTTP MCP, distinct resource tokens and caches, optional-consent isolation, REST destination and input validation, theme lookup/application, and homepage existence checks. These tests do not establish live tenant compatibility.
