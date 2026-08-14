# Maintaining the unofficial Chromium Linux i686 port

## Project contract

The automation guarantees that every newly observed Linux stable version is either:

1. published as an ELF32 i686 release;
2. actively being built; or
3. represented by an open maintenance issue.

It cannot guarantee that upstream Chromium will remain technically portable to i686. Chromium no longer supports or tests this target, so each stable release is treated as a new downstream-port compatibility event.

## Safe activation

Scheduled discovery is intentionally gated. After this branch is reviewed and merged, create the repository variable:

```text
CHROMIUM_I686_AUTOMATION_ENABLED=true
```

Before enabling it, manually run **Watch Chromium Stable for i686** with `dry_run=true`.

Optional repository variables:

```text
CHROMIUM_I686_MAX_STAGES=20
CHROMIUM_RUNNER_RETRIES=2
CHROMIUM_I686_RUNNER=ubuntu-22.04
# Temporary migration only; omit normally:
CHROMIUM_ALLOW_LEGACY_CHECKPOINT_VERSION=<exact-version>
```

## Release lifecycle

1. The stable watcher queries Google's VersionHistory API.
2. Versions at or below the verified baseline are ignored.
3. Existing release tags, active runs and open maintenance issues are treated as recorded states.
4. The oldest unseen version is sent to compatibility preflight.
5. Preflight applies strict common and major-version patches, installs the i386 sysroot and generates the GN/Ninja graph.
6. A successful preflight dispatches the resumable staged build.
7. The final artifact is checksum-verified and inspected with `file` and `readelf`.
8. A separate trusted workflow creates the unofficial GitHub Release.
9. Failed/cancelled preflight/build run history immediately quarantines that exact version from automatic redispatch. Preflight also creates or updates a maintenance issue in the same run; the issue is a human-visible mirror, not the sole safety record.

## Porting a broken major release

1. Open the automatically generated issue and identify the first real compatibility failure.
2. Reproduce the failure in a short preflight wherever possible.
3. Update `patches/common` only when the change is valid across supported majors.
4. Put release-specific unified patches under `patches/versions/<major>/`.
5. Document the reason and upstream file in `patches/versions/<major>/README.md`.
6. Run the preflight manually with `dispatch_build=false`.
7. Close the maintenance issue only when the preflight passes.
8. Rerun the watcher with `force_version=<exact version>` and `dry_run=false`.

## Security boundary

The release workflow accepts artifacts only from a successful build on the repository's default branch and from the same repository. It validates the package checksum and confirms that `chrome` is an ELF32 Intel 80386 executable before publishing.


## Failure classification and retries

Compiler failures are classified before the workflow decides whether a fresh runner is useful:

- `infrastructure`: runner storage, OOM, network or similar transient host failures. These may consume a runner retry.
- `runtime_environment`: a generated ELF32 tool cannot load a host shared library. Known SONAMEs are repaired in-place first; an unrepaired environment failure may consume a runner retry.
- `deterministic_build`: compiler, linker, GN, API or source incompatibilities. These do **not** consume fresh-runner retries and should become maintenance issues immediately.

Baseline host requirements live in `I386_BASELINE_SONAMES`; package names are resolved per runner release. `I386_SONAME_PACKAGES` contains preferred mappings for known dependencies, but an unavailable mapping falls through to release-local discovery. Unknown SONAMEs first try verified Debian/Ubuntu package-name candidates derived from the SONAME; bounded Ubuntu i386 `apt-file` metadata is a last-resort fallback and is accepted only when there is exactly one installable provider. Only ELF32 executables/PIE executables are host-runtime checked—shared objects are target artifacts. Keep the production canary green and the scheduled `ubuntu-22.04` / `ubuntu-24.04` / `ubuntu-latest` matrix healthy before changing `CHROMIUM_I686_RUNNER`.

### LTS migration policy

`CHROMIUM_I686_RUNNER` controls the runner used by compatibility preflight and staged compilation. Leave it on the currently proven LTS until the validation matrix is green on the replacement. `ubuntu-latest` is intentionally included as an early-warning sentinel: when GitHub moves it to a newer LTS, the scheduled matrix starts testing that image automatically. A scheduled matrix failure opens or updates `[i686-port] Ubuntu LTS compatibility drift`, so runner drift becomes an explicit maintenance item rather than a silently red workflow. Platform metadata is parsed in an isolated subshell; never source `/etc/os-release` into workflow state because its generic variables (`VERSION`, `ID`, and others) can collide with build inputs.

No dependency-discovery or large cleanup operation is allowed to consume a compiler stage. SDK-tree deletion, source/output replacement, and swap creation are time-bounded; cleanup timeouts are either best-effort with later disk guards or classified as runner failures. APT operations have a global timeout, `apt-file` metadata refresh has a shorter discovery timeout, individual content searches are separately bounded, and discovery exhaustion is treated as deterministic maintenance work rather than repeated on fresh runners.

New checkpoints are a three-file bundle:

```text
out-Release_x86.tar.zst
out-Release_x86.tar.zst.sha256
checkpoint-manifest.json
```

Legacy checkpoints without the manifest/checksum bundle are rejected by default. During a one-version migration, set `CHROMIUM_ALLOW_LEGACY_CHECKPOINT_VERSION` to that exact Chromium version and remove or change it after the in-flight chain has produced a manifest-bearing checkpoint. Do not remove manifest compatibility checks merely to make a stale checkpoint restore. If build-affecting GN arguments or downstream patches change, starting from a compatible older-stage checkpoint or a fresh output directory is safer than reusing mismatched Ninja state.

## Reproducible toolchain and release invariants

Production builds must use the `gn_version` and depot_tools commit declared by the requested Chromium source `DEPS`. Do not replace these with `latest` tags. The validation workflow probes the latest stable Chromium `DEPS` and Linux installer definition on every relevant PR and on the scheduled compatibility run; source-contract drift becomes a maintenance issue before the next large build.

The source archive cache uses the versioned key `chromium-src-v2-<version>`. Every restore is streamed against the authoritative GCS object's generation, stored length and MD5 while computing SHA-256. A v2 marker is reusable only when version, generation, length, MD5 and SHA-256 match and the marker records both safe-archive and Gitiles-identity proofs. An old `chromium-src-<version>` cache can be restored only as migration input: it receives the full safe-member/Gitiles validation and is then saved under v2. The extracted `chrome/VERSION` is always checked. This prevents immutable Actions caches from trapping the pipeline on a pre-hardening trust format while avoiding repeated decompression scans of the multi-gigabyte source tar on every stage.

The staged `out/Release_x86` checkpoint—not Actions ccache—is the cross-run compilation state. Checkpoint creation/upload therefore precedes optional/performance work, and a completed compile whose package or final artifact upload fails attempts to publish a same-stage recovery checkpoint.

The standalone tarball is derived from Chromium's Linux `installer_deps` runtime contract, includes a rendered `chrome-wrapper`, rejects unsafe/duplicate/special archive members, and validates every ELF as 32-bit Intel 80386. The release manifest records source/package hashes, build run/SHA, clang revision, GN/depot_tools pins, port hash, checkpoint contract and runner image.

GitHub Releases are immutable provenance records. Before a release is created, the publisher resolves the exact `refs/tags/<tag>` ref; an existing tag is accepted only when it dereferences to the successful build SHA, while a missing tag is created once at that SHA and read-confirmed before release mutation. Release creation therefore does not depend on `target_commitish` (GitHub ignores it for pre-existing tags). Every remote asset digest must equal the local artifact; never restore `--clobber`. If release logic changes after a successful build, use **Publish Chromium i686 Release → Run workflow** with the retained successful `build_run_id`; the publisher independently verifies workflow identity, repository, default branch, completion, provenance and artifact contents before publication.

## Checkpoint provenance and extraction

Checkpoint artifacts are provenance-bound to the same repository, chromium-i686.yml, current default branch, expected Chromium version/compatible producer stage, and exactly one non-expired artifact. A streaming pre-extraction validator rejects traversal, absolute paths, special files, duplicate members, escaping or broken links, and missing Ninja graph files. Existing checksum, source, toolchain, port-configuration and checkpoint-contract manifest validation remains mandatory.

## Control-plane state and idempotence

The watcher intentionally reads a bounded recent history instead of paginating the repository's entire lifetime on every six-hour run. It queries only the preflight, staged-build, and trusted publisher workflows, defaults to a 1,095-day (three-year) quarantine window and at most 1,000 runs per relevant workflow, and fails closed if that horizon is saturated. Open maintenance issues remain the durable human failure record, while only a healthy immutable release with the complete uploaded/digested asset set counts as durable success. VersionHistory, releases and open-issue collections also have explicit pagination limits; repeated page tokens or saturated limits are maintenance failures, never silent truncation.

A manual `force_version` bypasses historical quarantine only after a fix. It does **not** bypass an active port run, a healthy immutable release, or the baseline/older history; use the manual preflight workflow for historical testing. The global queue remains single-owner.

All workflow dispatches that can start expensive work should use `scripts/github_workflow_dispatch.py`. The helper performs an exact-active-run guard and sends the non-idempotent `workflow_dispatch` write only once. If the client times out or loses the response, it polls the exact expected run title and accepts server-side evidence rather than blindly issuing a duplicate write.

Maintenance issue creation should use `scripts/github_maintenance_issue.py`. Exact-title lookup is bounded to 1,000 open issues, duplicate exact titles fail closed, a create timeout is confirmed by rereading state rather than retried, and failure to append a comment never triggers a duplicate issue. Preflight and terminal staged-build failures attempt same-run issue mirrors; the secondary failure reporter is redundancy and also covers trusted publisher failures.

`bootstrap-i686-live.yml` is now manual-only. It first checks the immutable Chromium 150 release tag and will not dispatch a released baseline. It exists only as historical/bootstrap recovery tooling, not as part of normal stable-version automation.
