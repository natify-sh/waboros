---
title: Briefing: read this first
kind: guide
status: current
updated: 2026-11-05
---

# Read this first

This page is the briefing for anyone (person or agent) who starts work on this repository. Everything
else under `docs/` is reference material: read it when a task needs it, not before.

## What this project is, in one paragraph

**A WebAssembly compiler written in pure Python that makes fast machine code at run time, with no
native dependency.** Python can map executable memory (`mmap`) and call into it (`ctypes`), so a
compiler written in Python can emit x86-64 and AArch64 (RISC-V planned, D48) bytes for a wasm module and run
them at native speed. The proof of concept already runs CoreMark at **86 % of Wasmtime/Cranelift with
explicit bounds checks and ~91 % with guard pages**, from a `pip install` that contains only `.py`
files. The package's `jit-x86_64` (explicit bounds checks) reaches 99.2 % of the PoC's JIT on CoreMark
(17 526 vs 17 662 it/s, `docs/BENCHMARKS.md`), and guard pages are the default where verified (D44; CoreMark +9.35 % over explicit checks on x86-64, S0006). Where machine code is not allowed or no backend exists yet, the same front end translates wasm
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
| **this project** | **86–91 %** (JIT, PoC numbers; the package is at the PoC's level with explicit checks, and guard pages are its default where verified), ~0.8 % (`pyc` on CPython, PoC) | **nothing**: standard library only |

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
  20532 pass, 66 expected failures; the oracle, Wasmtime, 63 911 pass over all claimed directories, GC included). `pyc` is frozen
  for new features and gets correctness fixes only (except the proposals that land on every target
  together, and S0010, the maintainer's exception for speed).
* **Phase 3 (`jit-x86_64`, `docs/specs/S0002` step 3) done, including step 6, the final review (S0002, S0003 implemented):**
  3a ABI, 3b integer core, 3c SSE2 floats with `memory.grow`, 3d tables, `call_indirect`, reference
  types and bulk memory, 3e tail calls, typed function references and tags, 3f function imports
  through records (D32), step 4 the fallback to `pyc` with `JitUnavailableWarning` (D31), step 5 the
  ISA gate in CI. `jit-x86_64` fails exactly what `pyc` fails in the core group (20532 pass on both)
  and `auto` selects it; CoreMark 17 526 vs PoC `w2jit2` 17 662 it/s (99.2 %). Phase 3 went beyond the
  "integer core" (floats, tables) because the goal is completeness (D25).
* **Phase 4 done:** multi-memory on both targets; `jit-aarch64` on native arm64 runners at parity with `pyc`
  (interface frozen, D39); the macOS arm64 follow-ups (the python.org CPython runs the JIT); Android x86_64
  checked in CI (RISKS R2).
* **Phase 5 under way** (`docs/ROADMAP.md`): SIMD is complete on all three targets (`docs/specs/S0005-simd.md`,
  D41) and the rename to `waboros` is done (D40). Guard pages are complete (S0006, D44): `auto` gives them on
  Linux glibc and musl x86-64, Linux glibc aarch64, macOS, Windows and Android x86-64, explicit checks
  elsewhere. Platform tests run through cibuildwheel (`platforms.yml`), and `pyc` has a bits mode for
  32-bit x86 (D46). Scalar code generation (S0007) is implemented (steps 1 and 3). Relaxed SIMD and wide arithmetic (S0008) and memory64 with table64 (S0009) are implemented on all three targets. The JIT memory semantics are written once over a
  per-architecture low layer ([S0011](docs/specs/S0011-jit-low-layer.md), implemented, D53), the `pyc` `_br`
  ladders ([S0010](docs/specs/S0010-pyc-branch-emission.md)) are implemented (CoreMark +9-10 %, switch VM x2.5),
  and exceptions with `exnref` ([S0012](docs/specs/S0012-exceptions.md), implemented, D52) are claimed on all
  three targets. **Next:** GC on top of the low layer, then the artifact format, then threads
  (`docs/WASM-PROPOSALS.md`); the public API only in phase 6.
  Native JIT targets are x86-64, aarch64 and riscv64 (planned); every other architecture runs `pyc` (D48).

1. **Read** this page, then `docs/BENCHMARKS.md`, then `docs/ARCHITECTURE.md` and the spec of the step
   in `docs/specs/`. Skip `docs/API.md` and `docs/FEATURES.md` until phase 6.
2. **Set up and check** (commands in `CLAUDE.md`, "Commands"): `uv sync`, `uv run pytest -q`,
   `uv run python tests/spec/fetch.py`, `uv run python -m tests.spec.run --engine jit-x86_64`
   (`auto` selects it on x86-64 where executable memory works).
3. **The metric is the spec suite** (`WebAssembly/spec` `test/core` at a pinned commit) run through a tiny
   internal engine interface, with Wasmtime behind the same interface as the oracle. The work is a loop:
   take the largest group of failures for the current target, fix it, the number grows. `pyc` is the
   semantic floor every other target must match; `tools/spec_diff.py` shows the gap between two targets.
4. **The JIT** emits baseline instructions only (D24); bounds checks are guard pages where verified and
   explicit elsewhere (D44, S0006). The
   second architecture (aarch64 on native arm64 runners, phase 4) came **before** the opcode set was
   widened: it proved the split, and the interface was frozen after it (D39).
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
| why a choice was made | `docs/DECISIONS.md` (D1–D51) |
| what went wrong before | `docs/LESSONS.md` |
| risks and possible answers | `docs/RISKS.md` |
| the JIT artifact contract | `docs/ARTIFACT.md` |
| the public API and CLI (phase 6) | `docs/API.md`, `docs/FEATURES.md` |
| how others did it | `docs/REFERENCES.md` |
| how documents are written and checked | `docs/STANDARDS.md` |
| component specs (contracts, written before implementation) | `docs/specs/` |

The maintainer writes in Ukrainian; code, comments, commits and documents are in English. The project
was called `w2py` until the rename to `waboros` (D40, done 2026-11-02); `poc/` keeps the old name.
