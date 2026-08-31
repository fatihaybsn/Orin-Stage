# Installed private runtime prototype

This document records Step 6D.  It consumes only a verified Step 6C runtime
wheelhouse and builds a disposable staging root; it does not create a Debian
directory, a package, or write to the host `/usr`.

## Layout and creation rule

For each Ubuntu series, `tools/release/stage_private_runtime.py` creates the
venv directly at its final, absolute staging location:

```text
<staging-root>/usr/bin/ostg
<staging-root>/usr/lib/orin-stage/venv/bin/python
<staging-root>/usr/lib/orin-stage/venv/pyvenv.cfg
<staging-root>/usr/lib/orin-stage/venv/lib/python3.x/site-packages/
```

It deliberately does not create a venv in a temporary directory and move it.
The first prototype did expose why: the generated wrapper retained the
temporary absolute interpreter path.  The final helper rejects an existing
output root and creates the venv only once the final staging path is known.
There is no developer-path fallback in the produced runtime.

The logical production launcher contract is exactly:

```sh
#!/bin/sh
exec "/usr/lib/orin-stage/venv/bin/python" -I -m orin_stage.cli "$@"
```

For a disposable staging proof, its interpreter string is injected with the
staging-root equivalent of that fixed path.  It is not selected from `PATH`, an
environment variable, the checkout, or the caller's Python installation.

## Exact offline input and installed set

The helper first invokes the 6C `WHEELS.json` verifier, including every wheel
SHA256 and the exact runtime lock check.  It then invokes:

```text
<venv>/bin/python -m pip install --no-index --no-deps --no-cache-dir --force-reinstall <eight runtime wheels>
```

No build wheel is considered for installation.  Both series installed exactly:

```text
attrs 26.1.0
jsonschema 4.26.0
jsonschema-specifications 2025.9.1
PyYAML 6.0.3
referencing 0.37.0
rpds-py 0.30.0
typing-extensions 4.16.0
orin-stage 0.1.0
```

`pip` and, depending on the distro venv bootstrap, `setuptools` are reported
separately and are the only tolerated bootstrap distributions.  Hatchling,
maturin, Cython, setuptools-rust, Cargo, and every other build-only package are
rejected if present.

## Jammy and Noble proof

Both runs used a disposable Podman container with `--network none`, the exact
6C wheelhouse, and a working directory outside the repository checkout:

| Series | Distro interpreter used for venv | Result |
|---|---|---|
| Jammy amd64 | `/usr/bin/python3` (CPython 3.10.12) | passed |
| Noble amd64 | `/usr/bin/python3` (CPython 3.12.3) | passed |

For both generated trees, `ostg --version`, `ostg --help`, and `ostg target
list` passed through the staging launcher.  The module entrypoint is explicitly
tested because the launcher uses `-m orin_stage.cli`; it now calls `main()`
when executed as a module.

`ostg doctor` also starts and reports normally in both containers.  Its expected
container-host diagnostics were 3 PASS, 3 WARN, and 5 FAIL (no Podman, sudo,
rootless helpers, ARM64 binfmt, or QEMU); these host capability findings are not
runtime-install or launcher failures.

Each proof also verifies `python -I -m orin_stage.cli --version` while all of
the following are present: `PYTHONPATH=/tmp/evil`, a bogus `PYTHONHOME`, a fake
`orin_stage` package in the working directory, and a fake package in the user
site directory.  The fake packages are not imported.  Package-data access from
the installed tree finds `target.schema.json`, seven target YAML files, two
hardware YAML files, and installed LICENSE metadata, without a checkout
fallback.

## Privilege, permissions, and data separation

The installed private interpreter is exercised through test doubles for all
four existing narrow privilege adapters: materialization, base construction,
storage deletion, and storage measurement.  Every resulting command begins
with `sudo -- <staging-private-python> -I -m orin_stage.privileged_...`, uses
`shell=False`, contains neither `sudo -E` nor `PYTHONPATH`, and preserves the
venv symlink path rather than resolving it to the system interpreter.

Inside each disposable container, the staging root, launcher, runtime prefix,
and all runtime files are owned by UID 0 and have no group- or world-writable
mode bits.  Symlink mode bits are ignored because their parent directory is
the access-control boundary.  The rootless Podman bind mount maps container
UID 0 back to the invoking host user; that expected host-side mapping is not a
claim that a final package install is user-owned.

The prototype creates no `~/.local/share/orin-stage` directory and does not
create bases, workspaces, or caches.  Runtime installation and user state stay
separate.

## Result

There is no Step 6E blocker in the private-runtime layout, offline install,
isolated import, package-data, launcher, privilege re-exec, or permission
contracts.  A future Debian package still needs to materialize the logical
`/usr/bin/ostg` and `/usr/lib/orin-stage/venv` paths as root-owned files.
