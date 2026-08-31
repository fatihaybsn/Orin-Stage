# Offline build-input contract

This contract records the Step 6B.3 split between Ubuntu archive tools and
exact PyPI source inputs. It does not define Debian packaging, a wheelhouse, or
a Cargo vendor directory. Package observations are for Ubuntu arm64 and were
read from the official Release, Updates, and Security `Packages` indices on
2026-08-31.

## Ubuntu Build-Depends

The selected common system input set is:

```text
python3
python3-dev
python3-venv
python3-wheel
build-essential
libyaml-dev
cython3
rustc-1.85
cargo-1.85
```

`gcc` is not repeated because `build-essential` provides the compiler and
normal C/C++ build utilities. `pkg-config` is not needed by the inspected
PyYAML, rpds-py, or maturin metadata and is therefore excluded. `python3-venv`
provides the distro pip bootstrap wheel used only as a build frontend;
`python3-wheel` is required by Jammy's distro setuptools to implement
`bdist_wheel`.

| Build input | Jammy Release | Jammy current archive | Noble Release | Noble current archive | Decision |
|---|---|---|---|---|---|
| `python3` | 3.10.4-0ubuntu2 | 3.10.6-1~22.04.1 | 3.12.3-0ubuntu1 | 3.12.3-0ubuntu2.1 | apt |
| `python3-dev` | 3.10.4-0ubuntu2 | 3.10.6-1~22.04.1 | 3.12.3-0ubuntu1 | 3.12.3-0ubuntu2.1 | apt |
| `python3-venv` | 3.10.4-0ubuntu2 | 3.10.6-1~22.04.1 | 3.12.3-0ubuntu1 | 3.12.3-0ubuntu2.1 | apt |
| `python3-wheel` | 0.37.1-2 | 0.37.1-2ubuntu0.22.04.1 | 0.42.0-2 | 0.42.0-2 | apt |
| `build-essential` | 12.9ubuntu3 | 12.9ubuntu3 | 12.10ubuntu1 | 12.10ubuntu1 | apt |
| `gcc` (provided) | 4:11.2.0-1ubuntu1 | 4:11.2.0-1ubuntu1 | 4:13.2.0-7ubuntu1 | 4:13.2.0-7ubuntu1 | via `build-essential` |
| `libyaml-dev` | 0.2.2-1build2 | 0.2.2-1build2 | 0.2.5-1build1 | 0.2.5-1build1 | apt |
| `cython3` | 0.29.28-1ubuntu3 | 0.29.28-1ubuntu3 | 3.0.8-1ubuntu3 | 3.0.8-1ubuntu3 | apt, proven below |
| `rustc` generic | 1.58.1+dfsg1~ubuntu1-0ubuntu2 | 1.75.0+dfsg0ubuntu1~bpo0-0ubuntu0.22.04.1 | 1.75.0+dfsg0ubuntu1-0ubuntu7 | 1.75.0+dfsg0ubuntu1-0ubuntu7.4 | insufficient for rpds lock v4 |
| `cargo` generic | 0.58.0-0ubuntu1 | 1.75.0+dfsg0ubuntu1~bpo0-0ubuntu0.22.04.1 | 1.75.0+dfsg0ubuntu1-0ubuntu7 | 1.75.0+dfsg0ubuntu1-0ubuntu7.4 | insufficient for rpds lock v4 |
| `rustc-1.78` | unavailable | 1.78.0+dfsg1ubuntu1~bpo0-0ubuntu0.22.04.1 | unavailable | 1.78.0+dfsg1ubuntu1-0ubuntu0.24.04.2 | insufficient after 6B.4 source proof |
| `cargo-1.78` | unavailable | 1.78.0+dfsg1ubuntu1~bpo0-0ubuntu0.22.04.1 | unavailable | 1.78.0+dfsg1ubuntu1-0ubuntu0.24.04.2 | insufficient after 6B.4 source proof |
| `rustc-1.85` | unavailable | 1.85.1+dfsg0ubuntu2~bpo0-0ubuntu1.22.04.1 | unavailable | 1.85.1+dfsg0ubuntu2~bpo0-0ubuntu0.24.04.2 | apt, selected and proven |
| `cargo-1.85` | unavailable | 1.85.1+dfsg0ubuntu2~bpo0-0ubuntu1.22.04.1 | unavailable | 1.85.1+dfsg0ubuntu2~bpo0-0ubuntu0.24.04.2 | apt, selected and proven |
| `pkg-config` | 0.29.2-1ubuntu3 | 0.29.2-1ubuntu3 | 1.8.1-2build1 | 1.8.1-2build1 | exclude |

The versioned Rust packages require the Updates/Security pockets. A
Release-only build environment is not compatible with this contract.

## PyYAML 6.0.3 distro-Cython proof

Both experiments used the SHA256-verified
`pyyaml-6.0.3.tar.gz` (`d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`).
The disposable containers were prepared from the Ubuntu archive and the build
and import phases ran with the container network set to `none`. PEP 517 build
isolation was disabled, dependencies were not resolved, and
`PYYAML_FORCE_CYTHON=1` prevented a silent pure-Python fallback.

| Evidence | Jammy | Noble |
|---|---|---|
| Python | 3.10.12 | 3.12.3 |
| setuptools | 59.6.0 (distro venv wheel) | 68.1.2 (distro venv wheel) |
| Cython | 0.29.28 | 3.0.8 |
| wheel | 0.37.1 | 0.42.0 |
| produced wheel | `PyYAML-6.0.3-cp310-cp310-linux_x86_64.whl` | `PyYAML-6.0.3-cp312-cp312-linux_x86_64.whl` |
| wheel size | 523,926 bytes | 592,995 bytes |
| experiment SHA256 | `c594754cb46fd7c285281ddaa62079ed33d6d6511ecf51668f70759ab598ee33` | `a15cb1f5998cab591c36da9a49cf74118e344789b74e5ff0833ef5e73d8aa154` |
| imports | `yaml`, `yaml._yaml`, version 6.0.3 | `yaml`, `yaml._yaml`, version 6.0.3 |
| native proof | `_yaml.cpython-310-x86_64-linux-gnu.so`, `__with_libyaml__ == True` | `_yaml.cpython-312-x86_64-linux-gnu.so`, `__with_libyaml__ == True` |
| dynamic link | `libyaml-0.so.2` | `libyaml-0.so.2` |
| result | PASS | PASS |

The first controlled Jammy attempt without `python3-wheel` failed at
`invalid command 'bdist_wheel'`; adding that single archive package made the
same offline build pass. Cython is therefore an Ubuntu Build-Depends input and
is not in `build-tools.lock`.

## Locked runtime sdist metadata re-verification

The root `pyproject.toml` was extracted again from each SHA256-verified 6B.2
sdist; these values do not come from repository notes.

| Locked source | build-backend | requires | backend-path |
|---|---|---|---|
| attrs 26.1.0 | `hatchling.build` | `hatchling`; `hatch-vcs`; `hatch-fancy-pypi-readme>=23.2.0` | absent |
| jsonschema 4.26.0 | `hatchling.build` | `hatchling`; `hatch-vcs`; `hatch-fancy-pypi-readme` | absent |
| jsonschema-specifications 2025.9.1 | `hatchling.build` | `hatchling>=1.27.0`; `hatch-vcs` | absent |
| PyYAML 6.0.3 | `_pyyaml_pep517` | `setuptools`; `Cython; python_version < '3.13'`; `Cython>=3.0; python_version >= '3.13'` | `packaging` |
| referencing 0.37.0 | `hatchling.build` | `hatchling`; `hatch-vcs` | absent |
| rpds-py 0.30.0 | `maturin` | `maturin>=1.9,<2.0` | absent |
| typing-extensions 4.16.0 | `flit_core.buildapi` | `flit_core >=3.11,<4` | absent |
| Orin Stage | `setuptools.build_meta` | `setuptools>=77` | absent |

## Ubuntu Python backend audit

Versions below are upstream versions from the Ubuntu Release package unless an
update is material to the decision.

| Backend/tool | Jammy | Noble | Required by locked sources | Decision |
|---|---:|---:|---|---|
| setuptools (`python3-setuptools`) | 59.6.0 | 68.1.2 | Orin Stage `>=77` | VENDOR EXACT SOURCE |
| hatchling (`python3-hatchling`) | 0.15.0 | 1.21.0 | jsonschema-specifications `>=1.27.0` | VENDOR EXACT SOURCE |
| hatch-vcs | unavailable | 0.4.0 | attrs/jsonschema/referencing | VENDOR EXACT SOURCE |
| hatch-fancy-pypi-readme | unavailable | 24.1.0 | attrs `>=23.2.0` | VENDOR EXACT SOURCE |
| flit_core (`flit`) | 3.6.0 | 3.9.0 | typing-extensions `>=3.11,<4` | VENDOR EXACT SOURCE |
| maturin (`python3-maturin`) | unavailable | 1.3.2 | rpds-py `>=1.9,<2.0` | VENDOR EXACT SOURCE |
| Cython (`cython3`) | 0.29.28 | 3.0.8 | PyYAML, no minimum below Python 3.13 | USE UBUNTU |

The common vendored set avoids separate Jammy and Noble Python backend stacks.
No Ubuntu compiler, Rust compiler, Cargo, CPython, Cython, wheel, or pip source
is copied into the source bundle.

## Python build-tool bootstrap graph

Edges below combine each sdist's root `[build-system]` table and active
`Requires-Dist` metadata for Python 3.10/3.12. Extras for tests, docs,
publishing, benchmarks, and development are excluded.

```text
flit-core 3.11.0
└── self-contained backend-path [.] (no external build requirement)

hatchling 1.27.0
├── self-contained hatchling.ouroboros backend-path [src]
├── wheel metadata imports its runtime closure during bootstrap
└── runtime: packaging, pathspec, pluggy, tomli (3.10), trove-classifiers
    └── trove-classifiers build: setuptools + calver
        └── calver build: setuptools

hatch-fancy-pypi-readme 23.2.0
└── build/runtime: hatchling (+ tomli on Python 3.10)

hatch-vcs 0.5.0
├── build/runtime: hatchling
└── runtime: setuptools-scm
    ├── packaging + setuptools (+ tomli on Python 3.10)
    └── self-contained _own_version_helper backend-path [., src]

packaging 24.2, pathspec 0.12.1, tomli 2.0.2
└── build: flit-core

pluggy 1.5.0
└── build: setuptools + setuptools-scm

setuptools 77.0.3
├── self-contained backend-path [.] (no declared external build requirement)
└── wheel metadata requires packaging >=24.2 for SPDX license processing

setuptools-rust 1.11.1
├── build: setuptools + setuptools-scm
└── runtime: setuptools + semantic-version
    └── semantic-version has no pyproject.toml; legacy build uses setuptools

maturin 1.9.0
├── backend-path [maturin], backend bootstrap
├── build: setuptools + setuptools-rust (+ tomli on Python 3.10)
└── Rust: Cargo.lock external sources (deferred to Step 6B.4)
```

There is no inter-package dependency cycle. The apparent self references for
flit-core, hatchling, and setuptools are finite in-tree `backend-path`
bootstraps, not source dependencies. Maturin does not require an already-built
maturin wheel, but its Rust portion cannot be completed until its locked crate
sources are supplied in Step 6B.4.

Step 6C's real source builds refined the ordering without adding a source:
`flit-core` builds locked `packaging 24.2` before setuptools, and Hatchling is
built after `pathspec`, `pluggy`, and `trove-classifiers`. These imports are
not fully expressed by the corresponding root `[build-system].requires`
tables, but every input was already present in the exact 6B.3 closure.

`tomli==2.0.2` is intentional: it satisfies hatchling/maturin lower bounds and
the setuptools-scm sdist's Python 3.10 bootstrap cap `tomli<=2.0.2`.
Markers excluding supported interpreters are omitted from the closure, notably
`typing-extensions` for Python `<3.10` and all extras.

## Rust boundary

Crate identity is counted as the exact `(name, version, source)` tuple in the
verified sdist `Cargo.lock`; local/workspace packages are excluded.

| Source | Edition | Declared rust-version | Lock format | External exact crate identities |
|---|---:|---:|---:|---:|
| rpds-py 0.30.0 | 2021 | not declared | 4 | 26 |
| maturin 1.9.0 | 2021 | 1.74 | 3 | 371 |
| union | — | — | — | 395 |

The two exact overlaps are `heck 0.5.0` and `shlex 1.3.0`, hence
`26 + 371 - 2 = 395`. No crate was downloaded in this step.

Cargo lock format 4 requires Cargo 1.78 or newer, but that parser floor is not
the complete source-closure floor. Step 6B.4's exact crate materialization
showed that rpds-py's locked direct dependency `rpds 1.2.0` declares Rust
edition 2024 and `rust-version = "1.85.0"`. Cargo 1.78 consequently rejects
the locked crate manifest before compilation. The selected common archive
toolchain is therefore `rustc-1.85`/`cargo-1.85`; its 1.85.1 binaries passed
no-network `cargo metadata --locked --offline` for both exact sdists against
the shared 395-crate vendor tree without changing either lock file. Generic
Cargo 1.75, Cargo 1.78, and a Release-only archive remain blockers.

## Scope guard

The production dependency contract remains exactly `PyYAML>=6,<7` and
`jsonschema>=4.23,<5`, and the project build requirement remains
`setuptools>=77`. Runtime lock/source records are separate from the build tool
lock/source records. This step did not create Debian metadata, fetch Cargo
crates, build the final runtime wheels, or change production code.
