# Chromium i686 on GitHub Actions

A reproducible, resumable GitHub Actions pipeline for compiling upstream Chromium as a 32-bit Linux (`i686`) build on standard GitHub-hosted Ubuntu runners.

## Proven result

This repository successfully compiled and linked Chromium on GitHub Actions:

```text
Chromium version: 150.0.7871.186
Target OS:        Linux
Target CPU:       x86 / i686
Final Ninja step: [21/21] LINK ./chrome
Result:           Build completed successfully
```

The completed runtime package was uploaded as a GitHub Actions artifact:

```text
Artifact: chromium-150.0.7871.186-linux-i686
Artifact ID: 8688377641
Size: 155,797,042 bytes
```

The important result is not only that Chromium compiled, but that the build survived GitHub Actions job limits and continued correctly across fresh runners.

## Why the build is staged

A full Chromium build can run longer than one GitHub-hosted job. This project splits the build into timed stages:

1. Prepare the Chromium source, toolchain and i386 sysroot.
2. Compile for up to 350 minutes.
3. Stop before the GitHub job limit.
4. Archive `out/Release_x86` as a checkpoint.
5. Upload the checkpoint as an Actions artifact.
6. Restore it on a fresh runner.
7. Continue the same Ninja build.
8. Repeat until `chrome` links successfully.

The workflow supports up to 12 stages and can automatically retry a failed stage on a fresh runner.

## Repository structure

```text
.github/
├── workflows/
│   └── chromium-i686.yml
├── actions/
│   └── chromium-i686-stage/
│       └── action.yml
└── scripts/
    ├── chromium_i686_common.sh
    ├── chromium_i686_resume.sh
    └── runner_diagnostics.sh
```

| File | Responsibility |
|---|---|
| `.github/workflows/chromium-i686.yml` | Controls stages, retries and checkpoint hand-off |
| `.github/actions/chromium-i686-stage/action.yml` | Runs one complete compiler stage |
| `.github/scripts/chromium_i686_common.sh` | Source download, toolchain setup, GN, build and packaging |
| `.github/scripts/chromium_i686_resume.sh` | Preserves and restores Ninja state correctly |
| `.github/scripts/runner_diagnostics.sh` | Reports storage, inode and swap usage |

## Build configuration

The pipeline generates an x86 Linux build with these core GN arguments:

```gn
target_os="linux"
target_cpu="x86"
is_debug=false
symbol_level=0
blink_symbol_level=0
enable_nacl=false
is_official_build=false
use_thin_lto=false
use_reclient=false
treat_warnings_as_errors=false
cc_wrapper="ccache"
```

Chromium currently contains a source guard that blocks this x86 Linux target. The pipeline applies a small local patch before generating the GN graph.

Each compiler stage runs:

```bash
autoninja -C out/Release_x86 -j3 chrome
```

## The checkpoint problem that had to be solved

The first staged implementation restored `out/Release_x86`, but Ninja repeatedly returned to roughly:

```text
[1/57146]
```

The archive existed, but the restored outputs looked stale. Three fixes made the checkpoints genuinely resumable.

### 1. Preserve nanosecond timestamps

Ninja records high-resolution timestamps in `.ninja_log` and `.ninja_deps`. A normal tar archive can round modification times, making restored outputs appear older than their inputs.

Checkpoints are therefore created with POSIX/PAX metadata:

```bash
tar \
  --format=posix \
  --pax-option='delete=atime,delete=ctime' \
  -C "${CHROMIUM_SRC}/out" \
  -I 'zstd -T0 -1' \
  -cf "${CHECKPOINT_ARCHIVE}" \
  Release_x86
```

This preserves subsecond modification times across compression and extraction.

### 2. Stabilise recreated input timestamps

Every fresh runner recreates the source tree, Clang toolchain and sysroot. Fresh mtimes can make those inputs appear newer than valid restored outputs.

Before restoring the checkpoint, the pipeline normalises all non-output:

- files;
- symbolic links;
- directories.

Directories are included because Ninja can track them as regeneration inputs.

### 3. Reuse the restored GN graph

The workflow no longer runs `gn gen` unconditionally after every restore. It reuses:

```text
out/Release_x86/build.ninja
out/Release_x86/args.gn
```

when they are present in the checkpoint.

A bounded dry run is also used to explain remaining dirty targets:

```bash
ninja -C out/Release_x86 -n -d explain chrome
```

After these fixes, the final recovery resumed with only 21 operations instead of restarting tens of thousands.

## The final i386 runtime issue

Near completion, Chromium executed a generated 32-bit build tool:

```text
v8_context_snapshot_generator
```

The runner initially lacked its 32-bit shared libraries. The observed failures were:

```text
libglib-2.0.so.0: cannot open shared object file
libexpat.so.1 => not found
```

The pipeline now enables i386 multiarch and installs:

```text
libc6:i386
libgcc-s1:i386
libstdc++6:i386
libglib2.0-0:i386
libexpat1:i386
```

Before compilation, it validates the restored generator with `file` and `ldd`. The stage fails early when the binary is non-executable or any dependency is unresolved.

## Verified final stage

With checkpoint restoration and i386 runtime support fixed, the last stage completed as expected:

```text
[1/21] ACTION //tools/v8_context_snapshot:generate_v8_context_snapshot
...
[20/21] CXX obj/chrome/chrome_initial/chrome_main_delegate.o
[21/21] LINK ./chrome
Build finished successfully
```

This verified that:

- the Ninja checkpoint was restored correctly;
- completed work was retained across runners;
- the generated 32-bit V8 tool executed successfully;
- Chromium linked successfully;
- the final runtime package and checksum were produced;
- the build artifact uploaded successfully.

## Reproduce this in another repository

### 1. Copy the pipeline files

Copy these paths:

```text
.github/workflows/chromium-i686.yml
.github/actions/chromium-i686-stage/action.yml
.github/scripts/chromium_i686_common.sh
.github/scripts/chromium_i686_resume.sh
.github/scripts/runner_diagnostics.sh
```

### 2. Enable Actions permissions

Open:

```text
Settings → Actions → General → Workflow permissions
```

Select **Read and write permissions**.

The workflow requests:

```yaml
permissions:
  contents: write
  actions: write
```

`actions: write` is required because one stage dispatches the next stage.

### 3. Start a fresh build

Open:

```text
Actions → Chromium i686 Build (Cloud Experiment) → Run workflow
```

Use:

```text
stage: 1
version: leave empty
preferred_checkpoint_run_id: leave empty
fallback_checkpoint_run_id: leave empty
retry_count: 0
```

Stage 1 resolves and pins the Chromium version. Later stages are dispatched automatically with the same version.

### 4. Resume a failed stage manually

Provide:

```text
stage: failed stage number
version: exact pinned Chromium version
preferred_checkpoint_run_id: run containing the same-stage checkpoint
retry_count: 1 or 2
```

For a new stage starting from the previous completed stage, use that previous run as `fallback_checkpoint_run_id`.

### 5. Confirm that resume is real

A healthy restore should show:

```text
Reusing build.ninja and args.gn from the restored checkpoint.
```

The remaining target count should fall, for example:

```text
[1/24]
```

It should not return to the original full count such as `[1/57146]`.

## Output

A successful run creates:

```text
chromium-<version>-linux-i686.tar.xz
chromium-<version>-linux-i686.tar.xz.sha256
chromium-<version>-linux-i686-manifest.txt
```

These files are uploaded as a GitHub Actions artifact. GitHub Release publication is attempted separately and is non-fatal so a release-permission issue cannot invalidate a successful build.

## Replication checklist

- [ ] Pin one Chromium version across all stages.
- [ ] Keep GN arguments identical across runners.
- [ ] Apply the same x86 Linux source patch each time.
- [ ] Install Chromium's i386 sysroot.
- [ ] Install the required host i386 runtime libraries.
- [ ] Preserve nanosecond mtimes in checkpoint archives.
- [ ] Normalise recreated source, symlink and directory mtimes.
- [ ] Reuse restored `build.ninja` and `args.gn`.
- [ ] Validate `v8_context_snapshot_generator` with `ldd`.
- [ ] Keep checkpoint artifacts available until the next stage completes.
- [ ] Monitor disk, inode and swap capacity before linking.

## Limitations

This is an experimental, unofficial Chromium build:

- it is not Google Chrome;
- it is not an official Chromium release binary;
- the x86 Linux source guard is patched locally;
- GitHub runner images and package repositories can change;
- checkpoint artifacts are temporary;
- the output should be tested before production use.

For long-term reproducibility, pin the Chromium version, runner image, GN arguments, source patch, toolchain revision and i386 package set.

## Final outcome

The project achieved its goal:

> Chromium was compiled for Linux i686 entirely on GitHub-hosted Actions runners, despite job time limits and fresh-runner state loss.

The decisive improvements were exact Ninja timestamp preservation, stable recreated-input mtimes, restored GN graph reuse and complete i386 runtime validation. Together they changed the pipeline from repeatedly restarting tens of thousands of operations to restoring the final 21 operations and successfully linking `chrome`.