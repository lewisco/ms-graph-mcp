# Kubernetes deployment and manual image builds

The [Helm chart](../charts/ms-graph-mcp/values.yaml) supports AKS and RKE2 on Kubernetes 1.30+, defaults to **two replicas**, and accepts a deployment-time replica count of two or more. Builds can run manually on a workstation or external build machine. CI is optional.

Only the authentication/profile scaffold is currently implemented. Its stateless HTTP transport and independent OBO caches support routing between replicas. File transfers and document workflows remain future work.

## 1. Build from source and publish an image

The Git remote is `https://github.com/lewisco/ms-graph-mcp.git`. Container images belong in a registry such as Harbor or GitHub Container Registry (GHCR), not in the Git repository. The commands below are manual examples; no image has been published by adding them.

The default build uses `dhi.io/python:3.14-debian13-dev` and the matching `dhi.io/python:3.14-debian13` runtime. Both support amd64/arm64. The runtime runs as UID/GID 65532; container and Helm commands use `/app/.venv/bin/python` directly. It does not depend on a shell or a `python` alias. [DHI Python definitions](https://github.com/docker-hardened-images/catalog/tree/main/image/python/debian-13)

**Release criterion: zero findings outside the approved DHI OS baseline; all Python dependency findings block release.** A clean base image alone does not pass this criterion. Use the [manual release gate](../scripts/release_image.py); Docker build alone is a development build and does not perform the scan.

Install Docker with Buildx, Python 3.11+ on the build host, and a current Trivy CLI supporting the flags in the script. Log into DHI and your destination registry through Docker's credential store. No CI service is required.

```sh
docker login dhi.io
docker login harbor.example.com
python3 scripts/release_image.py \
  --platform linux/amd64 \
  --image harbor.example.com/ai/ms-graph-mcp:0.1.0-dhi-amd64 \
  --push
```

For a Mac build pushed to GitHub Container Registry:

```sh
docker login dhi.io
docker login ghcr.io --username lewisco
python3 scripts/release_image.py \
  --platform linux/arm64 \
  --image ghcr.io/lewisco/ms-graph-mcp:0.1.0-dhi-arm64 \
  --push
```

Select `linux/amd64` even on Apple Silicon when that is the cluster architecture. Omit `--push` to build, scan and tag locally. Each invocation covers one platform: run the gate separately for amd64 and arm64. The script does not publish a multi-platform manifest; do not use a raw multi-platform `--push` command as a substitute for scanning every architecture. [Docker platform support](https://docs.docker.com/build/building/multi-platform/)

The gate builds locally with refreshed base-image metadata, checks runtime imports under non-root/read-only restrictions, exports the immutable image ID to an archive, downloads a vulnerability database into a new cache and scans that exact archive. It explicitly includes UNKNOWN/LOW/MEDIUM/HIGH/CRITICAL and unfixed vulnerabilities, and checks that OS and Python application packages were inventoried. Local Trivy config, ignore files and `TRIVY_*` environment overrides cannot weaken the gate. Download/scan errors, stale DB metadata, incomplete coverage, suppressed findings or any vulnerability outside the accepted OS baseline block tagging/publishing the release image. [Trivy image scan options](https://trivy.dev/docs/latest/references/configuration/cli/trivy_image/)

Each run writes evidence into a unique directory under `dist/`: `scan.json`, database metadata and scanner version in `release.json`, Buildx metadata, archive SHA-256, Docker image ID, verified image configuration digest and, after publication, registry digests. Failed runs retain evidence with `status: blocked`. The archive can be large; retain it according to your release-evidence policy. Docker's image ID can identify a configuration, manifest, or index depending on the image store. The gate verifies the archive's digest chain to bind Trivy's configuration digest to that image ID. Use the recorded registry manifest digest for Helm's `image.digest` after a successful push. Database downloads require network access and up-to-date upstream metadata; the gate fails closed when these are unavailable. Configure enterprise trust on the host for Trivy as well as on Docker when needed.

For base images mirrored inside work, provide matching approved DHI references, preferably pinned to digests:

```sh
python3 scripts/release_image.py \
  --platform linux/amd64 \
  --build-arg PYTHON_BUILD_IMAGE=harbor.example.com/base/python:3.14-debian13-dev \
  --build-arg PYTHON_RUNTIME_IMAGE=harbor.example.com/base/python:3.14-debian13 \
  --build-arg UV_IMAGE=harbor.example.com/base/uv:0.12.12 \
  --image harbor.example.com/ai/ms-graph-mcp:0.1.0-dhi-amd64 \
  --push
```

The previous single `PYTHON_IMAGE` argument has been replaced by separate build/runtime arguments. The Python mirrors must retain compatible Python paths and ABI and include the standard CA bundle; the uv mirror must provide `/uv`. Python packages remain locked in `uv.lock`; network access to the locked package sources is required. The gate retains all findings. Known OS findings may pass under the exact, expiring baseline; new findings require repair or an explicitly reviewed acceptance update.

## 2. TLS trust by connection

| Connection | Configure trust here |
| --- | --- |
| LiteLLM or metadata ingress → MCP | Server certificate in `transportSecurity.existingSecret`; issuing CA trust and server-name verification in LiteLLM or the ingress controller |
| MCP → Graph, Entra token endpoint and signing keys | Chart `enterpriseCA`, or `GRAPH_MCP_EXTRA_CA_FILE` outside Kubernetes |
| Build step → Python package repositories | Optional BuildKit `enterprise_ca` secret below |
| Docker/BuildKit/Kubernetes node → Harbor or other image registry | Host, builder, and node/container-runtime registry trust configuration |

Runtime CA mounts cannot fix image pulls: the node downloads the image before the pod exists. Likewise, a build-step CA cannot fix downloading the base image. Configure Harbor trust on each relevant Docker/containerd host using your cluster's registry configuration.

For dependency downloads behind enterprise TLS, pass a PEM CA bundle as a build secret:

```sh
python3 scripts/release_image.py \
  --platform linux/amd64 \
  --enterprise-ca /path/to/enterprise-ca.pem \
  --image harbor.example.com/ai/ms-graph-mcp:0.1.0-dhi-amd64 \
  --push
```

The Dockerfile combines the supplied CA roots with the build image's public roots for `uv`, then removes the temporary bundle. The CA secret is not copied into the final runtime image. This addresses TLS trust; explicit outbound proxy support is separate. [Docker build secrets](https://docs.docker.com/build/building/secrets/), [uv certificate configuration](https://docs.astral.sh/uv/concepts/authentication/certificates/)

For runtime trust, create a ConfigMap in the release namespace:

```sh
kubectl create namespace ai
kubectl -n ai create configmap enterprise-ca \
  --from-file=enterprise-ca.pem=/path/to/enterprise-ca.pem
```

Use `enterpriseCA.configMapName: enterprise-ca`. An existing Secret can instead be referenced through `enterpriseCA.secretName`; choose exactly one source. The source key can be configured. Supply PEM certificates, without private keys. The application adds these roots to its public CA store for HTTPX (Graph), Requests/MSAL (OBO), and PyJWT/urllib (signing keys). Certificate and hostname verification remain enabled; malformed PEM fails startup.

The CA file is mounted read-only. SSL contexts load it at process startup, so a CA update requires a rolling restart. The same applies to the Entra secret, which is read from the environment. An external secret manager/reloader may automate that restart; this chart does not read secret values into Helm release history.

```sh
kubectl -n ai rollout restart deployment/ms-graph-mcp
kubectl -n ai rollout status deployment/ms-graph-mcp
```

## 3. Install the chart

Create or sync an existing Secret named `ms-graph-mcp-entra` containing the MCP API client secret under `client-secret`. Configure a registry pull Secret such as `harbor-pull` if required. The chart references these resources; credentials are not Helm values. For a private GHCR image, use the equivalent GHCR pull credentials.

The default `transportSecurity.mode: tls` also requires an existing `kubernetes.io/tls` Secret, such as `ms-graph-mcp-tls`, containing `tls.crt` (certificate chain) and `tls.key`. Provision it through your certificate/secret management workflow. The certificate must cover the exact Service DNS name used by LiteLLM, for example `ms-graph-mcp.ai.svc`. Set `transportSecurity.existingSecret` to its name. The chart mounts it read-only with group access for the non-root process, enables Uvicorn HTTPS, and uses HTTPS probes. LiteLLM must trust its issuing CA; the MCP's outbound `enterpriseCA` setting does not configure LiteLLM's trust store. Restart pods after certificate rotation to reload the files.

Replace `networkPolicy.ingressFrom` with your real LiteLLM namespace and pod selectors. The example selects pods labeled `app.kubernetes.io/name: litellm` in namespace `ai`; inspect your deployment labels before using it. Missing peers or a missing TLS Secret name cause chart rendering to fail. The cluster CNI must enforce NetworkPolicy.

Copy [the deployment example](../deploy/values.example.yaml) into an ignored local values file:

```sh
cp deploy/values.example.yaml deploy/work.local.yaml
```

Replace the tenant/app IDs, LiteLLM public URL, image reference, gateway selectors and existing credential/TLS Secret/CA names. Set `enterpriseCA.enabled: false` if no additional outbound CAs are needed; this does not disable the inbound HTTPS listener. The example uses placeholder registrations and a tag that must be built first.

```sh
helm lint charts/ms-graph-mcp -f deploy/work.local.yaml
helm template msgraph charts/ms-graph-mcp -n ai -f deploy/work.local.yaml
helm upgrade --install msgraph charts/ms-graph-mcp \
  --namespace ai --create-namespace \
  -f deploy/work.local.yaml --set replicaCount=2 \
  --wait --timeout 5m
```

For a larger deployment, set `replicaCount: 3` or higher in the values file or command. The chart rejects counts below two. Set `image.digest: sha256:<64 hex characters>` to deploy by digest; it takes precedence over `image.tag`. The examples use `fullnameOverride: ms-graph-mcp`, so Service and Deployment names remain stable.

To package the chart for an external deployment machine:

```sh
helm package charts/ms-graph-mcp --destination dist
```

That machine can install the resulting `.tgz` using its own values and registry access. There is no dependency on GitHub Actions, Argo CD, Flux, or a particular registry.

## 4. Availability behavior

The defaults provide:

- Two desired replicas, hard node spread with at least two eligible node domains, and preferred zone spread.
- Rolling updates with `maxUnavailable: 0`, `maxSurge: 1`, and ten seconds of readiness before an updated pod counts as available.
- A PodDisruptionBudget allowing one replica unavailable during voluntary eviction.
- Startup, readiness and liveness probes using local process health; no dependency on a user's Graph access.
- A ten-second pre-stop drain delay and up to 25 seconds for Uvicorn shutdown within a 45-second pod termination grace period.

Provide at least two eligible worker nodes and capacity for the extra rollout pod. Hard node spread intentionally leaves a replica Pending if only one eligible node exists. To require availability across zones, set `topology.zoneWhenUnsatisfiable: DoNotSchedule` and `topology.zoneMinDomains: 2`; nodes must have the corresponding zone labels and capacity. Default zone preference accommodates clusters without zone labels. [Topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)

The disruption budget does not prevent node failures or guarantee every in-flight request survives. It also does not control Deployment rolling updates; the rollout strategy does that. Surviving replicas serve subsequent requests. [Disruption budgets](https://kubernetes.io/docs/tasks/run-application/configure-pdb/)

No sticky sessions, shared refresh-token cache, or PVC is required by the current scaffold. Each replica independently validates tokens and acquires/caches delegated Graph tokens. Losing a cache leads to another OBO exchange. Future continuation handles, upload jobs and temporary artifacts must use shared durable state before those workflows can claim HA. Future writes must reconcile uncertain outcomes rather than blindly replaying a request after failure.

## 5. LiteLLM, ingress and network policy

With the example release in namespace `ai`, configure LiteLLM with:

```yaml
mcp_servers:
  msgraph:
    url: https://ms-graph-mcp.ai.svc:8000/mcp
    transport: http
    auth_type: oauth_delegate
```

The chart adds its Service DNS names to the allowed Host list. For a custom cluster domain or proxy-rewritten Host, add the exact name and port through `config.extraAllowedHosts`. The Service has no session affinity. WebUI still connects to the public HTTPS LiteLLM resource URL from `config.resourceUrl`.

Use the existing ingress arrangement for LiteLLM. If the advertised OAuth metadata URL needs a separate route, enable `metadataIngress` and configure its ingress class and frontend TLS entries to match that hostname. Also configure your controller's HTTPS backend protocol and certificate trust for the MCP Service; frontend TLS alone does not encrypt the backend connection. For ingress-nginx, the backend protocol annotation is `nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"`; configure upstream certificate verification and the matching server name as well. Other controllers need their corresponding settings. The generated route exposes only the exact path `/.well-known/oauth-protected-resource` plus the public MCP resource path. It neither exposes `/mcp` directly nor rewrites the metadata path. Verify your controller permits this additional route alongside LiteLLM; alternatively route it through your existing Gateway API/ingress configuration.

The ingress-only NetworkPolicy is enabled by default and requires explicit `networkPolicy.ingressFrom` peers. Use selectors matching LiteLLM and, when metadata ingress is enabled, its ingress-controller pods. Namespace and pod selectors inside one peer are ANDed. Disable this policy only when an existing cluster policy enforces the same gateway restriction. It does not change egress policies; outbound DNS and Microsoft HTTPS must be reachable.

For the ingress-nginx protocol and certificate verification settings, use its [backend protocol](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#backend-protocol) and [backend certificate authentication](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#backend-certificate-authentication) documentation.

For a deployment already protected by enforced service-mesh mTLS, select `transportSecurity.mode: mesh` explicitly and use `http://ms-graph-mcp.ai.svc:8000/mcp` inside that mesh. This option leaves TLS termination to the mesh and does not install or verify its policies. Keep NetworkPolicy enabled and confirm unmeshed/plaintext callers cannot reach the service. The chart has no automatic plaintext fallback.

## 6. Verify after deployment

```sh
kubectl -n ai get pods -l app.kubernetes.io/instance=msgraph -o wide
kubectl -n ai get pdb ms-graph-mcp
kubectl -n ai rollout status deployment/ms-graph-mcp
```

Check that both replicas are Ready on different nodes, follow the real WebUI → LiteLLM sign-in test in [setup](setup.md), and verify the advertised metadata URL. Confirm the gateway verifies the Service certificate, plaintext connections are rejected in TLS mode, and an unrelated workload cannot connect through the NetworkPolicy. In a designated test environment, send profile calls while restarting a replica and confirm subsequent calls succeed through the other replica. Test an actual CA and server-certificate rotation and rejected untrusted certificate in that environment as well.

Local tests verify TLS against generated enterprise certificates on all three clients, app-instance failover, and Helm-rendered configuration. They do not establish real cluster scheduling, ingress behavior, live tenant authorization, Harbor connectivity, or a successfully published image. Complete those checks before declaring production HA.

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| Helm requires `transportSecurity.existingSecret` | Provision a server TLS Secret and set its name. Use `mode: mesh` only with an existing enforced mesh mTLS policy. |
| Helm requires `networkPolicy.ingressFrom` | Set the actual gateway peers; the example's namespace and pod labels are placeholders for your deployment. |
| Pods are Pending | Check that at least two nodes match the selectors/tolerations and have capacity; default node spread requires two eligible nodes. |
| Pod fails to mount a Secret or start HTTPS | Check the credential and TLS Secret names in the release namespace, expected keys (`client-secret`, `tls.crt`, `tls.key`), and certificate/key validity. |
| LiteLLM reports a certificate error | Confirm its upstream URL is HTTPS, the certificate includes the exact Service DNS name, and LiteLLM trusts the issuing CA. The MCP's `enterpriseCA` setting does not configure LiteLLM. |
| LiteLLM times out connecting to MCP | Check Ready endpoints and gateway pod/namespace selectors. Confirm the CNI enforces the intended policy and that any mesh policy admits the gateway. |
| MCP rejects Host (HTTP 421) or Origin (HTTP 403) | Add the exact value the gateway sends to `config.extraAllowedHosts` or `config.allowedOrigins`. The public resource URL does not automatically authorize an incoming Host header. |
| Public OAuth metadata cannot be fetched | Check the exact advertised metadata path, ingress-controller NetworkPolicy peer, backend HTTPS protocol, certificate verification and server name. |
| Health works but Microsoft authorization fails | Health probes only show process readiness. Check outbound DNS/HTTPS, registration settings, consent, client allowlist and token mapping in the [setup guide](setup.md). |
| Short bursts receive authentication HTTP 503 | Honor `Retry-After: 10`. Check upstream availability and `config.oboMaxConcurrentExchanges`; inspect expected concurrency before raising its limit. |

For Graph-specific tool errors and reconnect guidance, see the [user guide](usage.md#results-and-errors). All application settings and limits are listed in the [configuration reference](configuration.md).

## 8. Upgrading the earlier HTTP-default scaffold

1. Build a new image from the fixed source with a new tag or digest. Reusing an old image tag with `IfNotPresent` can leave existing nodes running the earlier code.
2. Provision the MCP Service TLS certificate and configure its issuing CA in LiteLLM. Set the new image reference, `transportSecurity.existingSecret`, and actual `networkPolicy.ingressFrom` peers in your local values. Deployments using enforced mesh mTLS must explicitly choose `transportSecurity.mode: mesh`.
3. Plan a maintenance window for the first HTTP-to-HTTPS switch. A rolling update can briefly mix HTTP and HTTPS pods behind the same Service, and a single LiteLLM URL cannot speak both protocols. The two-replica setting does not make this transport migration seamless.
4. Run Helm lint/template with the new values, then the upgrade command in section 3. Wait for the rollout to finish before changing LiteLLM's upstream URL to HTTPS and resuming tool use. Update any metadata ingress backend TLS settings during the same window. Mesh mode retains the HTTP application URL inside the mesh.
5. Verify the client-facing metadata route, a signed-in user's `/me` result, certificate verification, and denial of connections from unrelated workloads. Subsequent deployments that keep the same transport can use the normal rolling-update flow.

No Entra client secret belongs in the LiteLLM MCP server entry. The application/API scopes and public resource URL do not need to change solely for these security fixes.

### Comparing Docker Scout with Trivy

For an independent local Scout audit, build with `docker buildx build --pull --load --platform linux/amd64 --provenance=mode=max --sbom=true -t ms-graph-mcp:scout-audit .`, then run `docker scout cves --platform linux/amd64 local://ms-graph-mcp:scout-audit`. Pin the runtime build argument to the digest being investigated when reproducing a scan. Full provenance and SBOM allow Scout to discover the DHI base and apply its vendor VEX attestations. [Docker DHI scanning](https://docs.docker.com/dhi/how-to/scan/)

The 2026-09-10 Python 3.14.7 app audit returned Scout 0, unfiltered Trivy 47, and Trivy with the DHI VEX repository 14 remaining/33 suppressed. These are different assessment modes. Scout is not required by the active release policy. The Trivy gate accepts the exact known OS baseline and retains the raw findings. See [acceptance evidence](acceptance.md).

### Trivy-only accepted baseline

Docker Scout is not required at work. The default release command uses `security/dhi-os-baseline.json`, pins its runtime digest and permits only its exact known OS findings. It still scans every severity and blocks all Python findings. See [active policy](release-vulnerability-policy.md).

```sh
python3 scripts/release_image.py --image harbor.example/ai/ms-graph-mcp:python314-reviewed --platform linux/amd64
```

Use your actual repository/tag. Add `--push` only when publication is intended. Mirrored runtime overrides must include the accepted digest, for example `--build-arg PYTHON_RUNTIME_IMAGE=harbor.example/dhi-cache/python:3.14-debian13@sha256:ee0154c1c675e1f51f361c239061128719c06cc7130c6fae0a8362b0ba777267`. Confirm the mirror serves that digest before running. Build/uv mirrors remain configurable.

Use `--strict` for the former zero-unfiltered-findings gate. The bundled acceptance is amd64-only and expires at the JSON's `expires_at`; an arm64 run requires its own reviewed baseline or a passing strict scan. A new base digest is not covered automatically. Harbor's own vulnerability enforcement remains independent.
