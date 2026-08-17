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
CHROMIUM_I686_NINJA_STALL_MINUTES=90
CHROMIUM_RUNNER_RETRIES=2
CHROMIUM_I686_RUNNER=ubuntu-24.04
# Temporary migration only; omit normally:
CHROMIUM_ALLOW_LEGACY_CHECKPOINT_VERSION=<exact-version>
```

## Release lifecycle

1. The stable watcher queries Google's VersionHistory API.
2. Versions at or below the verified baseline are ignored.
3. Existing release tags, active runs and open maintenance issues are treated as recorded states.
4. The oldest unseen version is sent to compatibility preflight.
5. Preflight applies strict common and major-version patches, installs the i386 sysroot and generates the GN/Ninja graph.
6. A successful preflight dispatches the resumable staged build. The preflight commit becomes the automatic compiler lineage SHA: every successor/recovery is dispatched only if the default branch still resolves to that same commit, and the dispatcher confirms the new run materialized at the expected `headSha`.
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

Automatic staged builds never silently cross a workflow-code update. `lineage_sha` defaults to the current `github.sha` only when a manual lineage starts, is carried through subsequent automatic stages/recoveries, and is checked again inside each build before compiler work. The dispatch helper also resolves the target branch immediately before the write and read-confirms the spawned run's `headSha`; a branch move at either side of the dispatch boundary fails closed. Write-capable build-lineage helper checkouts use the run SHA instead of the current default-branch tip. Run dedupe is filtered server-side by branch and exact lineage commit where known; active checks query only incomplete GitHub statuses, and parent/post-dispatch checks also use a GitHub creation-time lower bound before the bounded 1,000-result saturation check. Keep the local title/ref/SHA/timestamp checks as defence in depth. If this guard fires, keep the checkpoint and review the intervening repository change before manually starting a new lineage; do not bypass the SHA guard merely to keep a stale automatic chain moving.

Compiler failures are classified before the workflow decides whether a fresh runner is useful:

- `infrastructure`: runner storage, OOM, network or similar transient host failures. These may consume a runner retry.
- `runtime_environment`: a generated ELF32 tool cannot load a host shared library. Known SONAMEs are repaired in-place first; an unrepaired environment failure may consume a runner retry.
- `deterministic_build`: compiler, linker, GN, API or source incompatibilities. These do **not** consume fresh-runner retries and should become maintenance issues immediately.

Baseline host requirements live in `I386_BASELINE_SONAMES`; package names are resolved per runner release. `I386_SONAME_PACKAGES` contains preferred mappings for known dependencies, but an unavailable mapping falls through to release-local discovery. Unknown SONAMEs first try verified Debian/Ubuntu package-name candidates derived from the SONAME; bounded Ubuntu i386 `apt-file` metadata is a last-resort fallback and is accepted only when there is exactly one installable provider. Only ELF32 executables/PIE executables are host-runtime checked—shared objects are target artifacts. Keep the production canary green and the scheduled `ubuntu-22.04` / `ubuntu-24.04` / `ubuntu-latest` matrix healthy before changing `CHROMIUM_I686_RUNNER`; `ubuntu-26.04` is additionally exercised as a non-blocking preview sentinel while the GitHub-hosted image remains in public preview.

### LTS migration policy

`CHROMIUM_I686_RUNNER` controls the runner used by compatibility preflight, staged compilation, immutable-release runtime smoke drills, and the selected-runner validation canaries, but the raw repository variable is never passed directly to a heavyweight job. With no repository override, production defaults to `ubuntu-24.04`; `ubuntu-22.04` is retained only as an explicit compatibility-matrix-backed fallback. Runner allowlisting/defaulting is centralized in `.github/workflows/resolve-i686-production-runner.yml`, including the release runtime-smoke gate. A fast `ubuntu-latest` resolver accepts only explicit production LTS labels exercised by the compatibility matrix (`ubuntu-22.04` and `ubuntu-24.04`); both `ubuntu-latest` and preview `ubuntu-26.04` are intentionally matrix-only, so an unknown/mistyped value fails immediately, records maintenance state, and releases the global port queue instead of waiting indefinitely for a nonexistent runner. Leave production on the currently proven explicit LTS until the matrix is green on the replacement. Preview images may be added to the matrix as non-blocking early-warning sentinels, but must not enter the production resolver until they are GA and intentionally approved for production in a reviewed change; promote any new hosted-LTS label by adding it to both the compatibility matrix and centralized resolver in the same reviewed change. `ubuntu-latest` remains an early-warning sentinel for GitHub image migration and is never a production runner value. A scheduled matrix or runner-resolution failure opens or updates `[i686-port] Ubuntu LTS compatibility drift`; the next healthy scheduled or default-branch manual validation closes that issue idempotently. The source/tool-contract drift issue follows the same open-on-failure, close-on-recovery lifecycle. Platform metadata is parsed in an isolated subshell; never source `/etc/os-release` into workflow state because its generic variables (`VERSION`, `ID`, and others) can collide with build inputs.

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

APT index refreshes use a shorter bounded timeout than package installation. If a GitHub-hosted Ubuntu runner exposes an unresponsive `azure.archive.ubuntu.com`/`azure.ports.ubuntu.com` mirror, the pipeline rewrites only those Ubuntu archive hostnames to the canonical archive and retries the index refresh once. Package installation is never blindly retried after an uncertain mutation. Both update and install timeout overrides have hard ceilings.

Runner disk cleanup deliberately avoids optional `apt purge`/`autoremove`. Large expendable SDK/tool trees are removed directly and `apt-get clean` may discard package archives, but dpkg package state is left untouched until the required Chromium dependency install. Historical Jammy measurements showed the optional database-package purge/autoremove recovered only about 392 MB compared with roughly 18 GiB from direct SDK/tool cleanup, while adding a mirror/dpkg failure surface.

All configurable timeout knobs also have hard ceilings. The generic external-process boundary is capped at one hour even if a future call site misses a narrower wrapper; GitHub, checkpoint compression, cleanup/swap, runtime smoke, loader probes, and dependency discovery have tighter task-specific maxima. Invalid or oversized timeout text is rejected before the child command is started, so a repository/environment override cannot silently turn bounded maintenance into an hours-long operation.

The primary source archive cache key is `chromium-src-v4-<version>-<gcs-generation>`, so immutable Actions cache identity changes if GCS ever replaces an object under the same Chromium version. Every restore is checked against the authoritative GCS object's generation, stored length and MD5 while computing/confirming SHA-256. The trust marker remains reusable only when version, generation, length, MD5 and SHA-256 match and both safe-archive and Gitiles-identity proofs are present, together with a SHA-bound archive-stats sidecar whose member count and declared unpacked bytes satisfy the current hard-bounded policy. v3, v2, and older version-only cache keys are restore-only migration inputs: restored bytes are fully revalidated against the current authoritative object and policy, then saved under the v4 generation-bound key. The extracted `chrome/VERSION` is always checked, and free disk must cover declared unpacked bytes plus the configured reserve before extraction. This prevents immutable Actions caches from trapping the pipeline on stale same-version source bytes or a pre-hardening trust format while avoiding repeated decompression scans on later stages.

The staged `out/Release_x86` checkpoint—not Actions ccache—is the cross-run compilation state. Checkpoint creation/upload therefore precedes optional/performance work, and a completed compile whose package or final artifact upload fails attempts to publish a same-stage recovery checkpoint.

Two consecutive planned compiler slices with an non-increasing `.ninja_log` completed-entry count are treated as a deterministic stalled build. The first zero-progress slice is checkpointed with a provenance-bound streak marker; any completed Ninja command resets the streak to zero. A second consecutive zero-progress cutoff still preserves/uploads its checkpoint for diagnosis but stops automatic staged runners instead of burning the remaining stage budget.

A compiler slice also has a bounded durable-progress watchdog: by default, if `out/Release_x86/.ninja_log` does not change for 90 minutes while `autoninja` is running, the process tree is terminated, the partial output is checkpointed, and the next slice gets a fresh runner. `CHROMIUM_I686_NINJA_STALL_MINUTES` may be set only from 30 through 180 minutes. The existing two-slice no-progress streak still stops a repeatedly wedged build, while the 340-minute checkpoint cutoff remains the absolute backstop.

The standalone tarball is derived from Chromium's Linux `installer_deps` runtime contract, includes a rendered `chrome-wrapper`, rejects unsafe/duplicate/special archive members, and validates every ELF as 32-bit Intel 80386. The release manifest records source/package hashes, build run/SHA, clang revision, GN/depot_tools pins, port hash, checkpoint contract and runner image.

GitHub Releases are immutable provenance records. Before a release is created, the publisher resolves the exact `refs/tags/<tag>` ref; an existing tag is accepted only when it dereferences to the successful build SHA, while a missing tag is created once at that SHA and read-confirmed before release mutation. Release creation therefore does not depend on `target_commitish` (GitHub ignores it for pre-existing tags). Every remote asset digest must equal the local artifact; never restore `--clobber`. If release logic changes after a successful build, use **Publish Chromium i686 Release → Run workflow** with the retained successful `build_run_id`; the publisher independently verifies workflow identity, repository, default branch, completion, provenance and artifact contents before publication.

## Checkpoint provenance and extraction

Checkpoint artifacts are provenance-bound to the same repository, chromium-i686.yml, current default branch, expected Chromium version/compatible producer stage, and exactly one non-expired artifact. A streaming pre-extraction validator rejects traversal, absolute paths, special files, duplicate members, escaping or broken links, and missing Ninja graph files. Existing checksum, source, toolchain, port-configuration and checkpoint-contract manifest validation remains mandatory.

Automatic checkpoint retention keeps two historical generations rather than every stage indefinitely. A running stage uses the immediately previous completed stage as `fallback_checkpoint_run_id` and carries one older generation in `older_checkpoint_run_id` for emergency/manual recovery. After a bot-dispatched successor is read-confirmed, only the now-third-oldest checkpoint and any superseded same-stage recovery checkpoint are eligible for best-effort pruning. Manual workflow dispatches never prune historical checkpoints, failed/recovering stages never prune their active fallback chain, and deletion revalidates repository/workflow/default-branch/version/stage/artifact provenance before removing exactly one Actions artifact. The nominal 14-day artifact retention remains an emergency window for the newest retained generations, not a requirement to retain every compiler stage.

After the trusted publisher has revalidated provenance, executed the packaged i686 browser, and published an immutable release, a non-blocking `actions: write` cleanup reclaims that version's remaining checkpoint artifacts and version-scoped source cache. The cleanup revalidates the release tag/build SHA and refuses while the same Chromium version still has an active build, so publication storage reclamation cannot race resumable compilation.

## Control-plane state and idempotence

The watcher intentionally reads bounded, status-filtered history instead of paginating the repository's entire lifetime on every six-hour run. It queries only the preflight, staged-build, and trusted publisher workflows and only the active states plus terminal quarantine conclusions; successful compiler stages never consume the quarantine horizon. The backup quarantine window remains 1,095 days (three years) with at most 1,000 runs per workflow/status filter, and every filtered response is locally verified so an ignored GitHub status filter fails closed. Open maintenance issues remain the durable human failure record, while only a healthy immutable release with the complete uploaded/digested asset set counts as durable success. VersionHistory, releases and open-issue collections also have explicit pagination limits; repeated page tokens or saturated limits are maintenance failures, never silent truncation.

A manual `force_version` bypasses historical quarantine only after a fix. It does **not** bypass an active port run, a healthy immutable release, or the baseline/older history; use the manual preflight workflow for historical testing. The global queue remains single-owner.

All workflow dispatches that can start expensive work should use `scripts/github_workflow_dispatch.py`. The helper performs an exact-active-run guard and sends the non-idempotent `workflow_dispatch` write only once. If the client times out or loses the response, it polls the exact expected run title and accepts server-side evidence rather than blindly issuing a duplicate write.

Maintenance issue creation should use `scripts/github_maintenance_issue.py`. Exact-title lookup is bounded to 1,000 open issues, duplicate exact titles fail closed, a create timeout is confirmed by rereading state rather than retried, and failure to append a comment never triggers a duplicate issue. Preflight and terminal staged-build failures attempt same-run issue mirrors; the secondary failure reporter is redundancy and also covers trusted publisher failures.

`bootstrap-i686-live.yml` is now manual-only. It first checks the immutable Chromium 150 release tag and will not dispatch a released baseline. It exists only as historical/bootstrap recovery tooling, not as part of normal stable-version automation. Bootstrap no longer has repository-content write permission or commits status files; it reports status only in the workflow summary, and all production write-capable manual workflows require trusted default-branch workflow code.
 Both bootstrap and the six-hour stable watcher pin their helper checkout to `github.workflow_sha` and pass that SHA to the exactly-once dispatcher; if the default branch moves before dispatch, the starter run fails closed instead of launching preflight from a different workflow generation.

### Linux sandbox executable policy

The standalone runtime archive requires `chrome_sandbox` (and the other Chromium helper binaries) to retain execute permission. The archive intentionally does **not** require setuid/root ownership: whether to install the legacy setuid sandbox with elevated ownership/mode is a target-system deployment policy. Runtime CI uses `--no-sandbox` only for the isolated headless smoke test so the package can be validated on an unprivileged GitHub runner without mutating host security policy.
