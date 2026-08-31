# Exact offline Cargo source contract

This document records Step 6B.4. It covers only the Rust crates locked by the
verified `maturin 1.9.0` and `rpds-py 0.30.0` sdists. It does not authorize a
Rust compiler bundle, `rustup`, a Cargo registry mirror, a final wheel build,
or a change to either upstream lock file.

## Authoritative lock inputs

The two `Cargo.lock` snapshots in `cargo-locks/` were extracted directly from
the SHA256-verified PyPI sdists already locked by Step 6B.3.

| Consumer | Verified sdist SHA256 | Cargo.lock SHA256 | Registry crates | Local packages |
|---|---|---|---:|---:|
| maturin 1.9.0 | `ccb9cb87f8df88d1bab8f49efe3fc77f0abb0639ea4b4ebf4f35549200d16b9e` | `53b0b679cf2dd24243f048df3b277ac606c8cf95eb5b809444965e6f1ae5a79e` | 371 | 1 |
| rpds-py 0.30.0 | `dd8ff7cf90014af0c0f787eea34794ebf6415242ee1d6fa91eaba725cc441e84` | `8fb59015879aea2a3ca6e9c9e542ea949bf057b67b57d9e0fbbba4179a9acb61` | 26 | 1 |

The only shared identities are `heck 0.5.0` and `shlex 1.3.0`. Identity is the
exact `(name, version, source)` tuple, so `371 + 26 - 2 = 395` unique registry
crates. There are no Git dependencies. Local/workspace packages are recorded
in the counts but are not downloaded.

`cargo-sources.lock.json` is the authoritative merged source manifest. Each of
its 395 sorted entries contains the exact crate name, version, crates.io source
identity, deterministic `.crate` filename, official static.crates.io URL,
Cargo.lock checksum, and one or both consumers. Duplicate identities,
unsupported registries, Git sources, missing checksums, unofficial URLs, and
non-canonical filenames fail closed.

## Fetch and materialization results

`tools/release/fetch_cargo_sources.py` is a stdlib-only release-preparation
tool. Its fetch path verifies every SHA256 before atomic rename, re-verifies
cached archives, removes partial files after failure, and never performs
dependency resolution. A completed second run reported 395 cache reuses, zero
downloads, and 395 checksum matches.

| Metric | Result |
|---|---:|
| downloaded `.crate` files | 395 |
| compressed source bytes | 52,168,333 |
| standard vendor directories | 395 |
| files in vendor tree, including `.cargo-checksum.json` | 21,269 |
| uncompressed content bytes | 411,187,002 |
| vendor filesystem size (`du -sb`) | 424,744,762 |
| vendor tree content digest | `b720c626294b0585ff8daffce0998dc75fe15e1cfb6ab5eb36b18872de63b820` |
| Cargo config digest | `54310eb1557c7bff739e9e3bfcf0a46c241e34ec513a613a6fbcf9376d029ac8` |

The content digest is SHA256 over each sorted relative file path and that
file's SHA256. A second materialization into an independent empty directory
produced the same digest. The generated `.cargo/config.toml` replaces
`crates-io` with the adjacent `cargo-vendor` directory source. The generated
tree contains exactly the manifest set and no duplicate shared crates.
The small materializer performs a deterministic two-input merge into Cargo's
standard directory-source format; it does not define a private package format.
This path is necessary because the selected 1.78 candidate cannot parse one of
the exact locked manifests, as documented below.

Largest compressed archives are:

| Crate archive | Bytes |
|---|---:|
| `winapi-x86_64-pc-windows-gnu-0.4.0.crate` | 2,947,998 |
| `winapi-i686-pc-windows-gnu-0.4.0.crate` | 2,918,815 |
| `windows-sys-0.48.0.crate` | 2,628,884 |
| `windows-sys-0.52.0.crate` | 2,576,877 |
| `linux-raw-sys-0.4.14.crate` | 1,826,665 |
| `ring-0.17.13.crate` | 1,501,917 |
| `encoding_rs-0.8.34.crate` | 1,378,166 |
| `winapi-0.3.9.crate` | 1,200,382 |
| `pyo3-0.27.2.crate` | 1,171,342 |
| `windows_i686_msvc-0.52.5.crate` | 895,404 |

`cargo-downloads/` is a generated release-preparation cache and is not part of
the source bundle. The release assembly will copy the single generated
`cargo-vendor/` tree and its `.cargo/config.toml`; it does not also copy the
compressed cache. Both generated locations are gitignored. The exact manifest,
lock snapshots, inventory, digest record, tests, and fetch/materialize tool are
tracked.

The later source bundle's input classes are therefore explicit:

```text
CACHE ONLY
└── raw downloaded .crate archives

SOURCE BUNDLE
├── runtime sdists from sources.lock.json
├── Python build-tool sdists from build-sources.lock.json
├── deterministic cargo-vendor tree and relative Cargo config
└── runtime, build-tool, and Cargo lock/manifest metadata
```

## License inventory

`CARGO_THIRD_PARTY.json` is a machine-readable 1:1 inventory of all 395 exact
manifest entries. It records name, version, source, declared license or
license-file, repository, and consumer attribution. Every locked crate has
license metadata; `cargo-vendor.lock.json` records an empty
`missing_license_metadata` list. Test, development, and unrelated ecosystem
packages are not added beyond what the authoritative Cargo locks contain.

## Offline Cargo proof and toolchain floor

The first proof used Jammy's archive-provided Cargo/Rust 1.78.0 with an empty
`CARGO_HOME`, the network namespace disabled, and the generated directory
source. Cargo failed before compilation while parsing the locked
`rpds 1.2.0` manifest:

```text
feature `edition2024` is required
Cargo 1.78.0 does not stabilize edition2024
```

The exact locked direct dependency declares `edition = "2024"` and
`rust-version = "1.85.0"`. This is a source-level incompatibility that was not
visible from rpds-py's root `Cargo.toml`; it makes the earlier 1.78 parser-floor
selection insufficient. No patch, lock regeneration, source change, rustup,
or bundled compiler was used.

The corrected common Ubuntu archive contract is:

| Package | Jammy current archive | Noble current archive |
|---|---|---|
| `cargo-1.85` | `1.85.1+dfsg0ubuntu2~bpo0-0ubuntu1.22.04.1` | `1.85.1+dfsg0ubuntu2~bpo0-0ubuntu0.24.04.2` |
| `rustc-1.85` | `1.85.1+dfsg0ubuntu2~bpo0-0ubuntu1.22.04.1` | `1.85.1+dfsg0ubuntu2~bpo0-0ubuntu0.24.04.2` |

In a disposable Jammy environment using those archive binaries, both exact
sdists passed full `cargo metadata --locked --offline` against the same
395-crate vendor tree with the container network set to `none`. The maturin
lock remained `53b0...a79e` and the rpds-py lock remained `8fb5...cb61` before
and after. This is metadata/source-resolution proof only; the requested actual
no-network compilation remains Step 6C.

There is no remaining source-closure or Cargo metadata blocker for proceeding
to Step 6C under the selected Updates/Security-pocket 1.85 contract. A
Release-only archive, generic Cargo 1.75, or Cargo 1.78 is a blocker.
