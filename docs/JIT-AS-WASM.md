---
title: The JIT compilers as AOT-compiled wasm modules - replace the Python JIT backends instead of keeping two
kind: reference
status: draft
updated: 2026-11-05
---

# The JIT compilers as AOT-compiled wasm modules

A proposal from the maintainer's discussion (2026-11-05), with the orchestrator's notes and the
maintainer's answers to them. Nothing here is decided or measured. A decision (the next Dn) is taken
**after the artifact format is fixed** (ROADMAP.md phase 5, item 6), not before.

## The idea

[MICRO-COMPILERS.md](MICRO-COMPILERS.md) plans `arm64.wasm` as a port of `jit-aarch64` that lives
**next to** the Python backend, and puts the replacement of the Python compilers off "until arm64 and
riscv64 have shown what two implementations cost to keep in step". The proposal is the other end of
that sentence: **port, then replace.** `jit-aarch64` is written again in C (or Zig), built to
`arm64.wasm`, compiled once ahead of time into an ordinary JIT artifact, and the Python `jit-aarch64`
is removed once the port matches it. Then the same for `jit-x86_64`. Python keeps `pyc`, the runtime
and the loader; every JIT compiler is an AOT artifact made from wasm.

**What it gains** (maintainer, 2026-11-05): one implementation of each compiler instead of two, and a
compiler that **moves to another runtime** unchanged: a host written in Rust, C++, Zig or anything
that runs wasm gets the whole pipeline (D36; MICRO-COMPILERS.md, "Hosts in other languages"). Compile
speed is measured and recorded, but it is **not** the threshold of the decision.

## The maintainer's position on the usual objections (2026-11-05)

1. **The brief.** A JIT compiler shipped as an AOT artifact compiled from wasm fits the project: the
   package contains no native extension; the artifact is data loaded by our loader (D36). BRIEF.md
   predates the micro-compilers and is rewritten when the decision is taken.
2. **The oracle.** The Python `jit-aarch64` has no byte oracle of its own today either: it is checked
   by the spec suite against `pyc` and Wasmtime, and its encoder against llvm-mc. After the
   replacement aarch64 is checked the same way. The byte-for-byte comparison is needed only during
   the port.
3. **Encoder tests** run in C: the same sources built natively into a test binary that compares every
   form with `llvm-mc --show-encoding` (LESSONS 43 carries over).
4. **Bootstrap** is not needed in an installed wheel: the wheel ships the AOT artifact. The bootstrap
   paths of MICRO-COMPILERS.md remain for verification (a shipped artifact is byte-identical with a
   self-built one, D35) and for an architecture without a shipped artifact.
5. **One wheel** (maintainer): the artifacts of every architecture and ISA level go into the one
   `py3-none-any` wheel as data (D40); no per-platform wheels.

## Why only after the artifact format is fixed

* The ported compiler emits artifacts and is itself an artifact; both must be in a fixed, versioned
  format ([ARTIFACT.md](ARTIFACT.md) part 2, context ABI part 3).
* The **one LIR vocabulary** (ROADMAP phase 5) turns today's string operation names into one
  enumerated list; port one list, not several naming schemes. The low layer of
  [S0011](specs/S0011-jit-low-layer.md) (`ll_*` operations under one shared semantic layer) is the
  first part of that list and makes the port cheaper: one semantic layer plus thin per-architecture
  layers, instead of two thick backends.
* The **C reference loader and the specified runtime services** (D36) are what runs the artifact of
  the compiler outside Python.

**Exceptions and GC** stay in the ROADMAP order: written in Python first, so the Python
implementation is the byte oracle for that part of the port. Format v1 must leave room for new
sections (stack maps for GC), or be fixed after them, as the ROADMAP order already has it.
Exceptions need no handler table per module: they propagate as a status word checked after each
call (S0012, D52).

## The shared core

`jit/common/{compiler,backend,context,simd}.py` serve both JIT backends. While `jit-x86_64` stays in
Python the core exists in two languages, so the decision is **"the JIT compilers in C"**: aarch64
first (the smaller backend), then x86-64 by the same steps, and only then is the Python core deleted.
Between the two ports the Python core and `jit-x86_64` stay the byte oracle for core changes.

The Python front end (`binary.py`, `validate.py`) stays: `pyc` and `mpy` need it. The micro-compiler
carries its own validator, so the front end is in two languages permanently; the nightly differential
fuzz (ROADMAP) also compares their accept/reject decisions.

## Plan

0. **Prerequisite:** the artifact format and context ABI fixed, one LIR vocabulary, the C loader in CI
   (ROADMAP phase 5, item 6).
1. **Port** the core and the aarch64 backend without SIMD, `wasm32-unknown-unknown`, pinned
   toolchain, no WASI. Acceptance: byte-identical output with the Python `jit-aarch64` on every spec
   module and CoreMark (native arm64 runners and QEMU); encoder test in C against llvm-mc; the module
   is deterministic (D35: the same bytes when run on `pyc`, `jit-x86_64` and Wasmtime); the artifact
   in the wheel is byte-identical with a self-built one, checked in CI and at release.
2. **Measure and record** (BENCHMARKS.md): compile speed of the `arm64.wasm` artifact against the
   Python `jit-aarch64` (CoreMark, zip.wasm, a large module), the cost of loading the compiler artifact
   per process, the size of `arm64.wasm`, of its artifact and of the wheel, bootstrap time on `pyc`.
3. **Decide (Dn).** The criterion is byte identity and portability (the port runs unchanged in a
   non-Python host, shown by the C loader), not compile speed; the measured costs are recorded with it.
4. If accepted: SIMD for `arm64.wasm`; ship the artifact in the one wheel (one per ISA level; the
   OS-specific parts selected by a compile parameter, so one artifact serves Linux, macOS, Windows and
   Android); remove the Python `jit-aarch64`; then `x64.wasm` by the same steps; then the Python core.
   `riscv64.wasm` is written on the ported core from the start.

## Costs and risks

* **Porting is mechanical, byte identity is not:** unbounded ints vs fixed width (constant folding,
  immediate-range checks, masks), `%`, `//`, `>>` on negative numbers, `dict`/`set` iteration order,
  exceptions used as control flow (`Unsupported`), recursion depth. The byte oracle finds these, which
  is why the port happens while the Python version exists.
* **Debugging** is harder, inside a wasm32 module even more; a native build of the same sources for
  tests and debugging is part of step 1.
* **Machine code in the wheel**: reproducible (D35) and checked by the bootstrap; a release gains a
  build step per architecture and ISA level.
* **Language of the port**: C (simplest; the same toolchain as sljit, so a later `sljit-<arch>.wasm`,
  D48, builds the same way), Zig, or Rust; MICRO-COMPILERS.md lists the trade-offs. To be decided with
  the Dn.

## What to decide (at the Dn, after ROADMAP item 6)

1. The direction: replace, not duplicate.
2. The language of the port.
3. BRIEF.md and CLAUDE.md rewritten: the compilers are written once and shipped as artifacts; Python
   is the runtime, the loader and `pyc`.
