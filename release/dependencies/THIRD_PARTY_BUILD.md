# Third-party Python build sources

This inventory describes the exact source-only Python build closure in
`build-tools.lock`. URLs point to official PyPI sdists; wheels and generated
GitHub archives are not accepted. `not declared` means the sdist metadata has
no `Requires-Python` field, not that a value was inferred.

| Package | Version | Requires-Python | Why present | Form | License |
|---|---:|---|---|---|---|
| calver | 2025.3.31 | >=3.9 | trove-classifiers sdist bootstrap | pure Python | Apache-2.0 |
| flit-core | 3.11.0 | >=3.6 | exact typing-extensions lower bound; builds packaging/pathspec/tomli | pure Python | BSD-3-Clause |
| hatch-fancy-pypi-readme | 23.2.0 | >=3.7 | exact attrs lower bound | pure Python | MIT |
| hatch-vcs | 0.5.0 | >=3.9 | Hatch VCS plugin; maintained line requiring setuptools-scm >=8.2 | pure Python | MIT |
| hatchling | 1.27.0 | >=3.8 | exact jsonschema-specifications lower bound | pure Python | MIT |
| maturin | 1.9.0 | >=3.7 | exact lower edge of rpds-py `>=1.9,<2` | native Rust build tool | MIT OR Apache-2.0 |
| packaging | 24.2 | >=3.8 | exact hatchling lower bound; setuptools-scm runtime | pure Python | Apache-2.0 OR BSD-2-Clause |
| pathspec | 0.12.1 | >=3.8 | hatchling runtime | pure Python | MPL-2.0 |
| pluggy | 1.5.0 | >=3.8 | hatchling runtime | pure Python | MIT |
| semantic-version | 2.10.0 | >=2.7 | setuptools-rust runtime | pure Python | BSD-2-Clause |
| setuptools | 77.0.3 | >=3.9 | patched 77.x release satisfying Orin Stage `>=77`; bootstrap root | pure Python | MIT |
| setuptools-rust | 1.11.1 | >=3.9 | patched 1.11 line satisfying maturin sdist `>=1.11.0` | pure Python Rust adapter | MIT |
| setuptools-scm | 8.2.0 | >=3.8 | exact hatch-vcs runtime lower bound; several sdist builds | pure Python | MIT |
| tomli | 2.0.2 | >=3.8 | Python 3.10 marker closure and setuptools-scm bootstrap cap | pure Python | MIT |
| trove-classifiers | 2025.5.9.12 | not declared | hatchling runtime; contains Python 3.14 classifiers used by locked runtime sdists | pure Python | Apache-2.0 |

All declared ranges admit both Python 3.10 and 3.12. The undeclared
trove-classifiers range creates no metadata exclusion for either interpreter.
The selected releases are not yanked. Minimum constraint edges are used where
they are explicit; small patch/stability selections avoid maintaining two
distro-specific backend stacks without selecting every newest upstream
release.

The complete source set is 2,487,900 bytes (2.373 MiB). Filenames, sizes,
SHA256 hashes, license files, build backends, backend paths, and build
requirements are authoritative in `build-sources.lock.json`.

## Source artifact hashes

| Filename | Bytes | SHA256 |
|---|---:|---|
| `calver-2025.3.31.tar.gz` | 7,435 | `255d1a70bba8f97dc1eee3af4240ed35980508da69257feef94c79e5c6545fc7` |
| `flit_core-3.11.0.tar.gz` | 52,038 | `6ceeee3219e9d2ea282041f3e027c441597b450b33007cb81168e887b6113a8f` |
| `hatch_fancy_pypi_readme-23.2.0.tar.gz` | 28,592 | `2762b31de2c78572f87d82986910ff46b89834d3743fa5fcdefc1d215ab21b24` |
| `hatch_vcs-0.5.0.tar.gz` | 11,424 | `0395fa126940340215090c344a2bf4e2a77bcbe7daab16f41b37b98c95809ff9` |
| `hatchling-1.27.0.tar.gz` | 54,983 | `971c296d9819abb3811112fc52c7a9751c8d381898f36533bb16f9791e941fd6` |
| `maturin-1.9.0.tar.gz` | 209,543 | `ccb9cb87f8df88d1bab8f49efe3fc77f0abb0639ea4b4ebf4f35549200d16b9e` |
| `packaging-24.2.tar.gz` | 163,950 | `c228a6dc5e932d346bc5739379109d49e8853dd8223571c7c5b55260edc0b97f` |
| `pathspec-0.12.1.tar.gz` | 51,043 | `a482d51503a1ab33b1c67a6c3813a26953dbdc71c31dacaef9a838c4e29f5712` |
| `pluggy-1.5.0.tar.gz` | 67,955 | `2cffa88e94fdc978c4c574f15f9e59b7f4201d439195c3715ca9e2486f1d0cf1` |
| `semantic_version-2.10.0.tar.gz` | 52,289 | `bdabb6d336998cbb378d4b9db3a4b56a1e3235701dc05ea2690d9a997ed5041c` |
| `setuptools-77.0.3.tar.gz` | 1,367,236 | `583b361c8da8de57403743e756609670de6fb2345920e36dc5c2d914c319c945` |
| `setuptools_rust-1.11.1.tar.gz` | 310,804 | `7dabc4392252ced314b8050d63276e05fdc5d32398fc7d3cce1f6a6ac35b76c0` |
| `setuptools_scm-8.2.0.tar.gz` | 77,572 | `a18396a1bc0219c974d1a74612b11f9dce0d5bd8b1dc55c65f6ac7fd609e8c28` |
| `tomli-2.0.2.tar.gz` | 16,096 | `d46d457a85337051c36524bc5349dd91b1877838e2979ac5ced3e710ed8a60ed` |
| `trove_classifiers-2025.5.9.12.tar.gz` | 16,940 | `7ca7c8a7a76e2cd314468c677c69d12cc2357711fcab4a60f87994c1589e5cb5` |
