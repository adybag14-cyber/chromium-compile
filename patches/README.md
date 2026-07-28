# Linux i686 downstream patch set

Chromium no longer officially builds or tests desktop Linux i686. This directory contains the downstream compatibility layer used before every expensive build.

## Layout

- `common/enable_linux_i686.py` applies a checked semantic change to Chromium's top-level GN target guard.
- `versions/<major>/*.patch` contains ordinary unified patches needed only for a specific Chromium major release.
- `versions/<major>/README.md` records the compatibility status and known upstream breakages for that major.

The semantic patch is deliberately strict. If Chromium changes the relevant guard, preflight fails immediately instead of silently editing an unrelated expression. Version patches are dry-run checked before they are applied.

A new stable release should not enter the multi-stage compiler pipeline until all patches apply and the GN/Ninja preflight succeeds.
