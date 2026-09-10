import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ms_graph_mcp.config import Settings

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm CLI required")


def render(*overrides, success=True):
    command = [
        "helm",
        "template",
        "test",
        str(ROOT / "charts/ms-graph-mcp"),
        "--namespace",
        "ai",
        "-f",
        str(ROOT / "deploy/values.example.yaml"),
    ]
    for value in overrides:
        command += ["--set", value]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if not success:
        assert result.returncode != 0
        return result.stderr
    assert result.returncode == 0, result.stderr
    return {item["kind"]: item for item in yaml.safe_load_all(result.stdout) if item}


@pytest.mark.parametrize("replicas", [2, 3, 5])
def test_ha_and_secret_contract(replicas, monkeypatch):
    resources = render(f"replicaCount={replicas}")
    deployment = resources["Deployment"]["spec"]
    assert deployment["replicas"] == replicas
    assert deployment["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}
    assert resources["PodDisruptionBudget"]["spec"]["maxUnavailable"] == 1
    pod = deployment["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 65532
    assert pod["securityContext"]["runAsGroup"] == 65532
    container = pod["containers"][0]
    assert container["command"] == ["/app/.venv/bin/python", "-m", "uvicorn"]
    assert container["lifecycle"]["preStop"]["exec"]["command"][0] == "/app/.venv/bin/python"
    spread = pod["topologySpreadConstraints"][0]
    assert spread["minDomains"] == 2 and spread["whenUnsatisfiable"] == "DoNotSchedule"
    assert not pod["automountServiceAccountToken"]
    assert resources["Service"]["spec"]["sessionAffinity"] == "None"
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "ms-graph-mcp-entra",
        "key": "client-secret",
    }
    config = resources["ConfigMap"]["data"]
    assert "GRAPH_MCP_CLIENT_SECRET" not in config
    assert config["GRAPH_MCP_OBO_MAX_CONCURRENT_EXCHANGES"] == "4"
    assert "ms-graph-mcp.ai.svc:8000" in json.loads(config["GRAPH_MCP_ALLOWED_HOSTS"])
    for key, value in config.items():
        if key != "GRAPH_MCP_EXTRA_CA_FILE":
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("GRAPH_MCP_CLIENT_SECRET", "test-only")
    Settings(_env_file=None)  # Chart serializations must be accepted by the running app.


def test_ca_mount_options_and_metadata_route():
    resources = render("metadataIngress.enabled=true")
    pod = resources["Deployment"]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["configMap"] == {
        "name": "enterprise-ca",
        "items": [{"key": "enterprise-ca.pem", "path": "enterprise-ca.pem"}],
    }
    rule = resources["Ingress"]["spec"]["rules"][0]
    assert rule["host"] == "litellm.example.com"
    assert rule["http"]["paths"][0]["path"] == "/.well-known/oauth-protected-resource/msgraph/mcp"
    assert len(rule["http"]["paths"]) == 1
    resources = render("enterpriseCA.configMapName=", "enterpriseCA.secretName=enterprise-secret")
    assert (
        resources["Deployment"]["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"]
        == "enterprise-secret"
    )
    resources = render("enterpriseCA.enabled=false")
    volumes = resources["Deployment"]["spec"]["template"]["spec"]["volumes"]
    assert [volume["name"] for volume in volumes] == ["server-tls"]
    assert "GRAPH_MCP_EXTRA_CA_FILE" not in resources["ConfigMap"]["data"]


def test_registry_digest_and_pull_credentials():
    digest = "sha256:" + "a" * 64
    resources = render(f"image.digest={digest}")
    pod = resources["Deployment"]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == f"harbor.example.com/ai/ms-graph-mcp@{digest}"
    assert pod["imagePullSecrets"] == [{"name": "harbor-pull"}]


def test_required_zone_spread():
    resources = render("topology.zoneWhenUnsatisfiable=DoNotSchedule")
    zone = resources["Deployment"]["spec"]["template"]["spec"]["topologySpreadConstraints"][1]
    assert zone["whenUnsatisfiable"] == "DoNotSchedule"
    assert zone["minDomains"] == 2


@pytest.mark.parametrize(
    "invalid",
    [
        "replicaCount=1",
        "enterpriseCA.secretName=ambiguous",
        "enterpriseCA.configMapName=",
        "credentials.existingSecret=",
        "image.tag=",
        "podDisruptionBudget.maxUnavailable=2",
        "networkPolicy.ingressFrom=null",
        "transportSecurity.existingSecret=",
        "transportSecurity.mode=plaintext",
        "config.oboMaxConcurrentExchanges=0",
    ],
)
def test_invalid_deployments_fail_during_render(invalid):
    render(invalid, success=False)


def test_network_policy_with_explicit_peer():
    resources = render()
    policy = resources["NetworkPolicy"]["spec"]
    assert policy["ingress"][0]["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ai"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
        }
    ]
    assert policy["policyTypes"] == ["Ingress"]
    assert policy["ingress"][0]["ports"] == [{"port": 8000, "protocol": "TCP"}]


def test_tls_is_default_with_secret_and_https_probes():
    resources = render()
    pod = resources["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert "--ssl-certfile" in container["args"] and "--ssl-keyfile" in container["args"]
    assert pod["securityContext"]["fsGroup"] == 65532
    tls = next(volume for volume in pod["volumes"] if volume["name"] == "server-tls")
    assert tls["secret"]["secretName"] == "ms-graph-mcp-tls"
    assert tls["secret"]["defaultMode"] == 0o440
    assert {item["key"] for item in tls["secret"]["items"]} == {"tls.crt", "tls.key"}
    mount = next(item for item in container["volumeMounts"] if item["name"] == "server-tls")
    assert mount["readOnly"] and mount["mountPath"] == "/etc/ms-graph-mcp/tls"
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["scheme"] == "HTTPS"
    assert resources["Service"]["spec"]["ports"][0]["appProtocol"] == "https"


def test_mesh_mode_is_explicit_and_keeps_gateway_policy():
    resources = render("transportSecurity.mode=mesh", "enterpriseCA.enabled=false")
    pod = resources["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert "--ssl-certfile" not in container["args"]
    assert "volumes" not in pod and "volumeMounts" not in container
    assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTP"
    assert "NetworkPolicy" in resources


def test_policy_can_be_explicitly_delegated_to_cluster():
    assert "NetworkPolicy" not in render("networkPolicy.enabled=false")
