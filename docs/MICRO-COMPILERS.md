---
title: Micro-compilers - what they are and why
kind: reference
status: draft
updated: 2026-11-02
---

# Micro-compilers: what they are and why

A note from the maintainer's discussion, for the orchestrator. It explains the idea behind ROADMAP
open question 14 so that later steps keep the doors open. **Nothing here is approved for
implementation**: building it needs its own spec. Until then it only constrains how today's work is
done ("Rules for now" below).

## The idea in one paragraph

A **micro-compiler** is our JIT compiler, rewritten in a language that compiles to WebAssembly (Zig,
C or Rust), shipped as **one small wasm module per target architecture** (`riscv64.wasm`,
`x64.wasm`, ...). It takes wasm bytes and returns a native JIT artifact in our format (ARTIFACT.md,
context ABI of [S0003](specs/S0003-jit-context-abi.md)). Wasm is the portable IR (D16), and the
micro-compiler is a program written in that IR that translates the IR to native code, including
itself.

## Why: portability of the compiler itself (not compile speed)

* **The compiler becomes a portable artifact.** The Python compiler needs CPython with `ctypes`. A
  micro-compiler runs on any wasm engine, even a slow interpreter (`pyc`, wasm3, anything), and
  because it only writes bytes and never runs them, it emits code for **any** architecture from any
  host (cross-compilation, D35).
* **Hosts in other languages get the whole pipeline** (D36): any wasm engine to run the compiler,
  plus a small loader and the runtime services to run its output. No compiler is written per host
  language.
* **Bootstrap on a new machine without a prebuilt binary**: run the micro-compiler on an
  interpreter once, let it compile itself to native, keep that artifact, compile everything else
  natively from then on.
* **New architectures are cheaper**: the core is written once; an architecture adds only its
  backend.
* Compile speed is a side benefit, not the goal: a native one-pass compiler is much faster than the
  Python one (~0.6-0.8 MB/s of wasm code today, BENCHMARKS.md).

Precedents of parts of this: BCPL (compiler shipped as INTCODE), Pascal-P / UCSD p-System, OCaml
(`boot/ocamlc` bytecode run by `ocamlrun`), Zig (`zig1.wasm` bootstrap), GraalVM and Jikes RVM
(compiler written in the language it compiles), and Diverse Double-Compiling (byte-identical
cross-checks). The combination - per-architecture compiler modules written in wasm, compiling wasm,
emitting a stable language-neutral ABI, checked byte-for-byte against a reference - is new.

Not a candidate: `zig1.wasm` itself. It compiles **Zig to C** (only its C backend is enabled) and
needs a system C compiler; it does not compile wasm. Useful lessons from it: an interpreter was too
slow for their bootstrap, so they translate the wasm to C with a ~4k-line `wasm2c` and use `cc`
(a route a C host could also use for our micro-compiler); and a committed binary artifact is not
source, which is why a shipped micro-compiler needs a reproducible build.

## Roles of the two implementations

* **The Python compilers (`jit-x86_64`, `jit-aarch64`) stay the source of truth**: readable,
  debuggable, checked by the spec suite and by parity with `pyc`. They are the reference every
  micro-compiler is compared with, byte for byte, on every spec module and CoreMark. Any backend or
  core change lands in Python first, with tests; a byte difference is a micro-compiler bug by
  definition.
* **They are also the short bootstrap path** on x86-64 and aarch64: our JIT compiles `x64.wasm`
  straight to a native micro-compiler, no `pyc` step.
* **Micro-compilers are the portable path**: architectures with no Python backend first; on x86-64
  and aarch64 they may later become the production path for speed and for other-language hosts,
  with Python kept as the executable specification.

## What gets ported and what stays in the host

Counts are non-blank lines on `phase0-skeleton` at the end of phase 4. The SIMD modules (`frontend/simd.py`, `jit/common/simd.py`, `jit/<arch>/simd.py`, S0005) are compiler-side too and are not counted yet; the SIMD review step (5f) updates this table.

| ported into the micro-compiler | lines |
|---|---|
| `frontend/binary.py` (reader) | 464 |
| `frontend/validate.py` (validator: a foreign host must not need its own) | 590 |
| `jit/common/compiler.py` (one-pass core) | 725 |
| `jit/common/backend.py` (backend interface, frozen by D39) | 114 |
| `jit/common/context.py` (context layout constants, shared with the runtime) | 49 |
| **core total** | **~1 940** |
| `jit/x86_64/{backend,encoder,abi}.py` | ~1 500 |
| `jit/aarch64/{backend,encoder,abi}.py` | ~1 240 |

`abi.py` belongs to the compiler: the trampolines are bytes inside the artifact.

| stays in the host (runtime / loader) | why |
|---|---|
| `jit/common/execmem.py` | executable memory, W^X, per OS |
| `jit/common/instance.py` | context, stacks, entry, host bridge, services (`memory.grow`, `table.grow`, init), traps: the D36 loader |
| `jit/common/target.py`, `runtime/*`, `targets/base.py` | probe, selection, store, linking |
| `targets/pyc/*` | another target; its output is Python source |

Estimate: one micro-compiler = core ~1 900 + one backend ~1 200-1 500 = about 3-3.5k lines in
Zig/C/Rust. The compiler side imports neither `ctypes` nor `mmap` today: keep it that way.

## Requirements on a micro-compiler module

1. **No runtime, no GC, no WASI**: `wasm32-freestanding` / `wasm32-unknown-unknown`. A contract like
   `compile(in_ptr, in_len) -> (out_ptr, out_len)` in its own memory, plus an allocator export;
   versioned like the artifact format.
2. **Only wasm features every waboros target runs**: MVP + Wasm 2.0 core (no SIMD, threads, exceptions,
   GC). Pin toolchain `target-feature`s explicitly; some enable extras by default.
3. **Deterministic**: the same input always gives the same bytes (no randomly seeded hash maps, no
   address-dependent ordering). Byte-identical checks depend on it.
4. **Reproducible build of the module itself**, toolchain pinned, so a shipped compiler artifact can
   be verified by rebuilding it.
5. **Small**: the core plus one backend; size and first-run time on `pyc` recorded in BENCHMARKS.md.

Language choice is about safety and ergonomics, not architecture: Zig (already in the stack via
`zig cc`, tiny freestanding modules), Rust (`no_std`, watch determinism), C (simplest, least safe).

## Bootstrap paths

1. **Short** (x86-64, aarch64): the Python JIT compiles the micro-compiler to native directly.
2. **Long** (no Python backend): `pyc` runs `<arch>.wasm` (~150x slower than native, once per
   machine), it compiles itself, the artifact is cached (key: waboros version, architecture, ISA level).
3. **Cross** (D35): an x86-64 machine runs `riscv64.wasm` and emits a native riscv64 compiler
   artifact for a slow device, which then skips the bootstrap. A shipped artifact must be
   byte-identical with a self-built one.
4. **C host** (D36): the loader plus any wasm engine, or the Zig-style route (wasm -> C -> `cc`).

## Where it fits, and the order

* Prerequisites: the backend interface frozen (**done**, D39), the artifact format and context ABI
  fixed with a version (**phase 5**, D36), the C reference loader in CI (phase 5).
* **First: `arm64.wasm`, a port of the aarch64 backend** (maintainer, 2026-11-02). The Python
  `jit-aarch64` is a ready oracle: every spec module and CoreMark compared byte for byte, so the
  ported core, the backend and the toolchain are validated before anything new is written. aarch64
  is the smaller backend (~1.2k lines).
* **Then the third architecture: `riscv64`**, written as a micro-compiler on the proven core instead
  of a third Python backend; checked by the spec suite under QEMU against `pyc` and Wasmtime (no
  byte oracle exists for it).
* Later candidates (pyc-only today, all in cibuildwheel's `archs`): `armv7l`, `ppc64le`, `s390x`,
  `i686`/`x86`. `i686` is costly (8 registers, i64 as register pairs, x87 vs SSE2, cdecl
  trampolines, 4 GB address space) and declining: lowest priority.
* Replacing the Python compilers on x86-64/aarch64 as the production path: only after the arm64 and
  riscv64 micro-compilers have shown what two implementations cost to keep in step.

## Rules for now (so the door stays open)

* Keep the compiler side free of Python- and OS-specific code: no `ctypes`, `mmap`, threads or
  Python objects in `frontend/` and `jit/common/{compiler,backend,context,simd}.py` or the backends, encoders, `abi.py` and `simd.py` files (`tests/test_source_rules.py` checks it, and that none imports the host side: `execmem`, `instance`, `target`).
* Keep compilation deterministic and host-independent (already checked by D35 in CI).
* Specify runtime services in ARTIFACT.md precisely enough to implement without reading our code
  (D36): the micro-compiler path and every foreign host depend on it.
* Do not start a micro-compiler before phase 5 fixes the artifact format; do not change D39's
  interface for it without the amendment process D39 describes.

## Related (not micro-compilers)

* **Pyodide**: no compiler is needed there; the browser's WebAssembly engine already JIT-compiles a
  guest module. A pass-through target (`WebAssembly.compile` via Pyodide's `js`, imports as proxies,
  our Store and Linker) would give near-native guest speed. Main risk for D25 parity: mapping
  engine-specific `RuntimeError` / `RangeError` messages to trap codes; guest memory is a separate
  `WebAssembly.Memory`, so host access copies. ROADMAP open question on Pyodide; a better route, an
  Emscripten side module sharing Pyodide's heap, is in [PYODIDE.md](PYODIDE.md).
* **Microcontrollers**: not a target for running a micro-compiler on the device (memory). The path
  there is cross-compiling on a host (D35) and a minimal loader, or MicroPython's own emitters
  (`mpy`).
