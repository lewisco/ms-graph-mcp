# Security controls for the authentication/profile slice

The September 2026 review identified signing-key refresh contention, global OBO exchange contention, and deployment defaults that relied on external gateway isolation. The implemented controls below address those findings. They apply to the current profile-only server; future file and write operations need their own security validation.

## Authentication availability

Signing-key retrieval uses the fixed tenant JWKS URL and verified TLS. A successfully fetched key set is trusted for 300 seconds. Refreshes have a 30-second global cooldown, counted from completion, whether they succeed or fail. Unknown key IDs do not create an unbounded negative-cache map or bypass this cooldown. Known, unexpired keys remain usable while another request refreshes the set. A failed refresh never extends their original expiry; once it expires, the server returns a sanitized, retryable error. Key rotation can introduce a delay of up to the cooldown before the next successful refresh. Cancelling an HTTP caller does not cancel the shared refresh.

Public health and OAuth metadata routes ignore bearer headers entirely, so sending a token to them cannot trigger Microsoft requests. MCP Host, Origin and Content-Type checks run before authentication. Protected MCP routes retain signature, tenant, audience, time, client, scope and delegated-user validation. The verifier creates fresh JWT validation options on every call and never accepts token-provided JWKS URLs.

OBO cache hits do not wait for network calls. Requests with the same assertion share one exchange; different assertions can exchange independently up to `GRAPH_MCP_OBO_MAX_CONCURRENT_EXCHANGES` (default 4; Helm `config.oboMaxConcurrentExchanges`). Additional distinct misses receive HTTP 503 and `Retry-After: 10`, without an internal waiting queue. Each exchange owns its MSAL client and discovery cache, avoiding concurrent mutations of shared MSAL state. This means a cache miss may also perform discovery requests.

Sanitized exchange failures, including consent/Conditional Access challenges, are retained for at most ten seconds per assertion. Their cache is bounded by `GRAPH_MCP_OBO_CACHE_ENTRIES`, independently of the equally bounded successful-token cache. Only assertion hashes are cache keys; failure cache entries contain no raw upstream errors or exception tracebacks. A new assertion can be tried immediately, subject to the concurrency limit. All callers still need a currently valid incoming JWT. Cancelling a caller does not release an exchange's concurrency slot while its worker is running. Shutdown drains exchanges before clearing cached state.

These controls bound expensive authentication work. Deploy gateway request/connection limits appropriate to your environment as well; they do not replace overall ingress traffic limits.

## Deployment requirements

Helm enables NetworkPolicy and requires explicit gateway peers. Configure both namespace and pod selectors where appropriate, and verify that the CNI enforces the policy. NetworkPolicy admission is separate from Entra user authorization. A caller cannot substitute a Host header for network-level admission.

HTTPS is the default Service transport. Supply an existing TLS Secret and configure LiteLLM's CA trust and matching Service DNS name. The application mounts the private key read-only for its non-root group. The alternative `transportSecurity.mode: mesh` must be selected explicitly and requires independently enforced mesh mTLS. Loopback development remains available without TLS. See [Kubernetes setup](kubernetes.md) for installation and migration inputs.

For an existing installation, configure the TLS Secret, change LiteLLM's upstream URL to HTTPS and configure its trust, set the actual gateway selectors, then deploy the chart and application together. A mesh installation must explicitly select mesh mode. The chart will fail rendering when required values are missing; it will not silently fall back to unrestricted ingress or plaintext.

## Verification and remaining boundaries

The [active vulnerability release policy](release-vulnerability-policy.md) accepts the exact, expiring DHI OS baseline using Trivy alone. It retains raw findings and blocks all findings outside that acceptance. The [DHI discrepancy review](security-reviews/dhi-2026-09-10.md) records the 14 unresolved findings and a Docker support request draft.

Regression tests cover unknown-key bursts, cached-key progress during refresh, failed refresh backoff, expiry and rotation, request cancellation, public-route bypass of authentication work, isolated concurrent OBO exchanges, failed-exchange backoff, and overload rejection. Helm tests verify the default NetworkPolicy, TLS Secret mounts, HTTPS listener arguments/probes, and explicit mesh mode. Existing JWT rejection, user-isolation, Graph destination/redirect, and enterprise CA tests remain in place.

Live Entra/LiteLLM interoperability, certificate issuance/rotation, ingress-controller backend TLS and cluster NetworkPolicy enforcement require deployment verification. No live tenant or cluster changes are made by the local test suite.
