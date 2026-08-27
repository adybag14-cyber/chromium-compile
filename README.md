# Chromium i686 — Unofficial Linux and Windows Builds

A pair of independent, resumable GitHub Actions pipelines for maintaining unofficial 32-bit Linux and Windows (`i686` / `x86`) builds of current Chromium stable releases.

Chromium upstream no longer supports or tests desktop Linux i686. This repository therefore treats each stable release as a downstream-port compatibility event: detect it, run a fast preflight, apply maintained patches, compile through resumable stages, validate the resulting ELF32 binary, and publish or record the failure.

Windows x86 remains source-supported by Chromium, but current Chromium revisions move faster than hosted-runner SDK images. The Windows lane therefore treats the exact source tag—not a fixed runner image—as the toolchain authority, derives its Visual Studio and Windows SDK requirements from that source, installs a missing official SDK family, and validates every packaged executable and DLL as PE32 Intel i386 before publication.

## Proven baseline

The pipeline successfully compiled and linked:

```text
Chromium version: 150.0.7871.186
Target OS:        Linux
Target CPU:       x86 / i686
Final Ninja step: [21/21] LINK ./chrome
Result:           Successful
```

Verified build artifact:

```text
Name:        chromium-150.0.7871.186-linux-i686
Artifact ID: 8688377641
Size:        155,797,042 bytes
```

The build resumed on fresh GitHub-hosted runners without losing completed Ninja work.

## Long-term support model

For every newly observed Linux stable version, the automation aims to produce one of these durable states:

1. an unofficial GitHub Release containing a validated Linux i686 build;
2. an active compatibility preflight or staged build; or
3. an open maintenance issue describing the upstream breakage.

Only one port version owns the queue at a time. A failed version is not repeatedly dispatched, but later stable versions still receive their own compatibility attempt. Completed terminal preflight/build/publisher runs are recent backup quarantine records; maintenance issues mirror terminal state for humans and releases are the durable success record. Manual `force_version` bypasses historical quarantine for a deliberate retry, but never bypasses an active port owner.
The watcher also requires the authoritative `chromium-browser-official/chromium-<version>.tar.xz` GCS object to exist and pass bounded metadata validation before it dispatches a compatibility preflight. Chrome VersionHistory can lead Chromium source publication; a clean GCS 404 is therefore reported as **source pending** and retried later without creating a failed preflight/quarantine record. Other metadata/network errors still fail the watcher closed. The preflight repeats the same readiness check so direct/manual dispatches cannot turn normal source-publication lag into a maintenance quarantine.

This process cannot guarantee that every future source release remains portable to i686 without additional work. It guarantees detection, an attempted port, validation, and visible failure reporting.

## Pipeline

```text
Google stable version feed
          │
          ▼
Stable release watcher
          │
          ▼
Compatibility preflight
  ├─ download exact source version
  ├─ install Chromium toolchain and i386 sysroot
  ├─ apply common and major-specific port patches
  ├─ generate the Linux x86 GN graph
  └─ confirm the Ninja chrome target exists
          │
          ▼
Resumable staged build
  ├─ compile for a bounded time
  ├─ preserve out/Release_x86
  ├─ restore on a fresh runner
  └─ repeat until chrome links
          │
          ▼
Trusted release workflow
  ├─ verify package SHA-256
  ├─ extract the runtime
  ├─ require ELF32 / Intel 80386
  └─ publish the unofficial release
```

Failures after automatic retries create or update an issue named:

```text
[i686-port] Chromium <version> requires maintenance
```

## Parallel Windows i686 lane

The Windows path is intentionally independent from the proven Linux queue. A Windows SDK, Visual Studio image, compile, checkpoint, smoke, or publisher failure cannot cancel or serialize the Linux lane, and its state uses separate workflow names, concurrency groups, artifacts, release tags, and maintenance issues.

```text
Chrome Windows stable feed
          │
          ▼
Windows compatibility preflight
  ├─ verify the exact GCS source object and Gitiles-critical files
  ├─ prove upstream still declares target_os="win" / target_cpu="x86"
  ├─ derive Visual Studio + SDK requirements from build/vs_toolchain.py
  ├─ install a missing Microsoft.WindowsSDK.<family> package, including x86 libs
  │  and Windows Desktop Debuggers
  ├─ install Chromium-pinned depot_tools, GN, Ninja, CPython, Clang, and rc.exe
  ├─ generation/size/SHA-pin the DEPS-declared Windows Rust + libclang objects
  ├─ reject unsafe, linked, oversized, duplicate, or Windows-aliasing tool archives
  └─ generate/query and dry-run the complete chrome + mini_installer Ninja graph
          │
          ▼
Resumable Windows staged build
  ├─ compile chrome + mini_installer for a bounded 325-minute slice
  ├─ stop only the exact Ninja process tree on timeout/stall
  ├─ preserve out/Release_x86_win with PAX/subsecond metadata
  ├─ accept checkpoints only from the same repo/ref/SHA/version/stage/toolchain,
  │  including the canonical Rust/libclang GCS descriptor hash
  └─ retain two generations and retry only runner/environment failures
          │
          ▼
Trusted Windows release workflow
  ├─ validate the exact successful build run and three-file artifact
  ├─ reject unsafe/duplicate ZIP paths and verify every EXE/DLL as PE32 i386
  ├─ extract and launch a local headless DOM smoke on a fresh Windows runner
  └─ create or resume a draft-first, digest-identical immutable release
```

Windows failures use a separate exact-version issue:

```text
[windows-i686-port] Chromium <version> requires maintenance
```

The initial Windows watcher baseline is `153.0.8010.12`. Run **Chromium Windows i686 Compatibility Preflight** manually for that baseline; the scheduled Windows watcher independently handles later Windows stable versions whose authoritative source object is ready.

Optional Windows repository variables are bounded before heavyweight work:

```text
CHROMIUM_WINDOWS_I686_RUNNER=windows-2025-vs2026
CHROMIUM_WINDOWS_I686_MAX_STAGES=20
CHROMIUM_WINDOWS_I686_CHECKPOINT_MINUTES=325
CHROMIUM_WINDOWS_I686_NINJA_STALL_MINUTES=90
CHROMIUM_WINDOWS_I686_JOBS=4
CHROMIUM_WINDOWS_RUNNER_RETRIES=2
```

Production runner selection accepts only `windows-2025-vs2026` or the explicitly reviewed `windows-2025` alias. `windows-latest` is deliberately rejected so an image migration cannot silently change the production compiler environment. Within that reviewed runner family, Chromium's source-declared SDK family is still dynamic: for example Chromium 152 uses SDK `10.0.26100.0`, while Chromium 153 requires `10.0.28000.0`. The preflight installs the matching official Microsoft package only when the family is absent and proves its servicing version meets Chromium's documented minimum.

## Why checkpoint restoration works

A normal archive was not sufficient. Ninja initially restarted at approximately `[1/57146]` after every runner change because restored outputs appeared stale.

The working implementation:

- stores checkpoints with POSIX/PAX metadata so subsecond mtimes survive;
- normalises recreated source, symlink and directory mtimes;
- restores `.ninja_log`, `.ninja_deps`, `build.ninja` and `args.gn`;
- avoids unconditional `gn gen` after a valid restore;
- reports Ninja dirty-state explanations before continuing.

After these fixes, the final build resumed with only 21 operations remaining.

## i386 host requirements

Some Chromium build tools generated near the end of compilation are themselves 32-bit executables. The pipeline defines the baseline as required **SONAMEs**, not Ubuntu package names. On each runner release it resolves those SONAMEs to installable `:i386` packages, which allows package renames across LTS releases without changing Chromium's ABI contract.

Only actual ELF32 executables/PIE executables are host-runtime checked. ELF32 shared objects such as Qt shims are target build artifacts and are deliberately excluded, preventing target-only libraries from becoming accidental host dependencies. Package installation, package simulation, and the rare `apt-file` fallback all have hard time limits so dependency discovery cannot consume an entire six-hour compiler stage.

The restored/generated build tools are checked with `file` and `ldd`; missing host libraries are repaired only for tools that can actually execute on the runner.

## Repository layout

```text
.github/
├── actions/
│   ├── chromium-i686-stage/action.yml
│   └── chromium-windows-i686-stage/action.yml
├── scripts/
│   ├── chromium_i686_common.sh
│   ├── chromium_i686_port.sh
│   ├── chromium_i686_resume.sh
│   └── runner_diagnostics.sh
└── workflows/
    ├── chromium-i686-preflight.yml
    ├── chromium-i686.yml
    ├── chromium-windows-i686-preflight.yml
    ├── chromium-windows-i686.yml
    ├── publish-i686-release.yml
    ├── publish-windows-i686-release.yml
    ├── report-i686-build-failure.yml
    ├── validate-port-infrastructure.yml
    ├── watch-chromium-stable.yml
    └── watch-chromium-windows-stable.yml

patches/
├── common/
└── versions/<major>/

scripts/chromium_stable_watcher.py
scripts/chromium_windows_pipeline.py
scripts/chromium_windows_runtime.py
scripts/github_immutable_release.py
support/baseline.json
tests/
docs/MAINTENANCE.md
```

## Patch policy

`patches/common` contains strict semantic changes that are expected to apply across supported releases. The current common patch enables the guarded Linux x86 GN target only when the exact expected upstream expression is present.

Major-specific unified patches belong under:

```text
patches/versions/<major>/*.patch
```

They are checked with `git apply --check` before being applied. If upstream changes the expected source, preflight stops before the expensive build begins.

## Safe activation

Scheduled discovery is disabled unless this repository variable is set:

```text
CHROMIUM_I686_AUTOMATION_ENABLED=true
```

Recommended rollout after merging the infrastructure:

1. Run **Watch Chromium Stable for i686** manually with `dry_run=true`.
2. Run **Chromium i686 Compatibility Preflight** with an exact version and `dispatch_build=false`.
3. Confirm the preflight evidence artifact.
4. Confirm repository workflow permissions permit Actions dispatch and release publication.
5. Set `CHROMIUM_I686_AUTOMATION_ENABLED=true`.

Optional repository variables:

```text
CHROMIUM_I686_MAX_STAGES=20
CHROMIUM_I686_NINJA_STALL_MINUTES=90
CHROMIUM_RUNNER_RETRIES=2
CHROMIUM_I686_RUNNER=ubuntu-24.04
```

`CHROMIUM_I686_RUNNER` is resolved by a fast `ubuntu-latest` guard before any heavyweight job is scheduled. The fail-closed default is `ubuntu-24.04`; `ubuntu-22.04` remains an explicit fallback only while it continues to pass the compatibility matrix. The allowlist/default live in the reusable `.github/workflows/resolve-i686-production-runner.yml` policy, and build, preflight, validation, and release runtime smoke all consume that one policy. Production accepts only explicit LTS labels continuously exercised by the compatibility matrix: `ubuntu-22.04` or `ubuntu-24.04`. `ubuntu-latest` is matrix-only so a GitHub image migration cannot move production without a reviewed change. Add a new runner label to the matrix and resolver in the same reviewed change; an unknown label fails immediately and opens maintenance state instead of leaving the global port queue waiting for a nonexistent runner.

The watcher checks every six hours when enabled.

## Manual compatibility test

Open **Actions → Chromium i686 Compatibility Preflight → Run workflow**.

Use:

```text
version:        exact four-part Chromium version
dispatch_build: false
```

When preflight passes, rerun it with `dispatch_build=true` to start stage 1.

## Manual build or recovery

Open **Actions → Chromium i686 Build (Unofficial Port) → Run workflow**.

Select the repository **default branch** when starting either a manual compatibility preflight or a manual build/recovery lineage. Non-default workflow runs are validation-only and do not automatically continue or recover production stages.

Fresh build:

```text
stage:                         1
version:                       exact pinned version
lineage_sha:                   empty for a new manual lineage
preferred_checkpoint_run_id:  empty
fallback_checkpoint_run_id:   empty
older_checkpoint_run_id:      empty
retry_count:                   0
```

Same-stage recovery:

```text
stage:                         failed stage number
version:                       identical pinned version
lineage_sha:                   empty for a new manual recovery lineage
preferred_checkpoint_run_id:  run containing that stage checkpoint
fallback_checkpoint_run_id:   previous-stage run when available
older_checkpoint_run_id:      leave empty for a manual recovery; automatic lineage manages this
retry_count:                   1 or 2
```

Stages automatically dispatch their successor until the build completes or reaches the configured limit. An automatic lineage carries the workflow commit SHA from preflight through every continuation and fresh-runner recovery. Before dispatch, the exactly-once helper confirms that the default branch still resolves to that SHA; after dispatch it confirms the materialized run reports the same `headSha`. Each stage independently rejects a mismatched `lineage_sha`, and write-capable continuation/pruning/reporting helpers are checked out from `${{ github.sha }}` rather than a moving branch tip. Duplicate detection is server-scoped by workflow + branch and, for immutable lineages, exact commit; active-run checks query only GitHub incomplete statuses, while parent/run-confirmation checks also push their creation-time lower bound into GitHub before applying the bounded 1,000-run saturation guard. This keeps the fail-closed lookup meaningful across years of staged builds instead of eventually filling it with unrelated history. If the default branch changes during a long compile, the current checkpoint is preserved and automatic continuation fails closed instead of mixing workflow generations. A maintainer may then review the change and start a new manual lineage with `lineage_sha` empty; normal checkpoint compatibility/provenance checks still decide whether older compiled state is reusable.

## Release validation

A successful compiler workflow uploads:

```text
chromium-<version>-linux-i686.tar.xz
chromium-<version>-linux-i686.tar.xz.sha256
chromium-<version>-linux-i686-manifest.txt
```

A separate default-branch workflow then:

- downloads the exact build artifact;
- verifies the package checksum;
- extracts `chrome`;
- checks it with `file` and `readelf`;
- requires `ELF32` and `Intel 80386`;
- verifies or creates the exact `refs/tags/chromium-<version>-linux-i686` ref at the validated build SHA;
- creates/resumes a draft release only after that tag provenance is fixed;
- publishes only after every retained asset digest matches, while an already-published release remains immutable.

Release publication is separated from compilation so a permissions or publishing problem cannot erase the successful build artifact. Release creation never relies on `target_commitish` to correct an existing tag; GitHub ignores that field for pre-existing tags, so the Git ref itself is verified first.

## Maintenance

See [docs/MAINTENANCE.md](docs/MAINTENANCE.md) for:

- handling newly broken stable releases;
- deciding between common and major-specific patches;
- retry and issue behaviour;
- safe automation activation;
- the release trust boundary.

## Limitations

This is an unofficial downstream port:

- it is not Google Chrome;
- it is not an upstream Chromium release binary;
- Linux i686 is unsupported and untested by Chromium upstream;
- future releases may require source, V8, Rust, sandbox, graphics or dependency patches;
- GitHub-hosted runner images and Ubuntu repositories can change;
- generated releases must be tested on real 32-bit Linux systems before production use.

## Project goal

> Continue attempting every future Chromium stable release through independent Linux and Windows i686 lanes, publish every successful validated build, and make every upstream or hosted-toolchain incompatibility visible and maintainable.


## Pipeline resilience hardening

The staged builder now treats the GitHub-hosted runner as an untrusted, replaceable execution environment rather than assuming that its image remains stable.

- Runner platform detection is isolated from workflow variables: `/etc/os-release` is sourced only inside a subshell, and Chromium inputs use collision-resistant names so generic distro keys such as `VERSION` cannot overwrite the requested Chromium version.
- Required i386 host libraries are expressed as SONAMEs and resolved against the active Ubuntu release instead of assuming one LTS package naming scheme.
- Generated ELF32 **executables** are discovered dynamically; ELF32 shared target objects are excluded. Missing host libraries are repaired in bounded cycles only when the host actually changes.
- Compiler failures are classified as `runtime_environment`, `infrastructure`, or `deterministic_build`. Deterministic compiler/source failures do not consume fresh-runner retries.
- Checkpoints include a SHA-256 sidecar and JSON manifest recording Chromium version, stage, source checksum, clang revision, GN/patch configuration hash, Ninja metadata hashes, workflow commit, and runner image metadata.
- Restores verify the compression stream, checksum, target/version/stage compatibility, source/toolchain/configuration identity, and extracted `args.gn` / `build.ninja` hashes before reuse.
- Legacy checkpoints without manifests are rejected by default. A single in-flight Chromium version can be temporarily opted in with `CHROMIUM_ALLOW_LEGACY_CHECKPOINT_VERSION=<version>`; the next checkpoint automatically gains the new metadata.
- Validation includes the production disk-cleanup and native build-prerequisite installation path, a real 32-bit compile-and-execute canary, plus an Ubuntu `22.04`, `24.04`, and `ubuntu-latest` compatibility matrix. The matrix also runs weekly so the next GitHub-hosted LTS image is exercised before production is switched. Scheduled failures automatically open/update an Ubuntu LTS compatibility-drift issue.
- Missing generated-tool SONAMEs use a controlled resolver: known mappings are preferences only, release-local package candidates are verified, and bounded Ubuntu i386 `apt-file` metadata is a last-resort fallback. An unavailable old mapping automatically falls through to discovery on a newer LTS.
- First-party GitHub Actions are upgraded and pinned to immutable commit SHAs.
- Disk-space guards trim expendable caches before compilation/checkpoint creation and fail before a near-full runner can destroy resumable state.
- Recent terminal preflight/build/publisher run history is an independent watcher quarantine source. The watcher reads only the three relevant workflows over a bounded three-year (1,095-day) horizon, bounds release/issue pagination, rejects repeated VersionHistory page tokens, and fails closed if a configured history horizon saturates instead of silently forgetting state.
- Stable-watcher and legacy-bootstrap dispatches execute helpers from `${{ github.workflow_sha }}` and require the target default branch to still resolve to that exact workflow commit before starting preflight. A branch move during either control-plane run therefore fails closed and the next run can retry from the newer workflow generation.
- Publisher validation/smoke/mutation/cleanup, the secondary failure reporter, preflight runner-configuration reporting, and validation maintenance writers also checkout helpers at `${{ github.workflow_sha }}`. A single workflow run therefore cannot silently switch helper implementations if the default branch moves between jobs.
- Workflow dispatches use an exactly-once helper: an already-active exact run suppresses a duplicate, and a client/network failure after `workflow_dispatch` is confirmed against Actions state before the write is considered failed. Non-idempotent dispatch calls are never blindly retried.
- Maintenance issues use an exact-title upsert helper with bounded reads and post-error confirmation. A timed-out issue creation cannot fall through to a second create, and a failed comment cannot erase the existing quarantine issue.
- Preflight and terminal build failures attempt same-run issue mirrors; the secondary `workflow_run` reporter also covers publisher failures as redundancy.
- The old Chromium 150 bootstrap is manual-only and refuses to redispatch when the baseline release tag already exists.
- Every compiler stage writes a compact GitHub job summary with progress, checkpoint size, failure classification, free disk and ccache statistics.
- Chromium's own `DEPS` file is the source of truth for the depot_tools commit and GN CIPD version; rolling `latest` host tooling is not used for production builds.
- Source tarballs are validated against the authoritative GCS object's generation, content length and MD5 while computing a local SHA-256. First-seen bytes also undergo safe-member validation before extraction. After extraction, `chrome/VERSION` must exactly match the requested version and critical files are compared against the authoritative Gitiles tag.
- Source cache contract `chromium-src-v4-<version>-<gcs-generation>` binds the immutable Actions cache key to the authoritative GCS object generation as well as the Chromium version. The cached payload stores the trust marker plus SHA-bound bounded archive stats (member count and declared unpacked bytes), all tied to version + GCS generation/length/MD5 + SHA-256 + successful safe-archive/Gitiles proofs. v3, v2, and older version-only keys are restore-only migration fallbacks: restored bytes are revalidated against the current authoritative object and current resource policy, then the trusted payload is saved under the v4 generation-bound key. Exact v4 bytes can skip redundant decompression/Gitiles scans on later stages while still rechecking the compressed object against GCS metadata and proving extraction capacity from trusted stats.
- The build includes Chromium's upstream `chrome/installer/linux:installer_deps` group. The standalone archive derives its binary/resource closure from that installer definition and renders Chromium's wrapper template as `chrome-wrapper`.
- Resumable output checkpoints are correctness state. Cross-stage Actions ccache persistence is intentionally disabled, the immutable source archive is cached once per Chromium version, and packaging/final-artifact failures attempt a same-stage final-output recovery checkpoint before the runner disappears.
- Release archives reject unsafe, duplicate and special tar members; every packaged ELF must be ELF32 Intel 80386. Manifest/checksum provenance must match the exact successful build run.
- Release tags target the exact build SHA and existing release assets are immutable. A trusted manual publisher can republish a retained successful build artifact by run ID after release-logic maintenance without recompiling Chromium.
- A completed final compiler run uses a lineage-bound release-handoff workflow to bridge to the publisher exactly once; it waits for terminal build success, prefers any legacy `workflow_run` publisher that appears, and otherwise uses `workflow_dispatch` so token-triggered staged builds cannot silently lose the publication handoff.


Checkpoint reuse is fail-closed: the producer run/artifact must match the repository, Chromium build workflow, current ref, version and compatible stage; archive members/links are containment-validated before extraction; then checksum/manifest source, toolchain and port-contract checks apply.

A compiler slice also has a bounded durable-progress watchdog: by default, if `out/Release_x86/.ninja_log` does not change for 90 minutes while `autoninja` is running, the process tree is terminated, the partial output is checkpointed, and the next slice gets a fresh runner. `CHROMIUM_I686_NINJA_STALL_MINUTES` may be set only from 30 through 180 minutes. The existing two-slice no-progress streak still stops a repeatedly wedged build, while the 340-minute checkpoint cutoff remains the absolute backstop.
