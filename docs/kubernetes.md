# Kubernetes deployment and manual image builds

The [Helm chart](../charts/ms-graph-mcp/values.yaml) supports AKS and RKE2 on Kubernetes 1.30+, defaults to **two replicas**, and accepts a deployment-time replica count of two or more. Builds can run manually on a workstation or external build machine. CI is optional.

Only the authentication/profile scaffold is currently implemented. Its stateless HTTP transport and independent OBO caches support routing between replicas. File transfers and document workflows remain future work.

## 1. Build from source and publish an image

The Git remote is `https://github.com/lewisco/ms-graph-mcp.git`. Container images belong in a registry such as Harbor or GitHub Container Registry (GHCR), not in the Git repository. The commands below are manual examples; no image has been published by adding them.

Build a native image on an amd64 workstation at work, then push to Harbor:

```sh
docker login harbor.example.com
docker buildx build --platform linux/amd64 --load \
  -t harbor.example.com/ai/ms-graph-mcp:0.1.0-local .
docker push harbor.example.com/ai/ms-graph-mcp:0.1.0-local
```

The explicit platform also makes an amd64 cluster image from an Apple Silicon Mac. For a local Mac smoke test, use `--platform linux/arm64 --load`. For one image reference supporting both architectures, publish a manifest list:

```sh
docker login ghcr.io --username lewisco
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/lewisco/ms-graph-mcp:0.1.0-local --push .
```

Use a builder with the requested platform support; Docker Desktop normally provides emulation. Cross-platform builds can be slower. The target architecture's Python environment is built inside the target image; the Mac `.venv` is excluded. Use a unique release tag and record the resulting digest for deployment. [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/)

For registries mirrored inside work, override the base images:

```sh
docker buildx build --platform linux/amd64 --load \
  --build-arg PYTHON_IMAGE=harbor.example.com/base/python:3.12-slim-bookworm \
  --build-arg UV_IMAGE=harbor.example.com/base/uv:0.12.12 \
  -t harbor.example.com/ai/ms-graph-mcp:0.1.0-local .
```

The Python mirror must match the expected Python 3.12 Debian image layout and include its standard CA bundle; the uv mirror must provide `/uv`. Pin those arguments to approved digests for reproducible base images. Python packages remain locked in `uv.lock`; network access to the locked package sources is required.

## 2. Enterprise CAs: three separate trust locations

| Connection | Configure trust here |
| --- | --- |
| MCP → Graph, Entra token endpoint and signing keys | Chart `enterpriseCA`, or `GRAPH_MCP_EXTRA_CA_FILE` outside Kubernetes |
| Build step → Python package repositories | Optional BuildKit `enterprise_ca` secret below |
| Docker/BuildKit/Kubernetes node → Harbor or other image registry | Host, builder, and node/container-runtime registry trust configuration |

Runtime CA mounts cannot fix image pulls: the node downloads the image before the pod exists. Likewise, a build-step CA cannot fix downloading the base image. Configure Harbor trust on each relevant Docker/containerd host using your cluster's registry configuration.

For dependency downloads behind enterprise TLS, pass a PEM CA bundle as a build secret:

```sh
docker buildx build --platform linux/amd64 --load \
  --secret id=enterprise_ca,src=/path/to/enterprise-ca.pem \
  -t harbor.example.com/ai/ms-graph-mcp:0.1.0-local .
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

Copy [the deployment example](../deploy/values.example.yaml) into an ignored local values file:

```sh
cp deploy/values.example.yaml deploy/work.local.yaml
```

Replace the tenant/app IDs, LiteLLM public URL, image reference and existing Secret/CA names. Set `enterpriseCA.enabled: false` if no additional runtime CAs are needed. The example uses placeholder registrations and a tag that must be built first.

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
    url: http://ms-graph-mcp.ai.svc:8000/mcp
    transport: http
    auth_type: oauth_delegate
```

The chart adds its Service DNS names to the allowed Host list. For a custom cluster domain or proxy-rewritten Host, add the exact name and port through `config.extraAllowedHosts`. The Service has no session affinity. WebUI still connects to the public HTTPS LiteLLM resource URL from `config.resourceUrl`.

Use the existing ingress arrangement for LiteLLM. If the advertised OAuth metadata URL needs a separate route, enable `metadataIngress` and configure its ingress class and TLS entries to match that hostname. The generated route exposes only the exact path `/.well-known/oauth-protected-resource` plus the public MCP resource path. It neither exposes `/mcp` directly nor rewrites the metadata path. Verify your controller permits this additional route alongside LiteLLM; alternatively route it through your existing Gateway API/ingress configuration.

The optional ingress-only NetworkPolicy requires explicit `networkPolicy.ingressFrom` peers. Use selectors matching LiteLLM and, when metadata ingress is enabled, its ingress-controller pods. Namespace and pod selectors inside one peer are ANDed. The policy is disabled by default because workload labels vary. It does not change egress policies. Outbound DNS and Microsoft HTTPS must be reachable; internal TLS/service-mesh transport remains owned by your cluster.

## 6. Verify after deployment

```sh
kubectl -n ai get pods -l app.kubernetes.io/instance=msgraph -o wide
kubectl -n ai get pdb ms-graph-mcp
kubectl -n ai rollout status deployment/ms-graph-mcp
```

Check that both replicas are Ready on different nodes, follow the real WebUI → LiteLLM sign-in test in [setup](setup.md), and verify the advertised metadata URL. In a designated test environment, send profile calls while restarting a replica and confirm subsequent calls succeed through the other replica. Test an actual CA rotation and rejected untrusted certificate in that environment as well.

Local tests verify TLS against generated enterprise certificates on all three clients, app-instance failover, and Helm-rendered configuration. They do not establish real cluster scheduling, ingress behavior, live tenant authorization, Harbor connectivity, or a successfully published image. Complete those checks before declaring production HA.
