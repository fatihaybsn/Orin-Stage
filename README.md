# Orin Stage (`ostg`)

[![CI](https://github.com/fatihaybsn/Orin-Stage/actions/workflows/ci.yml/badge.svg)](https://github.com/fatihaybsn/Orin-Stage/actions/workflows/ci.yml)
[![Orin Stage ARM64 Reference](https://github.com/fatihaybsn/Orin-Stage/actions/workflows/orin-stage-reference.yml/badge.svg)](https://github.com/fatihaybsn/Orin-Stage/actions/workflows/orin-stage-reference.yml)

**Orin Stage** is an open-source development workspace engine for x86_64 Linux. It builds release-specific **JetPack 6 / Jetson Linux ARM64 userspace environments** from official NVIDIA inputs, keeps them as persistent workspaces, and lets you test target-side software before moving to a physical Jetson.

The goal is simple: reduce the gap between development on an x86_64 workstation and deployment to **Jetson AGX Orin, Orin NX, and Orin Nano**.

Orin Stage is a **userspace development tool, not a Jetson hardware emulator**. GPU, DLA, camera, kernel, firmware, boot, power, thermal, and performance behavior still require a physical Jetson.

---

## 🎯 Why Orin Stage?

Developing for Jetson from an x86_64 workstation can hide differences until deployment:

- ARM64 vs. x86_64 architecture and ABI behavior
- JetPack/L4T package and filesystem differences
- Python wheels and native library compatibility
- target-specific headers, libraries, loaders, and build assumptions

A normal Ubuntu container is useful, but it does not automatically reproduce a JetPack target userspace.

Orin Stage creates a verified JetPack base from official NVIDIA inputs, then gives each project its own persistent mutable workspace. The same workspace is used for ARM64 userspace execution and as the target sysroot for cross-builds.

---

## 🧭 How it works

```text
Official NVIDIA inputs
        │
        ▼
Verified immutable JetPack base
        │
        ▼
Persistent mutable workspace
       / \
      /   \
     ▼     ▼
ARM64 shell/run        x86_64 cross-build
QEMU linux-user        same tree at /target:ro
      \                 /
       \               /
        └──► Physical Jetson validation
```

Core ideas:

- **Official inputs:** BSP, Sample RootFS, SDK Manager packages, exact target metadata.
- **Immutable base:** a verified base is built once for an exact target and reused.
- **Persistent workspace:** APT, pip, files, and configuration changes live outside the base.
- **ARM64 execution:** CPU-only target processes run through QEMU linux-user and rootless Podman.
- **Same-tree cross-build:** the same workspace is mounted read-only as `/target` for cross-compilation.
- **Traceable identity:** target lock, base digest, workspace generation, and toolchain identity remain inspectable.

---

## ✅ Physical Jetson validation

A **JetPack 6.2.1 / Jetson Linux 36.4.4** workspace was compared with a physical **Jetson Orin NX 16 GB** reference device.

<p align="center">
  <img src="validation/physical/jp6.2.1-orin-nx/physical-orin-nx-jp621.jpeg"
       alt="Physical Jetson Orin NX validation"
       width="760">
</p>

The validation record checks the parts Orin Stage is designed to represent locally:

| Check | Result |
|---|---|
| Target identity | JetPack 6.2.1 / L4T 36.4.4 / ARM64 matched |
| NVIDIA package identity | `nvidia-jetpack` and `nvidia-l4t-core` versions matched |
| `nvidia-l4t-core` payloads | **51 / 51** package-owned payload files matched byte-for-byte |
| ARM64 artifact | the exact same binary was used on Orin Stage and the physical Jetson |
| Runtime behavior | Orin Stage output matched the physical Jetson |
| Negative control | native x86_64 produced the expected different architecture-sensitive result |
| Evidence integrity | frozen SHA-256 manifests verify the recorded files |

A small architecture-sensitive demo makes the difference visible:

```text
x86_64 host              Orin Stage / ARM64        Physical Orin NX
-----------              ------------------        ----------------
plain char: signed       plain char: unsigned      plain char: unsigned
value:      -1           value:      255           value:      255
                                └──────── MATCH ────────┘
```

This demo is not a claim of whole-device equivalence. The physical record combines it with exact JP6.2.1 package identity and package-owned payload verification.

**[View the full JP6.2.1 physical validation record →](validation/physical/jp6.2.1-orin-nx/)**

---

## 📚 JetPack 6 target catalog

| Selector | JetPack | Jetson Linux / L4T | Published physical record |
|---|---:|---:|---|
| `jetson-orin@jp6.0` | 6.0 | 36.3 | — |
| `jetson-orin@jp6.1` | 6.1 | 36.4 | — |
| `jetson-orin@jp6.2` | 6.2 | 36.4.3 | — |
| `jetson-orin@jp6.2.1` | **6.2.1** | **36.4.4** | **Jetson Orin NX 16 GB** |
| `jetson-orin@jp6.2.2` | 6.2.2 | 36.5.0 | — |
| `jetson-orin@jp6.2.3` | 6.2.3 | 36.5.2 | — |

Catalog presence and physical validation are intentionally separate. Use `ostg target list` to see the current support state reported by the installed build.

---

## 💻 Basic workflow

```bash
# Check the host
ostg doctor

# List exact JetPack targets
ostg target list

# Acquire official inputs and ensure the target base
ostg target ensure jetson-orin@jp6.2.1 --allow-validation-pending

# Create a persistent workspace
ostg workspace create \
  --target jetson-orin@jp6.2.1 \
  --name demo \
  --allow-validation-pending

# Open an ARM64 target shell
ostg shell --workspace demo

# Or run a target command directly
ostg run --workspace demo -- /bin/uname -m

# Cross-build using the same workspace as /target:ro
ostg build --workspace demo -- <your-build-command>

# Inspect target, base, workspace, and toolchain identity
ostg inspect --workspace demo

# Inspect disk usage
ostg storage status
```

`--allow-validation-pending` is an explicit opt-in for targets whose catalog status has not yet been promoted to `supported`.

---

## 🔍 Scope

| Represented locally | Requires physical Jetson |
|---|---|
| JetPack / L4T userspace package state | GPU execution |
| ARM64 CPU-only userspace via QEMU | DLA |
| filesystem and dynamic-linker behavior | camera / sensor pipelines |
| APT / pip / non-hardware dependency work | Jetson kernel, device nodes, ioctls |
| cross-build against the target workspace | bootloader / firmware / device tree |
| target-side CPU behavior within the tested corpus | performance / thermal / power behavior |

The physical validation record is therefore evidence for the **userspace and CPU-side scope tested there**, not for the hardware-specific items in the right column.

---

## 🧪 CI and validation

Orin Stage currently uses complementary validation layers:

**CI** runs the automated test suite on GitHub-hosted x86_64 Ubuntu runners for normal code changes.

**ARM64 Reference** is a manually triggered workflow that compares the same deterministic CPU/userspace probe between an Orin Stage JP6 workspace running through QEMU and a GitHub-hosted native ARM64 runner. The native ARM64 runner is generic ARM64 Linux, not a Jetson.

**Physical Jetson validation** adds a matching JetPack reference device. The published JP6.2.1 record contains the terminal recording, exact artifact hashes, package/payload comparisons, negative control, and frozen evidence manifests.

These layers answer different questions; none of them is presented as GPU or whole-system Jetson emulation.

---

## 🧰 Main components

- Python 3.10+
- NVIDIA SDK Manager
- Rootless Podman
- QEMU linux-user / `binfmt_misc`
- pinned ARM64 cross-toolchain
- YAML + JSON Schema target catalog
- `pytest` test suite

---

## 📄 License

Orin Stage is licensed under the [MIT License](LICENSE).
