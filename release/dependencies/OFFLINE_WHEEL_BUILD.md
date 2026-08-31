# Jammy and Noble full offline wheel proof

This document records Step 6C. Both proof runs began only after Ubuntu archive
packages and all verified source inputs had been prepared. Every Python and
Cargo build, clean install, import check, CLI smoke test, and repository test
run then used a disposable Podman container with `--network none`.

## Environments and system inputs

| Input | Jammy 22.04 amd64 | Noble 24.04 amd64 |
|---|---|---|
| CPython binary | 3.10.12 | 3.12.3 |
| `python3` package | 3.10.6-1~22.04.1 | 3.12.3-0ubuntu2.1 |
| `python3-wheel` | 0.37.1-2ubuntu0.22.04.1 | 0.42.0-2 |
| Cython | 0.29.28-1ubuntu3 | 3.0.8-1ubuntu3 |
| libyaml development files | 0.2.2-1build2 | 0.2.5-1build1 |
| Cargo | 1.85.1 (`cargo-1.85`) | 1.85.1 (`cargo-1.85`) |
| Rust | 1.85.1 (`rustc-1.85`) | 1.85.1 (`rustc-1.85`) |
| C/C++ toolchain | build-essential 12.9ubuntu3 | build-essential 12.10ubuntu1 |

The build input set stayed exactly as documented in `BUILD_INPUTS.md`.
`python3-pytest` was installed before isolation only as test-runner support; it
was not used as a wheel build input and is absent from both wheel manifests.
No `rustup`, bundled compiler, PyPI wheel, network resolver, or source patch was
used.

Ubuntu's versioned Rust packages expose `cargo-1.85` and `rustc-1.85` rather
than unversioned names. The orchestrator creates temporary `cargo` and `rustc`
symlinks pointing to those exact binaries for tools such as setuptools-rust.
It does not modify `/usr/bin` or user Cargo configuration.

## Offline orchestration

`tools/release/build_offline_wheelhouse.py` verifies both Python source
manifests and every sdist hash before building. It uses an explicit environment
and invokes subprocesses with `shell=False`. Every package build uses:

```text
pip wheel --no-index --no-build-isolation --no-deps --no-cache-dir
```

Cargo uses a temporary config that replaces crates.io with the exact generated
395-crate directory source, plus `CARGO_NET_OFFLINE=true`. Maturin's bootstrap
already supplies Cargo `--locked`; `MATURIN_SETUP_ARGS` adds `--offline`.
rpds-py's exact offline-built maturin backend receives `--locked --offline`.

The actual source builds refined two hidden bootstrap edges without adding an
input or creating a cycle:

```text
flit-core
└── packaging 24.2
    └── setuptools 77.0.3 metadata/SPDX processing

pathspec + pluggy + trove-classifiers
└── hatchling 1.27.0 metadata imports
```

The remainder follows the finite 6B.3 graph. Fifteen exact build-tool wheels
are produced and installed only in the temporary build environment. The final
runtime wheelhouse contains the seven exact runtime-lock packages and Orin
Stage, never build tools.

## First-build wheel results

Each series produced 15 build wheels and 8 runtime wheels. The fourteen pure
build-tool filenames are common; maturin has the interpreter-specific filename:

```text
calver-2025.3.31-py3-none-any.whl
flit_core-3.11.0-py3-none-any.whl
hatch_fancy_pypi_readme-23.2.0-py3-none-any.whl
hatch_vcs-0.5.0-py3-none-any.whl
hatchling-1.27.0-py3-none-any.whl
packaging-24.2-py3-none-any.whl
pathspec-0.12.1-py3-none-any.whl
pluggy-1.5.0-py3-none-any.whl
semantic_version-2.10.0-py2.py3-none-any.whl
setuptools-77.0.3-py3-none-any.whl
setuptools_rust-1.11.1-py3-none-any.whl
setuptools_scm-8.2.0-py3-none-any.whl
tomli-2.0.2-py3-none-any.whl
trove_classifiers-2025.5.9.12-py3-none-any.whl
```

| Series | Exact maturin source result |
|---|---|
| Jammy | `maturin-1.9.0-cp310-cp310-linux_x86_64.whl` |
| Noble | `maturin-1.9.0-cp312-cp312-linux_x86_64.whl` |

Runtime wheels and first-build SHA256 values are:

| Package | Jammy filename / SHA256 | Noble filename / SHA256 |
|---|---|---|
| attrs | `attrs-26.1.0-py3-none-any.whl` / `ed73c232...2b01eb` | same / `ed73c232...2b01eb` |
| jsonschema | `jsonschema-4.26.0-py3-none-any.whl` / `11484470...916f60` | same / `11484470...916f60` |
| jsonschema-specifications | `jsonschema_specifications-2025.9.1-py3-none-any.whl` / `98802fee...0cc6fe` | same / `98802fee...0cc6fe` |
| PyYAML | `PyYAML-6.0.3-cp310-cp310-linux_x86_64.whl` / `5e143c76...8aef0` | `PyYAML-6.0.3-cp312-cp312-linux_x86_64.whl` / `ca86b737...05d9f` |
| referencing | `referencing-0.37.0-py3-none-any.whl` / `381329a9...692231` | same / `381329a9...692231` |
| rpds-py | `rpds_py-0.30.0-cp310-cp310-linux_x86_64.whl` / `43c3e2cd...4d2f6c` | `rpds_py-0.30.0-cp312-cp312-linux_x86_64.whl` / `0f5168ff...c8c9e3b` |
| typing-extensions | `typing_extensions-4.16.0-py3-none-any.whl` / `44bee9f6...53fc1` | same / `44bee9f6...53fc1` |
| Orin Stage | `orin_stage-0.1.0-py3-none-any.whl` / `73800f97...0f4bc` | same filename / `27d089e9...eddea` |

The generated, gitignored `WHEELS.json` files contain every complete SHA256,
size, role, Python tag, ABI tag, and platform tag. Both manifests re-verified
all 23 on-disk wheels and their exact lock consistency.

Step 6D added the required `python -m orin_stage.cli` entrypoint.  The two 6C
wheelhouses above were consequently regenerated from the same locked inputs,
offline, before the installed-runtime proof; the table records those current
first-build hashes.  A fresh second build of each regenerated wheelhouse again
matched all 23 identities/tags and runtime member names.

The Orin Stage wheel contains the package, one catalog schema, seven target
YAML files, two hardware YAML files, LICENSE, and the `ostg` entry point. Its
version remains 0.1.0.

## Native and clean-install proof

PyYAML imported `yaml._yaml`, reported `yaml.__with_libyaml__ is True`, and its
native module linked `libyaml-0.so.2` in both series. rpds-py imported from
freshly built interpreter-specific shared objects:

```text
Jammy: rpds/rpds.cpython-310-x86_64-linux-gnu.so
Noble: rpds/rpds.cpython-312-x86_64-linux-gnu.so
```

Fresh ordinary venvs installed only the eight runtime wheels with
`--no-index --no-deps` and network disabled. Exact installed versions were:

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

Hatchling, maturin, Cython, setuptools-rust, setuptools-scm, and flit-core
were absent. Jammy's venv bootstrap supplied pip 22.0.2 and setuptools 59.6.0;
Noble supplied pip 24.0 and no setuptools. These bootstrap packages are not
Orin Stage runtime dependencies.

Both clean environments passed `ostg --version`, `ostg --help`,
`ostg target list`, and imports of yaml, yaml._yaml, jsonschema, rpds, and
orin_stage. Target-list output contained seven lines.

## Test and rebuild results

Jammy's rootless, no-network test environment passed all 536 tests. The distro
pytest 6.2.5 emits one warning because it predates pytest's `pythonpath`
configuration option; this is not a test failure.

Noble passed 532 tests. The remaining four are the already-known rootless
Podman/GNU tar permission limitation in `test_materialization_extract.py`:
`--same-owner`/`--same-permissions` cannot restore the requested symlink and
directory modes in that user namespace. No dependency, import, wheel, or build
test failed. Per scope, production Podman/materialization code was not changed.

Independent second no-network builds produced the same 23 package identities,
versions, filenames, Python/ABI/platform tags, and the same member-name sets in
all eight runtime wheels for each series. Eleven wheel SHA256 values changed in
each series due to upstream timestamp/build metadata: eight build wheels plus
Orin Stage, PyYAML, and rpds-py. The other twelve wheels were byte-identical.
No reproducibility patch or `SOURCE_DATE_EPOCH` framework was added.

All authoritative Python and Cargo lock hashes were identical before and after
both builds. There is no Step 6D blocker in the source, build, install, import,
or CLI chain. Noble's four tar tests remain a documented container-only
limitation for later real-host acceptance.
