#!/usr/bin/env python3
"""Build and scan one platform; publish only within the reviewed Trivy risk policy."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
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


def archive_config_id(archive, image_id, platform):
    """Bind Trivy's config digest to Docker's config, manifest, or index identity."""
    try:
        with tarfile.open(archive) as image:

            def read_json(name, digest=None):
                member = image.getmember(name)
                if not member.isfile() or member.size > 4 * 1024 * 1024:
                    raise ValueError("invalid image metadata member")
                with image.extractfile(member) as stream:
                    data = stream.read()
                actual = "sha256:" + hashlib.sha256(data).hexdigest()
                if digest is not None and actual != digest:
                    raise ValueError("image metadata digest mismatch")
                return json.loads(data), actual

            def read_blob(digest):
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                    raise ValueError("invalid image digest")
                return read_json("blobs/sha256/" + digest.removeprefix("sha256:"), digest)[0]

            entries, _ = read_json("manifest.json")
            if not isinstance(entries, list) or len(entries) != 1:
                raise ValueError("archive must contain exactly one runnable image")
            config, config_id = read_json(entries[0]["Config"])
            if f"{config['os']}/{config['architecture']}" != platform:
                raise ValueError("archive platform differs from the requested platform")
            if image_id != config_id:
                # The containerd image store can return an index/manifest ID from inspect.
                # Verify its content-addressed chain instead of accepting an arbitrary config.
                manifest = read_blob(image_id)
                if "manifests" in manifest:
                    matches = [
                        item
                        for item in manifest["manifests"]
                        if f"{item.get('platform', {}).get('os')}/"
                        f"{item.get('platform', {}).get('architecture')}" == platform
                    ]
                    if len(matches) != 1:
                        raise ValueError(
                            "image index does not identify exactly one target platform"
                        )
                    manifest = read_blob(matches[0]["digest"])
                if manifest["config"]["digest"] != config_id:
                    raise ValueError("archive config is not referenced by the built image")
            return config_id
    except (tarfile.TarError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ReleaseBlocked(f"Cannot verify archive image identity: {exc}") from exc


def check_report(report, image_id, baseline=None):
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
    accepted = 0
    if baseline:

        def identity(v):
            return tuple(
                v.get(k) for k in ("VulnerabilityID", "PkgName", "InstalledVersion", "Severity")
            ) + (
                v.get("Layer", {}).get("Digest"),
                v.get("Layer", {}).get("DiffID"),
            )

        allowed = {identity(v) for v in baseline["findings"]}
        accepted = sum(
            identity(v) in allowed
            for r in results
            if r.get("Class") == "os-pkgs"
            for v in r.get("Vulnerabilities") or []
        )
    if findings != accepted or suppressed:
        raise ReleaseBlocked(
            f"Release blocked: {findings - accepted} vulnerabilities outside acceptance, "
            f"{suppressed} suppressed findings; {accepted} accepted OS findings."
        )
    return {
        "raw_findings": findings,
        "accepted_os_findings": accepted,
        "suppressed_findings": suppressed,
    }


def load_baseline(path, platform, now):
    if path is None:
        return None
    baseline = json.loads(path.read_text())
    if baseline.get("schema_version") != 1 or baseline.get("policy") != "accepted-dhi-os-baseline":
        raise ReleaseBlocked("Unsupported risk-acceptance policy.")
    if baseline.get("platform") != platform:
        raise ReleaseBlocked("OS risk acceptance does not cover this platform.")
    created = datetime.fromisoformat(baseline["created_at"])
    expires = datetime.fromisoformat(baseline["expires_at"])
    if not created <= now < expires:
        raise ReleaseBlocked("OS risk acceptance is expired or not yet valid.")
    digest = baseline["runtime_digest"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) or not baseline["runtime_image"].endswith(
        "@" + digest
    ):
        raise ReleaseBlocked("Risk acceptance requires an immutable runtime digest.")
    if not isinstance(baseline.get("findings"), list) or not baseline["findings"]:
        raise ReleaseBlocked("Risk acceptance has no findings.")
    for v in baseline["findings"]:
        if not all(
            isinstance(v.get(k), str) and v[k]
            for k in ("VulnerabilityID", "PkgName", "InstalledVersion", "Severity")
        ):
            raise ReleaseBlocked("Invalid accepted finding identity.")
        if not all(
            re.fullmatch(r"sha256:[a-f0-9]{64}", v.get("Layer", {}).get(k, ""))
            for k in ("Digest", "DiffID")
        ):
            raise ReleaseBlocked("Accepted findings must identify their exact image layer.")
    return baseline


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
        baseline = load_baseline(getattr(args, "baseline", None), args.platform, datetime.now(UTC))
        evidence["policy"] = baseline["policy"] if baseline else "zero-unfiltered-findings"
        build_args = list(args.build_arg)
        if baseline:
            evidence["risk_acceptance"] = baseline
            evidence["risk_acceptance_sha256"] = hashlib.sha256(
                args.baseline.read_bytes()
            ).hexdigest()
            runtime_args = [
                v.partition("=")[2] for v in build_args if v.startswith("PYTHON_RUNTIME_IMAGE=")
            ]
            if any(not v.endswith("@" + baseline["runtime_digest"]) for v in runtime_args):
                raise ReleaseBlocked("Runtime override is outside the accepted DHI digest.")
            if not runtime_args:
                build_args.append("PYTHON_RUNTIME_IMAGE=" + baseline["runtime_image"])
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
        for value in build_args:
            build += ["--build-arg", value]
        if args.enterprise_ca:
            build += ["--secret", f"id=enterprise_ca,src={args.enterprise_ca.resolve()}"]
        build.append(str(ROOT))
        print("Building final DHI runtime image...", flush=True)
        (output / "build.stdout").write_text(run(build))
        if baseline:
            materials = (
                json.loads((output / "build.json").read_text())
                .get("buildx.build.provenance", {})
                .get("materials", [])
            )
            if not any(
                m.get("digest", {}).get("sha256")
                == baseline["runtime_digest"].removeprefix("sha256:")
                for m in materials
            ):
                raise ReleaseBlocked(
                    "Build provenance does not contain the accepted runtime digest."
                )
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
                "assert sys.version_info[:2] == (3, 14); "
                "assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0",
            ]
        )
        evidence["runtime_smoke"] = "passed"
        archive = output / "image.tar"
        run(["docker", "image", "save", "--output", str(archive), image_id])
        config_id = archive_config_id(archive, image_id, args.platform)
        evidence["image_config_id"] = config_id
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
            # Only check_report's explicit policy decision can open the publish step.
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
            # Recheck expiry at the actual decision, not only before the build.
            if baseline:
                if datetime.now(UTC) >= datetime.fromisoformat(baseline["expires_at"]):
                    raise ReleaseBlocked("OS risk acceptance expired during the scan.")
            evidence.update(
                check_report(json.loads((output / "scan.json").read_text()), config_id, baseline)
            )
        evidence["scanned_at"] = datetime.now(UTC).isoformat()
        evidence["vulnerabilities"] = evidence["raw_findings"]
        evidence["unaccepted_findings"] = 0
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
        print(
            f"Release gate passed ({evidence['policy']}); {evidence['raw_findings']} raw findings, "
            f"{evidence['accepted_os_findings']} accepted OS findings. Evidence: {evidence_path}"
        )
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
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "security/dhi-os-baseline.json",
        help="Reviewed OS risk-acceptance baseline (default: repository policy)",
    )
    policy.add_argument("--strict", action="store_true", help="Require zero unfiltered findings")
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "dist" / f"release-{uuid.uuid4().hex}"
    )
    args = parser.parse_args()
    if args.strict:
        args.baseline = None
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
