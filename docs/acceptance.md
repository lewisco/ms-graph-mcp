# Acceptance and validation plan

Status: **local authentication scaffold checks pass; all live integration gates remain pending**. Tests exercise the actual MCP SDK HTTP server with generated signed JWTs and mocked Microsoft calls. No real tenant, credentials, custom terminal image, or cluster deployment was exercised.

## 1. Evidence already established

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| User-provided requirements and versions | Scope and compatibility baseline | Installed image health or actual configuration |
| LiteLLM 1.100.0 public source | `oauth_delegate` and Entra OBO implementation exist | Complete WebUI-to-Graph OAuth compatibility |
| WebUI 0.11.3 public source | Static OAuth/custom headers and terminal image-input handling exist | Provider/model receives images through the user's route |
| Open Terminal 0.11.34 public source | Image reads are supported by the file endpoint | Custom image contains all rendering dependencies |
| Microsoft documentation | Candidate endpoints, permissions, limits | Access to a specific mailbox, meeting, file, or license |
| Local scaffold tests, 2026-09-10 | Signed JWT rejection/acceptance, OBO isolation/cache/errors, real SDK HTTP discovery and `/me` calls under 2025-03-26 and 2025-11-25 protocol negotiation, compact text results, input/redirect restrictions | Actual WebUI/LiteLLM interoperability, live Microsoft access or file/document workflows |

Sources are linked beside the relevant claims in [architecture](architecture.md), [coverage](service-coverage.md), and [terminal contract](terminal-contract.md). The future implementation must record actual image digests, dependency versions, provider/model route, test date, and tenant policy assumptions.

### Local scaffold run — 2026-09-10

- Python 3.12.14, MCP SDK 2.2.0, MSAL 1.38.0; complete dependency resolution in `uv.lock`.
- **71 tests passed**; Ruff lint and formatting passed. One upstream Starlette TestClient/AnyIO deprecation warning remains.
- Packaged Uvicorn factory started successfully. Real loopback HTTP checks passed for health, readiness, path-specific metadata, and an unauthenticated MCP 401 challenge. The process was stopped after the check.
- Markdown links, code fences, and JSON examples validated.
- Docker build **not verified**: Docker Hub and GHCR base-image metadata requests timed out before build steps ran. No image digest or cluster result is available.
- No live Microsoft authorization, gateway/client-chain test, transfer, or document rendering was performed. G1–G3 remain pending.

### HA and enterprise CA extension — 2026-09-10

- **96 tests passed**; Ruff lint/format and Helm lint passed. The same upstream deprecation warning remains.
- Local HTTPS fixtures prove configured enterprise CA trust for Graph's HTTPX stack, MSAL's Requests stack, and PyJWT's JWKS stack. Untrusted roots and hostname mismatches are rejected. Public roots remain loaded; invalid PEM fails startup.
- Independent app instances accept the same user's request after the first instance stops; each instance uses its own token cache without sticky sessions. This simulates replica failover; it is not a node-failure or cluster-load test.
- Helm render tests cover replica counts 2/3/5, rolling-update settings, disruption budget, node/zone spread, ConfigMap/Secret CA mounts, existing credential references, digest-based images, metadata routing and NetworkPolicy peers. Invalid one-replica and ambiguous/unsafe configurations are rejected during render. A four-replica configuration also rendered successfully.
- Helm chart packaging and documentation validation completed. Image builds/publication, actual scheduling, live failover, registry trust, enterprise proxy behavior and CA rotation in a cluster remain pending.

### Security review fixes — 2026-09-10

- **119 tests passed**; Ruff lint/format, Helm lint and diff whitespace checks passed. The upstream Starlette TestClient/AnyIO deprecation warning remains.
- Built the server wheel offline, refreshed the local non-editable installation, and verified that all nine installed source modules match the reviewed source tree. No dependency versions changed.
- Signing-key tests cover unknown-ID floods, refresh cooldown, cached-key progress during a blocked fetch, failed fetches, expiry, rotation and caller cancellation. Public health/metadata requests cannot invoke bearer verification or OBO.
- OBO tests demonstrate that a failing user's exchange cannot block another user's cached token or an independent exchange. Same-assertion requests share work; cancellation preserves the concurrency bound; sanitized failure backoff is bounded and retains claims challenges.
- Helm defaults now render gateway NetworkPolicy and an HTTPS listener with an existing TLS Secret. Tests cover missing deployment inputs and explicit mesh mode. A real local Uvicorn HTTPS listener serves health and protected MCP routes, rejects plaintext, and passes client checks for issuer trust and hostname validation.
- See [security controls](security.md) and [deployment migration inputs](kubernetes.md). Live cluster policy enforcement, certificate rotation and Microsoft/gateway integration remain pending.

## 2. First implementation gates

Complete these narrow slices before expanding to the complete service catalog. They are implementation sequencing, not permission to drop any agreed service.

### G1 — Delegated authentication through the real client chain

Configure a separate Microsoft OAuth connection in WebUI through a dedicated LiteLLM route. Verify:

1. Initial unauthenticated discovery reaches the expected MCP resource and Entra tenant without redirect loops.
2. The static OAuth client and actual callback URI work with PKCE and explicit MCP scope.
3. Gateway admission and MCP bearer occupy separate headers; no admission key reaches the MCP or Graph.
4. `initialize`, `tools/list`, and a `/me` tool call work, including first use after a cold start.
5. `/me` identifies the connecting user. A second user's connection identifies that user instead.
6. Refresh succeeds after expiry; reconnect works after explicit disconnect. Missing/revoked consent gives an actionable failure.
7. Wrong audience, wrong tenant, expired token, and app-only token are rejected.
8. A Conditional Access challenge survives both proxy layers or produces an explicit reauthorization requirement without retry loops.

Evidence: sanitized requests/statuses, token claim names and expected identities without full tokens, configuration field mapping, correlation IDs, and results for two users. Record exact resource-indicator behavior rather than claiming generic OAuth compliance from discovery alone.

### G2 — Large file round trip

Through a model-coordinated MCP/terminal workflow, download a file larger than 5 MB and upload a new copy. Repeat at 250,000,000 bytes in a suitable test drive.

Verify size and compatible checksum/readback, destination IDs, original unchanged, bounded process memory, and absence of binary/base64 file content in model messages. Interrupt an upload and resume without duplicated content. Expire a URL, acquire a new one, and recover without exposing a general Graph token.

Evidence: source/destination metadata, byte counts, transfer status, checksum method, peak resource measurements, and sanitized tool-message sizes. This is the direct regression test for the user's Work IQ limitation.

### G3 — The model actually sees rendered pages

Create a fixture with a visual defect and a randomized visible marker that is not supplied in surrounding text. Render through LibreOffice/PDFium and read the PNG through terminal tools using the actual LiteLLM vision-model route.

Verify that the model identifies the marker and layout issue from the image. Correct the file and view the final render. Repeat with a region crop. Verify that `display_file`/browser preview alone is not counted as model inspection. On a text-only model, the system must explain that visual review is unavailable.

Evidence: input file hash, render manifest, image input received by the provider where observable, model finding, edit, final image, and final revision hash. The marker test supplements payload inspection; a model claiming “I saw it” is not sufficient evidence.

## 3. Core acceptance matrix

| ID | Scenario | Pass condition |
| --- | --- | --- |
| AUTH-01 | Two concurrent users | Graph tokens, result handles, jobs, and artifacts never cross user/tenant boundaries |
| AUTH-02 | Shared integration gateway key | MCP still attributes and authorizes using each validated Entra user |
| AUTH-03 | Missing Microsoft connection | Browser connection/reauthorization is requested; no application credential fallback |
| AUTH-04 | Cold/restarted proxy and MCP | Tool discovery works or returns actionable authorization state; no silently empty tool catalog |
| AUTH-05 | Registration credentials | Secrets exist only at their owning backend; no duplicate Graph secret in LiteLLM/terminal |
| TOOL-01 | Operation discovery | Model locates the correct service/path without receiving a whole Graph schema |
| TOOL-02 | Read/write classification | POST search is classified as read; send/delete/batch mutations cannot run through the read tool |
| TOOL-03 | Unsupported query option | Graph-compatible error returned; gateway does not silently reinterpret the query |
| TOOL-04 | Batch mixed success | Per-item outcomes reported; successful mutations not repeated when retrying failed items |
| CTX-01 | Collection pagination | Every item remains reachable; no skipped/duplicated item at local-preview/upstream-page boundary |
| CTX-02 | Oversized JSON page | Preview and complete artifact/continuation returned; JSON remains valid |
| CTX-03 | Explicit larger inline request | Soft preference can be exceeded; actual transport constraints are explicit and full data remains retrievable |
| CTX-04 | Long transcript | Requested excerpt and full-content route available; no hidden model-generated summary |
| CTX-05 | Repeated small calls | Client instructions support selective retrieval; server does not pretend to know remaining context |
| CTX-06 | Structured + text MCP result | Full payload does not reach the model twice; client-readable fallback remains usable |
| CTX-07 | Export all pages | Explicit job with progress/cancellation; “complete” only after all selected pages are fetched |
| FILE-01 | >5 MB and 250 MB drive files | Successful round trip through terminal with no document bytes in chat |
| FILE-02 | Session interruption/expiry | Resume or restart with clear status and no silent duplicate file |
| FILE-03 | Source changed before overwrite | Conditional write fails cleanly; current source retained |
| FILE-04 | Default save | New copy created with non-destructive collision handling |
| FILE-05 | Lost final upload response | Reconcile destination before retrying; distinguish unknown from failed |
| FILE-06 | Shared terminal path assumption | MCP does not attempt to open a terminal-local path |
| ART-01 | JSON, transcript, attachment | Terminal receives complete bytes through scoped artifact route |
| ART-02 | Wrong owner/handle substitution | Issuance denied; guessed/mutated/expired tickets rejected |
| ART-03 | Cleanup | Expired artifacts inaccessible; abandoned objects removed; user outputs/source files preserved |
| ART-04 | Active transfer and expiry | Current leased stream is well defined; expired tickets cannot start another stream |
| ART-05 | Pod restart/replica switch | Durable artifact/continuation state works; unsupported ephemeral mode reports expired/lost handles explicitly |
| OPS-01 | Graph 429 | Retry-After respected; bounded retries and clear next action |
| OPS-02 | Timeout after send | No blind replay; uncertain outcome is visible and reconcilable where possible |
| OPS-03 | Bad URL/redirect/batch route | Request cannot escape allowed Graph/transfer destinations with credentials |
| OPS-04 | Ingress | MCP JSON/event-stream negotiation, required headers, cancellation, and timeouts work |
| OPS-05 | Logs/errors | No client secrets, bearer tokens, transfer tickets, or document bodies leak into ordinary logs |
| HA-01 | Two or more replicas | Desired count configurable; replicas Ready across distinct nodes; requests reach either replica without sticky sessions |
| HA-02 | Pod loss and rolling update | Subsequent profile requests succeed on surviving/cold replicas; pending writes are never blindly replayed |
| TLS-01 | Enterprise CA | Graph, OBO and JWKS trust configured roots, retain public roots, and reject untrusted/wrong-host certificates |
| TLS-02 | CA/secret rotation | Rolling restart loads new material while another replica remains available |

## 4. Service fixtures

Each fixture has an expected user, source resource IDs, prerequisites, supported operations, and an explicit cleanup action for test-created resources. Permission-related unavailability is reported as a scoped result; it is not a pass for a core fixture the test account was intentionally provisioned to access.

| Fixture | Required evidence |
| --- | --- |
| Own Outlook | Search → read → draft → attach → send to controlled recipient; move/delete test messages |
| Shared Outlook | Read and send using the account's granted shared-mailbox identity; verify sender semantics |
| Calendar | Date-window occurrences, create/update event, invitation response/cancel, shared calendar access, timezone behavior |
| Outlook settings/contacts | Representative category/rule or settings operation; own/shared contact scope as provisioned |
| Teams chat/channel | Discover, read/search, send/reply, edit/delete a test user's own content where supported; file reference resolution |
| Hosted meeting | Calendar/join URL resolution and accessible transcript retrieval |
| Attended meeting | Artifact access tested independently of calendar visibility |
| Meeting insights | Licensed user sees available summary/action items with provenance |
| Non-Copilot user | Core tools work; unavailable insights do not break the connection; shared summary/transcript remains usable if accessible |
| No transcript/insight | No fabricated content; distinguish missing artifact from permission failure and processing delay |
| SharePoint | Site/library discovery, list/list-item CRUD, supported page create/edit/publish in test site |
| Planner basic | Plan/bucket/task discovery, create/update checklist/assignment, stale eTag conflict, delete test task |
| To Do | List/task/checklist/linked-resource workflow and cleanup |
| Excel | Copy-first edit, session/range/table operation, explicit persistent behavior, formula/calculation checks |
| People/profile | Own identity, relevant people, coworker lookup, contacts without unnecessary directory-admin access |

No transcript availability is assumed for every meeting. Include at least one hosted and one attended meeting known to have accessible artifacts. Add a controlled denied meeting to prove error behavior. External-tenant cases remain subject to actual cross-tenant access and are documented separately.

## 5. Document quality fixtures

| ID | Fixture | Pass condition |
| --- | --- | --- |
| DOC-01 | New deck from SharePoint/OneDrive PPTX | Theme/layout/style followed; editable copy saved; every slide visually reviewed in batches |
| DOC-02 | Chat-uploaded template and terminal reference | Correct local files resolved; no assumed shared filesystem |
| DOC-03 | Existing deck targeted edit | Requested content changed; current style and untouched elements retained |
| DOC-04 | Overflowing text and malformed table layout | Model identifies defects from rendered images, corrects them, re-renders final file |
| DOC-05 | Chart/notes/media/animation fixture | Preservation limits explicitly measured; static preview not mistaken for behavioral validation |
| DOC-06 | Missing font | Substitution reported; selected rendered output reviewed; no false fidelity claim |
| DOC-07 | Word table/page reflow | Affected and following pages inspected; no missing text, broken pagination, or unintended headers/footers |
| DOC-08 | Excel range and printed view | Formula/value validation plus selected visual review; no claim that a PDF represents the whole workbook |
| DOC-09 | Preview after another edit | Stale render hash detected; review bound to final saved revision |
| DOC-10 | Large presentation | Outline/overview/selected pages used without sending every full-resolution image at once |
| DOC-11 | Scratch cleanup | Preview files can expire/regenerate; source and final files remain |

## 6. Completion standard

For each implemented operation, record one of: passed, failed with defect, unavailable with evidenced service limitation, or pending. A feature cannot be called complete while its essential fixture is pending. Record residual Office fidelity differences against concrete examples rather than a blanket guarantee.

The system is ready for internal use when the three gates and core agreed workflows pass, remaining API limitations are documented, and temporary-file/token isolation is verified. Live mutation fixtures must use designated test resources and recipients; do not send test mail/messages or modify production documents merely to validate a documentation change.

For the current scaffold stage, local completion means the implemented authentication/profile slice passes its tests, setup instructions are available, and untested integration assumptions remain marked. It does not complete G1, G2, G3, or the overall service acceptance matrix.
