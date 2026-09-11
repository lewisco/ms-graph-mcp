# Roll out Microsoft 365 tools 0.2.0

This release replaces the profile-only interface with eight tools and a discoverable route catalog. It requires a **new container image**, not just a chart reconciliation. The chart/app versions are 0.2.0; Flux chart-label normalization is retained.

## Work-side procedure

1. Fetch the latest pushed commit and record it. Read `docs/usage.md`, `docs/setup.md` and the Trivy release policy.
2. Build linux/amd64 with `scripts/release_image.py`, a unique Harbor tag and the existing approved DHI runtime digest. Use compatible Python 3.14 development and uv mirror overrides. The script needs Trivy, not Scout; publish with `--push` only if its gate passes.
3. Record the published immutable digest, raw/accepted findings, scanner database timestamp and evidence directory. The accepted liblzma issue now uses Debian identifier `TEMP-1147318-639065`, formerly `TEMP-0000000-639065`, with the same `GHSA-5qpq-xqfv-j9pg` issue and package/layer. The acceptance expiry has not been extended.
4. Update the GitOps image reference to that digest and reconcile the chart/HelmRelease. Confirm Deployment, Service, ready pods and endpoints before testing LiteLLM. Harbor policy remains independent of the local gate.
5. Configure the MCP app's **delegated** Graph consent for the services to be used. `User.Read` alone still only authorizes basic profile access. Use the service-coverage permission guidance and each endpoint's Microsoft permission table. Shared mailboxes need user delegation rights; optional meeting artifacts/insights need their service permissions and eligibility. Never replace OBO with application-only tokens.
6. Refresh LiteLLM/WebUI tool discovery or reconnect the Microsoft tools connection. Verify all eight names appear: `graph_capabilities`, `graph_describe`, `graph_read`, `graph_write`, `graph_continue`, `graph_prepare_transfer`, `graph_transfer_status`, `graph_transfer_manage`.
7. Check `graph_capabilities` reports `stage: microsoft365`. Start with `/me`, a small mail query, a dated calendar view and drive discovery for a designated user. A supported route does not imply access; preserve actual 403/404/eligibility results.
8. Validate writes only against designated resources and with user authorization. Test pagination across replicas, a native download/upload round trip, and owner isolation. Preserve the uploaded driveItem ID and verify size/destination; test content hashes separately where available.

## Boundaries to report accurately

The implementation exposes cataloged Microsoft 365 operations, not every Graph API. Directory administration, beta and batch requests are excluded. Word/PowerPoint editing/rendering belongs to terminal tooling. Server-staged artifacts, large transcript/JSON delivery beyond the 2 MB inline ceiling, non-drive binary transfers and retention management remain incomplete. No live Microsoft calls or file transfers were performed by the local regression suite.

Native transfer handles expire after one hour and work across replicas sharing the tenant/client credentials. Credential rotation invalidates existing handles. Preauthenticated storage URLs have separate provider expiry; keep them out of logs and final answers and never attach Graph bearer tokens to them.
