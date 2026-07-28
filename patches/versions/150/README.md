# Chromium 150 compatibility status

`150.0.7871.186` is the verified baseline for this unofficial Linux i686 port.

Verified properties:

- Chromium source accepted `target_os="linux"` and `target_cpu="x86"` after the common GN guard patch.
- The i386 sysroot installed successfully.
- The 32-bit V8 context snapshot generator ran after installing the required host i386 libraries.
- The final `chrome` executable linked successfully on GitHub Actions.

No additional major-specific `.patch` files are currently required.
