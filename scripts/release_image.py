#!/usr/bin/env python3
"""Build one platform, scan its immutable archive, and optionally publish after a clean scan."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEVERITIES = "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL"


class ReleaseBlocked(Exception):
    pass


def run(command, *, env=None, cwd=None):
    result = subprocess.run(command, env=env, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        # Keep the diagnostic, but never turn a scanner/download failure into a pass.
        raise ReleaseBlocked(f"{command[0]} failed ({result.returncode}): {result.stderr[-3000:]}")
    return result.stdout


def check_report(report, image_id):
    if report.get("ArtifactType") != "container_image":
        raise ReleaseBlocked("Scanner did not identify a container image.")
    metadata = report.get("Metadata", {})
    if metadata.get("ImageID") != image_id:
        raise ReleaseBlocked("Scanner image ID does not match the built image.")
    if metadata.get("OS", {}).get("Family") != "debian":
        raise ReleaseBlocked("Scanner did not identify the expected Debian OS.")
    if metadata.get("OS", {}).get("EOSL"):
        raise ReleaseBlocked("Runtime OS is end of support.")
    results = report.get("Results")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise ReleaseBlocked("Missing or invalid scan results.")
    if not any(r.get("Class") == "os-pkgs" and r.get("Packages") for r in results):
        raise ReleaseBlocked("OS package scan coverage is missing.")
    python_results = [r for r in results if r.get("Type") == "python-pkg"]
    names = {
        p.get("Name", "").lower().replace("_", "-")
        for r in python_results
        for p in r.get("Packages", [])
    }
    if not {"ms-graph-mcp", "mcp", "msal", "uvicorn"} <= names:
        raise ReleaseBlocked("Python application package scan coverage is incomplete.")
    findings = sum(len(r.get("Vulnerabilities") or []) for r in results)
    suppressed = sum(len(r.get("ExperimentalModifiedFindings") or []) for r in results)
    if findings or suppressed:
        raise ReleaseBlocked(
            f"Release blocked: {findings} vulnerabilities, {suppressed} suppressed findings."
        )


def check_database(metadata, now):
    try:
        updated = datetime.fromisoformat(metadata["UpdatedAt"].replace("Z", "+00:00"))
        next_update = datetime.fromisoformat(metadata["NextUpdate"].replace("Z", "+00:00"))
        if not metadata["Version"] or not updated <= now < next_update:
            raise ValueError("database is stale or from the future")
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseBlocked("Vulnerability database metadata is missing or stale.") from exc


def release(args):
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence = {"status": "blocked", "requested_image": args.image, "platform": args.platform}
    evidence_path = output / "release.json"
    # Do not allow ambient TRIVY_* variables or local config/ignore files to weaken the gate.
    scanner_env = {k: v for k, v in os.environ.items() if not k.startswith("TRIVY_")}
    config = output / "trivy.yaml"
    config.write_text("{}\n")
    ignore = output / "trivyignore"
    ignore.write_text("")
    candidate = f"ms-graph-mcp-build:{uuid.uuid4().hex}"
    try:
        evidence["scanner"] = run(["trivy", "--version"], env=scanner_env, cwd=output).strip()
        build = [
            "docker",
            "buildx",
            "build",
            "--pull",
            "--load",
            "--platform",
            args.platform,
            "--metadata-file",
            str(output / "build.json"),
            "--tag",
            candidate,
        ]
        for value in args.build_arg:
            build += ["--build-arg", value]
        if args.enterprise_ca:
            build += ["--secret", f"id=enterprise_ca,src={args.enterprise_ca.resolve()}"]
        build.append(str(ROOT))
        print("Building final DHI runtime image...", flush=True)
        (output / "build.stdout").write_text(run(build))
        image = json.loads(run(["docker", "image", "inspect", candidate]))[0]
        image_id = image["Id"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ReleaseBlocked("Invalid built image ID.")
        if f"{image['Os']}/{image['Architecture']}" != args.platform:
            raise ReleaseBlocked("Built platform does not match the requested platform.")
        evidence["image_id"] = image_id
        # Validate the copied interpreter/dependencies under the production container restrictions.
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "/app/.venv/bin/python",
                image_id,
                "-c",
                "import sys, ssl, mcp, msal, jwt, uvicorn, ms_graph_mcp.app; "
                "assert sys.version_info[:2] == (3, 12); "
                "assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0",
            ]
        )
        evidence["runtime_smoke"] = "passed"
        archive = output / "image.tar"
        run(["docker", "image", "save", "--output", str(archive), image_id])
        with archive.open("rb") as stream:
            evidence["archive_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        with tempfile.TemporaryDirectory(prefix="trivy-", dir=output) as cache:
            common = ["trivy", "image", "--config", str(config), "--cache-dir", cache]
            print("Downloading a fresh vulnerability database...", flush=True)
            run([*common, "--download-db-only"], env=scanner_env, cwd=output)
            db = json.loads((Path(cache) / "db/metadata.json").read_text())
            check_database(db, datetime.now(UTC))
            evidence["database"] = db
            print("Scanning all OS and Python packages, at every severity...", flush=True)
            # exit-code=0 allows us to retain/inspect JSON, including low/unknown/unfixed findings.
            # Only check_report's explicit zero-finding decision can open the publish step.
            run(
                [
                    *common,
                    "--skip-db-update",
                    "--scanners",
                    "vuln",
                    "--pkg-types",
                    "os,library",
                    "--severity",
                    SEVERITIES,
                    "--ignore-unfixed=false",
                    "--ignore-status",
                    "",
                    "--ignorefile",
                    str(ignore),
                    "--show-suppressed",
                    "--list-all-pkgs",
                    "--format",
                    "json",
                    "--output",
                    str(output / "scan.json"),
                    "--exit-code",
                    "0",
                    "--input",
                    str(archive),
                ],
                env=scanner_env,
                cwd=output,
            )
            check_database(db, datetime.now(UTC))
            check_report(json.loads((output / "scan.json").read_text()), image_id)
        evidence["scanned_at"] = datetime.now(UTC).isoformat()
        evidence["vulnerabilities"] = 0
        evidence["scan_status"] = "passed"
        run(["docker", "image", "tag", image_id, args.image])
        if args.push:
            current = json.loads(run(["docker", "image", "inspect", args.image]))[0]
            if current["Id"] != image_id:
                raise ReleaseBlocked("Release tag changed after scanning.")
            (output / "push.stdout").write_text(run(["docker", "image", "push", args.image]))
            published = json.loads(run(["docker", "image", "inspect", args.image]))[0]
            evidence["repo_digests"] = published.get("RepoDigests", [])
            if not evidence["repo_digests"]:
                raise ReleaseBlocked("Push completed but registry digest evidence is missing.")
        evidence["status"] = "published" if args.push else "passed"
        print(f"Zero-finding gate passed. Evidence: {evidence_path}")
    except (ReleaseBlocked, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        evidence["error"] = str(exc)
        raise ReleaseBlocked(str(exc)) from exc
    finally:
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Explicit registry/repository:tag")
    parser.add_argument("--platform", required=True, choices=["linux/amd64", "linux/arm64"])
    parser.add_argument("--push", action="store_true", help="Publish only after a successful gate")
    parser.add_argument("--enterprise-ca", type=Path)
    parser.add_argument("--build-arg", action="append", default=[], metavar="NAME=IMAGE")
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "dist" / f"release-{uuid.uuid4().hex}"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]+:[A-Za-z0-9_][A-Za-z0-9_.-]*", args.image):
        parser.error("--image must specify a repository and explicit tag")
    allowed = {"PYTHON_BUILD_IMAGE", "PYTHON_RUNTIME_IMAGE", "UV_IMAGE"}
    if any(
        value.partition("=")[0] not in allowed or not value.partition("=")[2]
        for value in args.build_arg
    ):
        parser.error(
            "--build-arg accepts only PYTHON_BUILD_IMAGE, PYTHON_RUNTIME_IMAGE and UV_IMAGE"
        )
    try:
        release(args)
    except (ReleaseBlocked, OSError) as exc:
        parser.exit(1, f"Release blocked: {exc}\n")


if __name__ == "__main__":
    main()
