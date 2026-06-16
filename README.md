# Chromium i686 Build (GitHub Actions)

Experimental staged pipeline that compiles upstream Chromium for `linux-i686` on GitHub-hosted `ubuntu-22.04` runners.

## How it works

- Resolves the latest stable Chromium version from Google's version history API.
- Downloads the official Chromium source tarball once per run and reuses it across stages.
- Builds in up to **12 sequential jobs**, each compiling for up to **350 minutes**.
- Saves `out/Release_x86` checkpoints as artifacts between stages.
- Publishes a GitHub release when `chrome` finishes linking.

## Run manually

1. Open **Actions → Chromium i686 Build (Cloud Experiment) → Run workflow**.
2. Leave inputs empty for a fresh full build.
3. To resume a partial run, set:
   - `resume_run_id`: the failed run's numeric ID
   - `start_stage`: not yet wired for full skip logic; use a fresh run unless you know the prior checkpoints still exist

Checkpoints are kept for **14 days**.

## Expected runtime

A full i686 Chromium build usually needs many hours across all stages. If stage 12 still does not finish, add more stages or move to a larger self-hosted runner.