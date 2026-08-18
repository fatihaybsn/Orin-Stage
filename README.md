# Orin Stage (`ostg`)

**Orin Stage** is an open-source development workspace engine for x86_64 Linux workstations that bridges the critical gap between host development machines and physical edge deployments (AGX Orin, Orin NX, Orin Nano).

In mission-critical domains such as robotics, autonomous systems, defense, and physical AI, deploying complex software stacks requires absolute target parity. Orin Stage provisions fully configured, deterministic ARM64 **JetPack 6** userspace environments directly from official NVIDIA BSP, Sample RootFS, and SDK Manager releases into persistent, isolated workspaces. It empowers developers to build repositories, execute interactive ARM64 shell sessions, trial APT/pip package dependencies, audit system libraries and filesystem states, and cross-compile with exact sysroot fidelity — providing a complete local staging ground before deploying to physical edge devices.

---

## 🎯 The Problem

Developing edge software on an x86_64 workstation for an ARM64 Jetson target introduces subtle, difficult-to-debug discrepancies:
- **Architecture & ABI Differences:** ARM64 vs. x86_64 instruction execution, native compilation flags, and C/C++ ABI incompatibilities.
- **Ecosystem & Library Parity:** Exact JetPack/L4T userspace versions, Ubuntu releases, system libraries, and CUDA/TensorRT/cuDNN runtime dependencies.
- **Python & Native Dependency Drift:** Discrepancies in prebuilt Python wheels, shared library linking, and missing target dependencies.
- **Synthetic Container Gaps:** Standard Docker/Ubuntu images lack official NVIDIA BSP integration, proper Debian/APT package configurations, and authentic rootfs layouts.

Orin Stage eliminates these blind spots by synthesizing an authentic, fully configured Jetson target userspace directly on your workstation.

---

## 🚀 Key Features & Architectural Principles

- **Official NVIDIA Construction Pipeline:** Uses official Sample RootFS, BSP (`apply_binaries`), and SDK Manager packages with fully configured Debian/APT packages (`dpkg`, alternatives, ld cache).
- **Shared SDK Manager Acquisition Cache:** Orchestrates `sdkmanager` CLI (`downloadonly` mode) with a centralized download cache. SHA-256 verification and acquisition receipts ensure artifacts are downloaded only once.
- **Immutable Base & Isolated Workspaces:** Builds an immutable, verified ARM64 base once per target release. Users spawn independent, mutable directory-based workspaces without mutating the base.
- **Stateless Podman Execution:** Podman is utilized strictly as a transient, rootless execution wrapper:
  - **ARM64 Interactive Shell:** Runs ARM64 Bash, package managers, and CPU-only processes via QEMU user-mode emulation (`binfmt_misc`).
  - **x86_64 Cross-Build Capsule:** Pinned cross-compilation container mounting the workspace as a read-only `/target` sysroot, preventing host environment contamination.
- **Same-Tree Parity:** Interactive target shells and cross-build tools operate on the exact same canonical directory generation.
- **Explicit Version Binding:** Workspaces are permanently bound to a chosen JetPack 6 release digest — no error-prone in-place upgrades or fragile rebase mechanisms.
- **Honest Hardware Boundary:** Clearly delineates reproducible userspace (filesystem, packages, CPU execution, build toolchain) from hardware-dependent components (Tegra GPU/DLA acceleration, kernel modules, camera pipelines, flashing).

---

## 🛠 Supported Scope

| Dimension | Supported | Out of Scope |
|---|---|---|
| **Hardware Family** | Jetson AGX Orin, Jetson Orin NX, Jetson Orin Nano | Jetson Xavier / TX2 / Nano, Non-NVIDIA edge hardware |
| **Software Family** | JetPack 6.x / Jetson Linux (L4T 36.x) | JetPack 5.x / older, JetPack 7.x / Thor (future) |
| **Host System** | x86_64 Linux (Ubuntu 22.04 / 24.04 recommended) | macOS / Windows native (requires Linux container/VM) |
| **Execution** | ARM64 CPU-only userspace (via QEMU), Cross-compilation | Full system hardware emulation, Tegra GPU execution |

---

## 📦 Technology Stack

- **Core Engine:** Python 3.10+
- **Configuration & Schema:** YAML, JSON Schema (`jsonschema`), `PyYAML`
- **Acquisition:** NVIDIA SDK Manager CLI (`sdkmanager`)
- **Isolation & Execution:** Rootless Podman, QEMU user-mode (`qemu-aarch64-static`)
- **Testing:** `pytest` (unit, semantic schema validation, receipt audits, acceptance tests)

---

## 💻 Workflow Example (Concept)

```bash
# 1. List supported JetPack 6 targets in the catalog
ostg target list

# 2. Ensure official artifacts and build the immutable base
ostg target ensure jetson-orin@jp6.2.3

# 3. Create an isolated workspace for your project
ostg workspace create --target jetson-orin@jp6.2.3 --name edge-vision

# 4. Open an interactive ARM64 shell inside the target environment
ostg shell --workspace edge-vision

# 5. Cross-compile your host repository against the target sysroot
ostg build --workspace edge-vision

# 6. Inspect workspace metadata, package state, and storage usage
ostg inspect --workspace edge-vision
ostg storage status
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE) (or OSI/FSF compliant open-source license).
