---
title: Briefing: read this first
kind: guide
status: current
updated: 2026-11-02
---

# Read this first

This page is the briefing for anyone (person or agent) who starts work on this repository. Everything
else under `docs/` is reference material: read it when a task needs it, not before.

## What this project is, in one paragraph

**A WebAssembly compiler written in pure Python that makes fast machine code at run time, with no
native dependency.** Python can map executable memory (`mmap`) and call into it (`ctypes`), so a
compiler written in Python can emit x86-64 (later AArch64, RISC-V, …) bytes for a wasm module and run
them at native speed. The proof of concept already runs CoreMark at **86 % of Wasmtime/Cranelift with
explicit bounds checks and ~91 % with guard pages**, from a `pip install` that contains only `.py`
files. The package's `jit-x86_64` (explicit bounds checks) reaches 99.2 % of the PoC's JIT on CoreMark
(17 526 vs 17 662 it/s, `docs/BENCHMARKS.md`); guard pages in the package are phase 5. Where machine code is not allowed or no backend exists yet, the same front end translates wasm
to Python source (`pyc`), or to MicroPython code (`mpy`). Around the compiler sits a **thin runtime**
(memories, tables, instances, traps, host calls, WASI) — necessary, but not the point.

## Why it exists (the gap it fills)

| option | speed | what it costs the user |
|---|---|---|
| Wasmtime (wasmtime-py) | 100 % | a 30 MB native library per platform |
| wasm2c + a C build (natify-sh/wasmext) | ~95 % of native | a C toolchain and a build **per module per platform** |
| WAMR AOT | ~120 % | `wamrc` (LLVM) per module per platform, a native runtime, `.aot` tied to the WAMR version; modules cannot share memories or tables |
| wasm3 | ~13 % | a native extension |
| pywasm (pure-Python interpreter) | ~0.003 % | nothing |
| **this project** | **86–91 %** (JIT, PoC numbers; the package is at the PoC's level with explicit checks), ~0.8 % (`pyc` on CPython, PoC) | **nothing**: standard library only |

Measured numbers and conditions: `docs/BENCHMARKS.md`. The niche: run wasm from Python fast,
anywhere Python runs, without anyone building anything native.

## What it is NOT (read this twice)

* **Not "a reimplementation of wasmtime-py".** Wasmtime appears in the documents in two roles only:
  1. **the test oracle** — when the spec leaves room, and in differential tests, we behave as Wasmtime
     does (trap codes, NaN rules, limits);
  2. **the shape of the public Python API, later** (Engine/Store/Module/Linker/Instance), so users
     recognise it. Building that API is phase 6. **Do not start there.** API parity is not a goal by
     itself and is never worked on before the compiler and the spec tests need it.
* **Not an interpreter.** Every target translates the whole module ahead of execution (at load time).
* **Not a runtime that is embedded into generated code.** The runtime lives in the package; generated
  code is a cache artifact of it (DECISIONS.md D18).
* **Not the component model.** Components are fused into one core module beforehand (`meld fuse`).

## The architecture in five lines

```
wasm bytes -> front end (parse, validate, decode) -> HIR (trees + structured control, analyses)
   -> pyc target: Python source -> code object            (always available; the semantic reference)
   -> mpy target: MicroPython source / viper              (MicroPython)
   -> LIR (machine-neutral linear ops) -> jit-<arch>: machine code in mmap, entered through ctypes
runtime (package): memories, tables, globals, instances, traps, host calls, WASI host, cache
```

Details: `docs/ARCHITECTURE.md` ("The IR", "Memory", "Long calls and the GIL"), `docs/TARGETS.md`.

## What the PoC proves (and what it does not)

`poc/` is a set of scripts, not the package. It proves feasibility and gives the numbers; it covers
only what its test programs needed.

* `poc/w2py.py` — front end + `pyc` translator. Runs CoreMark on CPython (~150 it/s), PyPy (~3 000),
  MicroPython (~90); also ran **meld** (a 1.7 MB Rust program) byte-identically to Wasmtime.
* `poc/w2jit2.py` — the x86-64 JIT (pinned locals in registers, lazy value stack, fused compare+branch,
  jump tables for `br_table`, `if`/`else`): CoreMark ~16 800 it/s.
* `poc/w2jit_guard.py` — the same JIT with **guard pages**: an 8 GiB reservation plus a SIGSEGV handler
  that the JIT emits as machine code (no Python in the signal path): ~19 000 it/s.
* Compile speed of the JIT in Python: ~0.6–0.8 MB of wasm code per second (Cranelift on 2 cores is
  1.5–2.5× faster) — so a cache and, later, lazy per-function compilation matter.
* Gaps the PoC does not cover (the package has since closed the validator, the JIT opcode set of the
  core group and the runtime objects it needs; see "Where to start"): the validator, most of the opcode set in the JIT (`call_indirect`,
  full i64, floats beyond CoreMark, bulk memory, `memory.grow`), block parameters, reference types,
  SIMD, the Wasm 3.0 features (GC, exceptions, tail calls, memory64, multi-memory, …); the runtime
  objects; WASI.

## Where to start

State of `main` (phases 0–4 and SIMD merged on 2026-11-02, published as 0.0.0); work continues on branches:

* **Done, phases 0–2:** package layout, strict binary reader and validator (`src/waboros/frontend/`), the
  spec-suite runner (the metric), and the `pyc` target complete for the core group (spec core: `pyc`
  20509 pass, 89 expected failures; wasmtime oracle 63 782 over all claimed directories). `pyc` is frozen
  for new features and gets correctness fixes only.
* **Phase 3 (`jit-x86_64`, `docs/specs/S0002` step 3) done, including step 6, the final review (S0002, S0003 implemented):**
  3a ABI, 3b integer core, 3c SSE2 floats with `memory.grow`, 3d tables, `call_indirect`, reference
  types and bulk memory, 3e tail calls, typed function references and tags, 3f function imports
  through records (D32), step 4 the fallback to `pyc` with `JitUnavailableWarning` (D31), step 5 the
  ISA gate in CI. `jit-x86_64` fails exactly what `pyc` fails in the core group (20509 pass on both)
  and `auto` selects it; CoreMark 17 526 vs PoC `w2jit2` 17 662 it/s (99.2 %). Phase 3 went beyond the
  "integer core" (floats, tables) because the goal is completeness (D25).
* **Phase 4 done:** multi-memory on both targets; `jit-aarch64` on native arm64 runners at parity with `pyc`
  (interface frozen, D39); the macOS arm64 follow-ups (the python.org CPython runs the JIT); Android x86_64
  checked in CI (RISKS R2).
* **Phase 5 under way, SIMD first** (`docs/specs/S0005-simd.md`, D41): the front end and `pyc` are done and
  `pyc` claims `simd/`; `jit-x86_64` compiles every operator with SSE2 only and claims `simd/` at
  parity with `pyc` (5d3); `jit-aarch64` compiles every operator in NEON and claims `simd/` too (5e2);
  SIMD is complete (5a-5f: the benchmark and the review are done) and the rename to `waboros` is done (D40).
  **Next, per `docs/ROADMAP.md`:** the merge to main and publishing (the maintainer's step), then
  phase 5 continues: guard pages (D29), the rest of the opcode set; the public API only in phase 6.

1. **Read** this page, then `docs/BENCHMARKS.md`, then `docs/ARCHITECTURE.md` and the spec of the step
   in `docs/specs/`. Skip `docs/API.md` and `docs/FEATURES.md` until phase 6.
2. **Set up and check** (commands in `CLAUDE.md`, "Commands"): `uv sync`, `uv run pytest -q`,
   `uv run python tests/spec/fetch.py`, `uv run python -m tests.spec.run --engine jit-x86_64`
   (`auto` selects it on x86-64 where executable memory works).
3. **The metric is the spec suite** (`WebAssembly/spec` `test/core` at a pinned commit) run through a tiny
   internal engine interface, with Wasmtime behind the same interface as the oracle. The work is a loop:
   take the largest group of failures for the current target, fix it, the number grows. `pyc` is the
   semantic floor every other target must match; `tools/spec_diff.py` shows the gap between two targets.
4. **The JIT** keeps explicit bounds checks and baseline x86-64 only until phase 5 (D24, D29). The
   second architecture (aarch64 on native arm64 runners, phase 4) comes **before** the opcode set is
   widened: it is what proves the split, and the interface is frozen after it.
5. **Only then** the public runtime API in the shape of wasmtime-py, WASI (fusedwasi is the candidate
   host), the cache and serialized files (thin/fat), the CLI.
6. The PoC (`poc/`) stays runnable for comparisons (commands in `CLAUDE.md`).

Every speed claim goes into `docs/BENCHMARKS.md` with its conditions; every behaviour claim comes with
a test. "Measure, do not estimate."

## Hard rules (short form; full list in `CLAUDE.md`)

* Package imports nothing outside the standard library.
* Never crash the interpreter: bad accesses, deep recursion, host exceptions become traps/exceptions.
* One target interface, one test suite for all targets.
* The JIT artifact format and context ABI are versioned contracts (`docs/ARTIFACT.md`).
* The shared layers (front end, HIR, LIR) contain nothing Python-specific.
* Read how others did a hard thing (`docs/REFERENCES.md`: Winch, V8 Liftoff, wasm2c, TinyCC, …) and
  write our own; never translate their code.

## Map of the documents

| when you need | read |
|---|---|
| the numbers | `docs/BENCHMARKS.md` |
| how it is built | `docs/ARCHITECTURE.md`, `docs/TARGETS.md` |
| the order of work | `docs/ROADMAP.md` |
| how it is tested | `docs/TESTING.md` |
| why a choice was made | `docs/DECISIONS.md` (D1–D41) |
| what went wrong before | `docs/LESSONS.md` |
| risks and possible answers | `docs/RISKS.md` |
| the JIT artifact contract | `docs/ARTIFACT.md` |
| the public API and CLI (phase 6) | `docs/API.md`, `docs/FEATURES.md` |
| how others did it | `docs/REFERENCES.md` |
| how documents are written and checked | `docs/STANDARDS.md` |
| component specs (contracts, written before implementation) | `docs/specs/` |

The maintainer writes in Ukrainian; code, comments, commits and documents are in English. The project
was called `w2py` until the rename to `waboros` (D40, done 2026-11-02); `poc/` keeps the old name.
