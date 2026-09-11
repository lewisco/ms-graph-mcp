# Review of 33 VEX-filtered findings — 2026-09-10

Policy update: the user subsequently accepted these known OS risks under the [active Trivy-only baseline](../release-vulnerability-policy.md). Unresolved applicability questions below remain factual; earlier release-blocked statements describe the previous policy.

Status: all 33 remain unapproved under the proposed policy. This review classifies saved vendor claims; it does not establish exploitability, verify repository signatures or issue new VEX statements. No new scan was run.

The saved raw report contains 47 unique vulnerability/package/version findings. They reconcile exactly to 14 retained plus 33 filtered findings, with no gaps or overlap. Counts refer to package findings, not distinct CVEs. The complete scan identity and remaining findings are recorded in the [companion review](dhi-2026-09-10.md).

## Evidence categories

| Category | Package findings | Review requirement |
| --- | ---: | --- |
| Build-component rationale | 2 | Verify exact-image build configuration and absence of the affected component. |
| Disputed or usage-dependent | 8 | Review upstream dispute and service exposure; obtain exact-image applicability evidence. |
| Execution-path rationale | 2 | Verify exact-image vendor evidence and check native dependencies for calls to the diagnostic helpers. |
| No-dsa only | 16 | Request technical applicability evidence; advisory priority alone is insufficient. |
| Patch claimed | 5 | Verify signed evidence for this exact image and the claimed backport in its installed package. |

Specific patch/build claims are stronger candidates for resolution than advisory-priority claims, but none is approved solely because it appears in the repository. Package/version matches below are exact binary-package name and version matches after URL decoding, ignoring qualifiers; architecture, image scope, signature trust and continued validity still require verification. Generic unversioned products are not treated as exact matches.

## Finding ledger

| Identifier | Package and installed version | Severity | Rationale category | Exact package/version statement present? |
| --- | --- | --- | --- | --- |
| CVE-2026-42250 | `libbz2-1.0@1.0.8-6+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2026-19499 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | Patch claimed | No |
| CVE-2026-19542 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2026-5435 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | Execution-path rationale | No |
| CVE-2026-5450 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2026-5928 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2026-6238 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | Execution-path rationale | No |
| CVE-2026-77117 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2026-80489 | `libc6@2.41-12+deb13u3+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2010-4756 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2018-20796 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2019-1010022 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2019-1010023 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2019-1010024 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2019-1010025 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2019-9192 | `libc6@2.41-12+deb13u3+dhi2` | LOW | Disputed or usage-dependent | No |
| CVE-2025-66382 | `libexpat1@2.8.3-1~deb13u1+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2025-69720 | `libncursesw6@6.5+20250216-2+dhi4` | HIGH | Patch claimed | No |
| CVE-2025-6141 | `libncursesw6@6.5+20250216-2+dhi4` | LOW | No-dsa only | No |
| CVE-2021-45346 | `libsqlite3-0@3.46.1-7+deb13u1+dhi2` | LOW | Disputed or usage-dependent | Yes |
| CVE-2025-70873 | `libsqlite3-0@3.46.1-7+deb13u1+dhi2` | LOW | Build-component rationale | Yes |
| CVE-2025-69720 | `libtinfo6@6.5+20250216-2+dhi4` | HIGH | Patch claimed | No |
| CVE-2025-6141 | `libtinfo6@6.5+20250216-2+dhi4` | LOW | No-dsa only | No |
| CVE-2026-76642 | `libuuid1@2.41.5-0+deb13u1+dhi2` | HIGH | No-dsa only | No |
| CVE-2026-78408 | `libuuid1@2.41.5-0+deb13u1+dhi2` | HIGH | No-dsa only | No |
| CVE-2026-78409 | `libuuid1@2.41.5-0+deb13u1+dhi2` | HIGH | No-dsa only | No |
| CVE-2026-78410 | `libuuid1@2.41.5-0+deb13u1+dhi2` | HIGH | No-dsa only | No |
| CVE-2026-3184 | `libuuid1@2.41.5-0+deb13u1+dhi2` | MEDIUM | No-dsa only | No |
| CVE-2022-0563 | `libuuid1@2.41.5-0+deb13u1+dhi2` | LOW | Build-component rationale | No |
| CVE-2025-69720 | `ncurses-base@6.5+20250216-2+dhi4` | HIGH | Patch claimed | No |
| CVE-2025-6141 | `ncurses-base@6.5+20250216-2+dhi4` | LOW | No-dsa only | No |
| CVE-2025-69720 | `ncurses-bin@6.5+20250216-2+dhi4` | HIGH | Patch claimed | No |
| CVE-2025-6141 | `ncurses-bin@6.5+20250216-2+dhi4` | LOW | No-dsa only | No |

## Saved vendor rationale

The following are summaries of vendor statements in the cached package VEX documents, not independently verified conclusions. Full original statements, product scopes, timestamps and file hashes are preserved in `dist/mcp-vex-reconciliation/filtered-review.json`.

### Build-component rationale

CVE-2025-70873 cites an omitted SQLite zipfile extension; CVE-2022-0563 cites util-linux build options disabling chfn/chsh. Confirm those claims in this exact runtime.

### Disputed or usage-dependent

These statements cite disputed CVE classifications, upstream security exceptions, or usage conditions such as malicious regex patterns, database files, ELF files or unbounded globbing. A dispute or generic usage assumption does not by itself prove non-applicability for this application.

### Execution-path rationale

CVE-2026-5435 and CVE-2026-6238 cite deprecated libresolv diagnostic helpers, distinguishing them from normal resolver calls. This still needs assessment of the complete service and native dependencies.

### No-dsa only

The matching package statements cite Debian no-dsa decisions without an additional technical rationale. These require further evidence for every listed finding.

### Patch claimed

CVE-2026-19499 cites a glibc strfmon backport. CVE-2025-69720 cites an ncurses patch, appearing against four installed packages. Confirm the signed image assessment and package build evidence before resolution.

## Additional questions for Docker — draft, not sent

Please provide digest-bound, signed applicability evidence for the filtered findings as well as the 14 retained findings. In particular:

- Confirm the exact package builds containing the glibc and ncurses backports and the omitted SQLite/util-linux components.
- Explain the technical basis for every no-dsa-only non-applicability assertion.
- Confirm whether generic package statements are intended to cover older or unpatched versions, and provide precise version/platform scope.
- Provide authoritative references and image-specific justification for disputed and usage-dependent CVEs.

The current strict gate and the proposed evidence-based gate both remain blocked. A complete assessment requires reviewing all 47 raw findings, not merely making the remaining 14 disappear.

## Direct runtime inspection

Restored the saved image archive and confirmed Docker image identity `sha256:8696c8247b717d644a30c0c72f4c072a5b6db21b55cdc32e7c61ca82ffb77f77` before inspection. The inspection ran nonroot, read-only, without network or capabilities and with new privileges disabled.

- SQLite reports version 3.46.1. The initial connection lists no zipfile module or zip-named function. A filename search under `/usr/lib` returned only Python's standard-library `zipfile` directory, not a SQLite extension. This supports the vendor's omitted-extension rationale for CVE-2025-70873; it is not complete build attestation or a proof covering dynamically loaded extensions.
- `chfn`, `chsh`, `ldd` and `bzip2recover` are absent from the runtime PATH. This supports further review of utility-specific findings but does not prove that no copy exists anywhere in the filesystem or that library code is absent.
- SQLite **does enable FTS5 and Session extensions**. The retained FTS5/Session findings therefore cannot be dismissed on the assumption those features were omitted. Whether the service exposes an affected code path remains a separate question.

No exclusion was approved from these checks. The runtime output is saved in `dist/mcp-vex-reconciliation/runtime-component-review.json`. The field named `zipfile_shared_objects` is a filename glob result and can include directories; it is not an ELF-library inventory.

Runtime inspection output SHA-256: `afbff42c00bd571cac222eee07cd26683848193d4f1530eece9fdd19fe398d14`.
