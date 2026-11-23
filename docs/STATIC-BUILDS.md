---
title: Static builds and linked AOT artifacts
kind: reference
status: draft
updated: 2026-11-16
---

# Static builds: AOT artifacts linked into a native binary

The maintainer's direction of 2026-11-16, from two notes, recorded here as the input for the
artifact-format spec (ROADMAP "Next" (6)). Nothing here is built yet.

## The direction

Linking a waboros AOT artifact into a native binary with the system linker, instead of loading it at
run time, **will be done**, but **not in the first version of the ABI**: it comes after threads, or
even after WASI. D60 rejected linking only for embedding the micro-compiler in the native runtime
(the embedded bytes do that job). For **user modules compiled ahead of time** the linked form is the
one thing that runs them where executable memory cannot be had at run time.

A **static build** is one native binary made of three parts: the C loader and runtime (D36, D60);
the host functions in C, WASI included (fusedwasi's C half); and the program's wasm modules,
compiled ahead of time by the micro-compiler on a build host (also cross, D35) and linked in. It is
the same kind of product as a wasm2c build (one ordinary executable or library, no compiler and no
wasm engine at run time), made by our own pipeline from our own artifact format. **No extra
optimising compiler tier is planned for it** (maintainer): it uses the code the micro-compiler
emits for JIT and AOT.

## Platforms

| platform | what runs |
|---|---|
| JIT allowed (Linux, Windows, macOS with `MAP_JIT`) | JIT, AOT from a cache, linked AOT: everything |
| JIT forbidden, own native binary (an iOS app, strict W^X, firmware) | **linked AOT only**: modules compiled ahead of time on a host and linked into the binary, whose code is ordinary signed `.text` |
| a binary we cannot add native code to (Pythonista) | an interpreter only (`pyc`) |

Limit of the middle row: only modules known at build time; a module that arrives while the program
runs (a plug-in, a download) falls back to an interpreter (`pyc` in Python, or wasm3 where C can be
added).

## What it brings, and what it does not

* **Cheap host calls**, the main gain. Today a wasm call of a Python host function through the
  bridge and a ctypes callback costs about 1.5 µs (BENCHMARKS.md, "1e6 calls of an import from a
  wasm loop": 1 449-1 628 ns per iteration); a call into another JIT instance, a direct call with a
  context switch, about 3 ns. A host function in C reached through the bridge should sit near the
  second number, as wasm2c's plain C call does; for programs that talk to their host often (WASI
  I/O, callbacks, small imports) that can outweigh the whole difference in code quality.
* **No per-module build step at the user's site** (no C compiler per module as with wasm2c), and
  **one toolchain** for every product form (loaded JIT, AOT from a cache, static build: the same
  compiler, artifact and runtime contract, D59, ARTIFACT.md part 3).
* **Not faster compute code**: linking does not change machine code (LTO works on IR, not on
  bytes). On x86-64 CoreMark (maintainer's figures; wasm2c and Wasmtime are not yet rows of
  BENCHMARKS.md): wasm2c ~24.7k it/s (100 %), Wasmtime ~20k (~81 %), waboros and the sljit
  backend under our core ~18k (~73 %). With no optimising tier, that gap is accepted. Expectation
  to measure: **parity with wasm2c on host-call-heavy programs, about three quarters of it on pure
  compute**.

## Checked on the ELF64 prototype (2026-11-15, D59, D60)

`ld.bfd`, `ld.gold`, `ld.lld` (x86-64) and `ld.lld` (aarch64) accept the private `EI_OSABI`. Code
from `jit-x86_64` (a recursive function, a memory loop, an out-of-bounds load) linked inline into a
C host runs through `__waboros_entry_sysv`, the host building the context from ARTIFACT.md part 3;
results, traps and speed (613-643 M iterations/s on the kernel) are as with `mmap`. The linked form
needs three things the loaded form does not: unique global symbols (two artifacts collide on
`__waboros_entry_sysv`; that was the prototype's, before S0015: the file now has no global symbol,
and a linked host calls a function's C entry with its own entry); metadata in the image (`.waboros.*` without `SHF_ALLOC` stays in the file,
and the functions are local symbols C cannot name); `.note.GNU-stack` in every artifact (`ld.bfd`
warns of an executable stack).

## Three rules for format v1, so that the linked form is an amendment later

1. **A loader ignores `sh_flags` of `.waboros.*` sections** and finds them by name (D59), so
   `SHF_ALLOC` metadata reads the same and changing the flags is no format change.
2. **A loader never depends on global symbol names**: it finds the trampolines through a
   `.waboros.*` payload (offsets, `.waboros.trampolines`), never through a symbol; since S0015
   (D70) the file has no entry trampoline and no global symbol at all, the entry being the runtime's;
   `.symtab` stays for tools and a future linker, so the linked form can add a per-artifact prefix
   (for example the module hash) without touching a v1 loader.
3. **Unknown sections are skipped** (D59), so `.note.GNU-stack` and a future descriptor section go
   in without a version change; whether v1 writers already emit an empty `.note.GNU-stack` is the
   spec's choice (harmless, silences `ld.bfd` from day one).

## Loader work later, not format

A mode of the C loader for code already in the image (no `mmap`, copy, W^X switch or extra cache
flush; metadata through a descriptor symbol, the context built as usual); the trampolines as one
shared object of the runtime instead of a copy per artifact; signing and packaging per platform.

## Order

1. Format v1 with the three rules (ROADMAP "Next" (6), D59).
2. The C loader-runtime in its loading form (D36, D60).
3. Threads (D58), then WASI.
4. The static build: the symbol prefix, `SHF_ALLOC` metadata with a descriptor, the in-image mode,
   shared trampolines, C host functions with fusedwasi's C WASI, and the measurements: the import
   loop three ways (C host in a static build, Python host, wasm2c; expect C ≈ wasm2c in single-digit
   nanoseconds), a WASI-heavy corpus program against wasm2c with the same WASI (alternated), and
   CoreMark (no change expected from linking).

**wasmext** (wasm2c output as a Python extension) keeps one specific role after that: the code
quality of an optimising C compiler on compute-bound modules, which is wasm2c itself; keep it as
the working path until static builds run in practice, then decide.
