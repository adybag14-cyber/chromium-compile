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
9. Failures create or update a maintenance issue and stop automatic redispatch loops.

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

`CHROMIUM_I686_RUNNER` controls the runner used by compatibility preflight and staged compilation. Leave it on the currently proven LTS until the validation matrix is green on the replacement. `ubuntu-latest` is intentionally included as an early-warning sentinel: when GitHub moves it to a newer LTS, the scheduled matrix starts testing that image automatically. A scheduled matrix failure opens or updates `[i686-port] Ubuntu LTS compatibility drift`, so runner drift becomes an explicit maintenance item rather than a silently red workflow.

No dependency-discovery operation is allowed to consume a compiler stage. APT operations have a global timeout, `apt-file` metadata refresh has a shorter discovery timeout, individual content searches are separately bounded, and discovery exhaustion is treated as deterministic maintenance work rather than repeated on fresh runners.

New checkpoints are a three-file bundle:

```text
out-Release_x86.tar.zst
out-Release_x86.tar.zst.sha256
checkpoint-manifest.json
```

Legacy checkpoints without the manifest/checksum bundle are rejected by default. During a one-version migration, set `CHROMIUM_ALLOW_LEGACY_CHECKPOINT_VERSION` to that exact Chromium version and remove or change it after the in-flight chain has produced a manifest-bearing checkpoint. Do not remove manifest compatibility checks merely to make a stale checkpoint restore. If build-affecting GN arguments or downstream patches change, starting from a compatible older-stage checkpoint or a fresh output directory is safer than reusing mismatched Ninja state.
