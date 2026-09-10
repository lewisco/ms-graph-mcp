import importlib.util
import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_image", Path(__file__).resolve().parents[1] / "scripts/release_image.py"
)
release_image = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_image)
IMAGE_ID = "sha256:" + "a" * 64


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
    "failure", [None, "build", "smoke", "db", "stale", "scan", "finding", "retag"]
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
                Path(command[command.index("--output") + 1]).write_text(json.dumps(data))
        elif command[1] == "buildx":
            assert "--push" not in command
            assert "--load" in command and "--pull" in command
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
            Path(command[command.index("--output") + 1]).write_bytes(b"immutable image archive")
        return ""

    monkeypatch.setattr(release_image, "run", run)
    if failure:
        with pytest.raises(release_image.ReleaseBlocked):
            release_image.release(args)
    else:
        release_image.release(args)
    evidence = json.loads((args.output_dir / "release.json").read_text())
    pushes = [c for c in commands if c[:3] == ["docker", "image", "push"]]
    assert len(pushes) == (0 if failure else 1)
    assert evidence["status"] == ("blocked" if failure else "published")
    if not failure:
        assert evidence["vulnerabilities"] == 0
        assert evidence["database"]["Version"] == 2
        assert evidence["image_id"] == IMAGE_ID
        assert evidence["archive_sha256"]
