import hashlib
import importlib.util
import io
import json
import tarfile
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_image", Path(__file__).resolve().parents[1] / "scripts/release_image.py"
)
release_image = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_image)
IMAGE_CONFIG = {"os": "linux", "architecture": "amd64", "config": {"User": "65532:65532"}}
IMAGE_ID = "sha256:" + hashlib.sha256(json.dumps(IMAGE_CONFIG).encode()).hexdigest()


def write_archive(path, kind="classic", problem=None):
    members = {}

    def blob(obj):
        data = json.dumps(obj).encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        members["blobs/sha256/" + digest.removeprefix("sha256:")] = data
        return digest

    config_id = blob(IMAGE_CONFIG)
    entries = [{"Config": "blobs/sha256/" + config_id.removeprefix("sha256:"), "Layers": []}]
    members["manifest.json"] = json.dumps(entries * (2 if problem == "multiple" else 1)).encode()
    image_id = config_id
    if kind != "classic":
        image_id = blob(
            {
                "config": {"digest": "sha256:" + "b" * 64 if problem == "unrelated" else config_id},
                "layers": [],
            }
        )
        if kind == "index":
            target = {"digest": image_id, "platform": {"os": "linux", "architecture": "amd64"}}
            manifests = [
                target,
                {
                    "digest": "sha256:" + "c" * 64,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
            ]
            if problem == "ambiguous":
                manifests.append(target)
            image_id = blob({"manifests": manifests})
        if problem == "tampered":
            members["blobs/sha256/" + image_id.removeprefix("sha256:")] += b" "
    with tarfile.open(path, "w") as archive:
        for name, data in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return image_id


@pytest.mark.parametrize("kind", ["classic", "manifest", "index"])
def test_archive_identity_supports_docker_image_stores(tmp_path, kind):
    archive = tmp_path / "image.tar"
    image_id = write_archive(archive, kind)
    assert release_image.archive_config_id(archive, image_id, "linux/amd64") == IMAGE_ID


@pytest.mark.parametrize("problem", ["multiple", "unrelated", "ambiguous", "tampered", "platform"])
def test_archive_identity_rejects_unbound_or_ambiguous_content(tmp_path, problem):
    archive = tmp_path / "image.tar"
    image_id = write_archive(archive, "index", problem)
    platform = "linux/arm64" if problem == "platform" else "linux/amd64"
    with pytest.raises(release_image.ReleaseBlocked, match="archive image identity"):
        release_image.archive_config_id(archive, image_id, platform)


def report():
    return {
        "ArtifactType": "container_image",
        "Metadata": {"ImageID": IMAGE_ID, "OS": {"Family": "debian", "Name": "13"}},
        "Results": [
            {"Class": "os-pkgs", "Packages": [{"Name": "libc6"}]},
            {
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Packages": [{"Name": name} for name in ["ms-graph-mcp", "mcp", "msal", "uvicorn"]],
            },
        ],
    }


@pytest.mark.parametrize("severity", ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"])
@pytest.mark.parametrize("kind", [0, 1])
def test_every_severity_and_package_kind_blocks(severity, kind):
    data = report()
    data["Results"][kind]["Vulnerabilities"] = [
        {"VulnerabilityID": "CVE-test", "Severity": severity, "FixedVersion": ""}
    ]
    with pytest.raises(release_image.ReleaseBlocked, match="1 vulnerabilities"):
        release_image.check_report(data, IMAGE_ID)


@pytest.mark.parametrize("problem", ["wrong_image", "no_os", "no_python", "suppressed", "eol"])
def test_incomplete_or_suppressed_scans_fail(problem):
    data = report()
    if problem == "wrong_image":
        data["Metadata"]["ImageID"] = "sha256:" + "b" * 64
    elif problem == "no_os":
        data["Results"] = data["Results"][1:]
    elif problem == "no_python":
        data["Results"] = data["Results"][:1]
    elif problem == "eol":
        data["Metadata"]["OS"]["EOSL"] = True
    else:
        data["Results"][0]["ExperimentalModifiedFindings"] = [{"Status": "ignored"}]
    with pytest.raises(release_image.ReleaseBlocked):
        release_image.check_report(data, IMAGE_ID)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "build",
        "smoke",
        "db",
        "stale",
        "scan",
        "finding",
        "retag",
        "accepted",
        "new_os",
        "provenance",
    ],
)
def test_publish_only_after_verified_scan(tmp_path, monkeypatch, failure):
    commands = []
    monkeypatch.setenv("TRIVY_SEVERITY", "CRITICAL")
    monkeypatch.setenv("TRIVY_SKIP_DB_UPDATE", "true")
    args = Namespace(
        output_dir=tmp_path / "evidence",
        image="harbor.example/ai/mcp:test",
        platform="linux/amd64",
        push=True,
        build_arg=[],
        enterprise_ca=None,
    )

    success = failure in (None, "accepted")
    if failure in ("accepted", "new_os", "provenance"):
        args.baseline = tmp_path / "baseline.json"
        baseline = accepted_baseline()
        baseline["created_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        baseline["expires_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        args.baseline.write_text(json.dumps(baseline))

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "trivy":
            assert not any(k.startswith("TRIVY_") for k in kwargs["env"])
            if command[1] == "--version":
                return "Version: test-version"
            assert Path(command[command.index("--config") + 1]).read_text() == "{}\n"
            cache = Path(command[command.index("--cache-dir") + 1])
            if "--download-db-only" in command:
                if failure == "db":
                    raise release_image.ReleaseBlocked("download failed")
                assert not (cache / "db").exists()
                (cache / "db").mkdir()
                now = datetime.now(UTC)
                db = {
                    "Version": 2,
                    "UpdatedAt": (now - timedelta(hours=2)).isoformat(),
                    "NextUpdate": (
                        now + timedelta(hours=-1 if failure == "stale" else 6)
                    ).isoformat(),
                }
                (cache / "db/metadata.json").write_text(json.dumps(db))
            else:
                if failure == "scan":
                    raise release_image.ReleaseBlocked("scan failed")
                assert command[command.index("--severity") + 1] == release_image.SEVERITIES
                assert "--ignore-unfixed=false" in command and "--show-suppressed" in command
                assert Path(command[command.index("--ignorefile") + 1]).read_text() == ""
                data = report()
                if failure == "finding":
                    data["Results"][0]["Vulnerabilities"] = [{"Severity": "LOW"}]
                if failure in ("accepted", "new_os", "provenance"):
                    data["Results"][0]["Vulnerabilities"] = list(baseline["findings"])
                    if failure == "new_os":
                        data["Results"][0]["Vulnerabilities"].append({"VulnerabilityID": "CVE-new"})
                Path(command[command.index("--output") + 1]).write_text(json.dumps(data))
        elif command[1] == "buildx":
            assert "--push" not in command
            assert "--load" in command and "--pull" in command
            if hasattr(args, "baseline"):
                assert "PYTHON_RUNTIME_IMAGE=" + baseline["runtime_image"] in command
                materials = (
                    []
                    if failure == "provenance"
                    else [{"digest": {"sha256": baseline["runtime_digest"][7:]}}]
                )
                Path(command[command.index("--metadata-file") + 1]).write_text(
                    json.dumps({"buildx.build.provenance": {"materials": materials}})
                )
            if failure == "build":
                raise release_image.ReleaseBlocked("build failed")
        elif command[1] == "run":
            assert "--read-only" in command
            assert command[command.index("--entrypoint") + 1] == "/app/.venv/bin/python"
            if failure == "smoke":
                raise release_image.ReleaseBlocked("runtime smoke failed")
        elif command[1:3] == ["image", "inspect"]:
            image_id = (
                "sha256:" + "b" * 64
                if failure == "retag" and command[-1] == args.image
                else IMAGE_ID
            )
            return json.dumps(
                [
                    {
                        "Id": image_id,
                        "Os": "linux",
                        "Architecture": "amd64",
                        "RepoDigests": ["harbor.example/ai/mcp@sha256:" + "c" * 64],
                    }
                ]
            )
        elif command[1:3] == ["image", "save"]:
            assert command[-1] == IMAGE_ID
            write_archive(Path(command[command.index("--output") + 1]))
        return ""

    monkeypatch.setattr(release_image, "run", run)
    if not success:
        with pytest.raises(release_image.ReleaseBlocked):
            release_image.release(args)
    else:
        release_image.release(args)
    evidence = json.loads((args.output_dir / "release.json").read_text())
    pushes = [c for c in commands if c[:3] == ["docker", "image", "push"]]
    assert len(pushes) == (1 if success else 0)
    assert evidence["status"] == ("published" if success else "blocked")
    if success:
        assert evidence["vulnerabilities"] == (47 if failure == "accepted" else 0)
        assert evidence["accepted_os_findings"] == (47 if failure == "accepted" else 0)
        assert evidence["database"]["Version"] == 2
        assert evidence["image_id"] == IMAGE_ID
        assert evidence["image_config_id"] == IMAGE_ID
        assert evidence["archive_sha256"]


def accepted_baseline():
    return json.loads((release_image.ROOT / "security/dhi-os-baseline.json").read_text())


@pytest.mark.parametrize(
    "change", [None, "cve", "version", "severity", "layer", "python", "suppressed"]
)
def test_baseline_accepts_only_exact_os_findings(change):
    baseline = accepted_baseline()
    data = report()
    v = dict(baseline["findings"][0])
    data["Results"][0]["Vulnerabilities"] = [v]
    if change == "cve":
        v["VulnerabilityID"] = "CVE-new"
    elif change == "version":
        v["InstalledVersion"] += "-new"
    elif change == "severity":
        v["Severity"] = "CRITICAL"
    elif change == "layer":
        v["Layer"] = {"Digest": "sha256:" + "f" * 64, "DiffID": "sha256:" + "f" * 64}
    elif change == "python":
        data["Results"][0]["Vulnerabilities"] = []
        data["Results"][1]["Vulnerabilities"] = [v]
    elif change == "suppressed":
        data["Results"][0]["ExperimentalModifiedFindings"] = [{"Status": "ignored"}]
    if change:
        with pytest.raises(release_image.ReleaseBlocked):
            release_image.check_report(data, IMAGE_ID, baseline)
    else:
        assert release_image.check_report(data, IMAGE_ID, baseline) == {
            "raw_findings": 1,
            "accepted_os_findings": 1,
            "suppressed_findings": 0,
        }


@pytest.mark.parametrize("problem", [None, "expired", "platform", "unpinned", "missing_layer"])
def test_baseline_scope_validation(tmp_path, problem):
    baseline = accepted_baseline()
    now = datetime.fromisoformat(baseline["created_at"]) + timedelta(hours=1)
    if problem == "expired":
        now = datetime.fromisoformat(baseline["expires_at"])
    elif problem == "platform":
        baseline["platform"] = "linux/arm64"
    elif problem == "unpinned":
        baseline["runtime_image"] = "dhi.io/python:3"
    elif problem == "missing_layer":
        baseline["findings"][0].pop("Layer")
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    if problem:
        with pytest.raises(release_image.ReleaseBlocked):
            release_image.load_baseline(path, "linux/amd64", now)
    else:
        assert release_image.load_baseline(path, "linux/amd64", now) == baseline
