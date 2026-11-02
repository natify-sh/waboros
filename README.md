---
title: README
kind: guide
status: current
updated: 2026-11-02
---

# waboros

> New here? Read [`BRIEF.md`](BRIEF.md) first.

waboros is a WebAssembly **compiler and runtime written in pure Python, with no runtime dependency**:
the standard library is all it imports. Where Wasmtime has Cranelift, waboros has **targets**: machine-code
JITs written in Python that run through `mmap` + `ctypes` (`jit-x86_64`, `jit-aarch64`) and a translator
to Python code (`pyc`) that runs everywhere else. A target is picked for the machine, never by a module's
content (D25); where the JIT cannot run, `auto` falls back to `pyc` with one warning. Wasmtime is the
test oracle and, in phase 6, the shape of the public API.

## Status

**Pre-alpha.** There is **no public API yet** (phase 6): the release on PyPI is a name reservation and a
preview, not something to build on. What exists is the compiler and the test machinery
around it, run from a checkout.

What passes (details: [`docs/FEATURES.md`](docs/FEATURES.md), [`docs/ROADMAP.md`](docs/ROADMAP.md)):

* The pinned WebAssembly spec suite has 65 353 commands. The directories our targets claim (the core,
  SIMD, bulk memory, multiple memories) hold 55 647 of them, 1 171 of which are text-format checks
  skipped for every engine; of the remaining 54 476, `pyc`, `jit-x86_64` and `jit-aarch64` each pass
  54 385, and the other 91 are expected failures (listed in `tests/spec/xfail/`, mostly tests that use
  GC or exception types). Not claimed yet: memory64 (8 719 commands), GC (798), exceptions (110),
  relaxed SIMD (79).
* Claimed and complete on all three targets: fixed-width SIMD (25 990 commands), multiple memories,
  multi-value, tail calls, typed function references, extended constants, sign extension, non-trapping
  float-to-int. Bulk memory, reference types and mutable globals are partial (a few commands listed as
  expected failures).
* CoreMark and a corpus of real C libraries (Monocypher, SHA-256, BLAKE3, miniz) give the same results as
  `hashlib`, `zlib` and Wasmtime.

Not there yet: the public API, the CLI and WASI (phase 6), memory64, relaxed SIMD, exceptions, GC,
threads, fuel and epochs, the `mpy` (MicroPython) target, other architectures. Bounds checks use guard pages by default on Linux glibc, x86-64 and aarch64 (S0006), and explicit checks elsewhere: there the first JIT compile installs a process-wide SIGSEGV handler, never removed, that passes every fault not raised by waboros's own code on to the previous handler; `WABOROS_JIT_BOUNDS=explicit` opts out and installs nothing.

## Install

```
pip install waboros      # Python >= 3.11, CPython or PyPy; no dependencies
```

This gives you the importable package and nothing to call yet: do not expect a stable `import`. To see
what works, use a checkout (needs [uv](https://docs.astral.sh/uv/)):

```
uv sync
uv run pytest -q                                                          # package tests
uv run python tests/spec/fetch.py                                         # fetch the pinned spec suite
uv run python -m tests.spec.run --engine pyc                              # the spec metric (also: jit-x86_64, auto, wasmtime)
uv run python tools/run_coremark_jit.py poc/in/core.wasm                  # CoreMark on the package's jit-x86_64
uv run python tools/run_coremark_pyc.py poc/in/core.wasm                  # CoreMark on pyc
```

## Platforms

CI ([`docs/TESTING.md`](docs/TESTING.md), "CI matrix") runs the tests on Linux, macOS and Windows on
x86-64, and `jit-aarch64` natively on Linux, macOS (Apple Silicon) and Windows arm64 runners. Besides
that: `jit-aarch64` under QEMU with BTI enforced, `jit-x86_64` on an SSE2-only CPU model under QEMU, the
python.org CPython on macOS (hardened runtime) and CPython's testbed app on an Android emulator. Each
JIT is checked to emit only baseline instructions of its architecture. Where executable memory is
refused, `auto` gives `pyc` and says why ([`docs/RISKS.md`](docs/RISKS.md), R2).

## Speed

CoreMark, it/s, one cloud x86-64 machine (Xeon 2.1 GHz, 2-4 vCPU), CPython 3.13; each run passes
CoreMark's own self-check. Numbers differ by up to ~20 % between sessions, so compare within a row
group. Conditions and history: [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).

| | CoreMark, it/s |
|---|---|
| wasmtime (Cranelift) | 19 044 |
| **package `jit-x86_64`** (median of 5 runs, 2026-10-31) | **17 526** |
| wasm3 (pywasm3) | 2 433 |
| **package `pyc`**, CPython 3.13 | 110.4 / 107.6 |
| pywasm 2.2.3 (pure-Python interpreter), CPython 3.13 | 0.58 |

All rows but wasmtime and wasm3 need nothing but the standard library. Among the pure-Python options,
the package's `pyc` target is about 185 times faster than the pywasm interpreter (107.6 / 0.58), and
the JIT about 30 000 times.

The real-library corpus, microseconds per call, package `jit-x86_64` against Wasmtime: BLAKE2b 1 KiB
3.3 vs 4.5, SHA-256 4 KiB 44.1 vs 37.1, X25519 182 vs 117; `pyc` is 170-360 times slower
(BLAKE2b 1 117, X25519 59 110). SIMD kernels and the PoC rows are in BENCHMARKS.

## The documents

Status `current` documents describe the repository; the rest are the maintainer's notes (drafts),
not approved for implementation.

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | layers, the IR (HIR/LIR), the pipeline, cache, stack depth, long calls and the GIL, traps, memory |
| [`docs/TARGETS.md`](docs/TARGETS.md) | the target interface, automatic selection, how to add an architecture |
| [`docs/ARTIFACT.md`](docs/ARTIFACT.md) | the JIT artifact as a versioned contract: ISA levels, file format, context ABI |
| [`docs/API.md`](docs/API.md) | the planned Python API (after wasmtime-py) and CLI (phase 6) |
| [`docs/FEATURES.md`](docs/FEATURES.md) | what Wasmtime supports and the state of each target (generated table) |
| [`docs/TESTING.md`](docs/TESTING.md) | the spec suite, differential testing, benchmarks, the CI matrix |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | every measurement, with conditions |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | the phases and their status, the open questions |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | the decisions taken and the alternatives rejected |
| [`docs/LESSONS.md`](docs/LESSONS.md) | traps found on the way (bugs, platform limits) |
| [`docs/RISKS.md`](docs/RISKS.md) | risks with possible solutions (not decisions) |
| [`docs/REFERENCES.md`](docs/REFERENCES.md) | projects to read for how things are done correctly |
| [`docs/STANDARDS.md`](docs/STANDARDS.md) | how documents here are written, labelled and checked |
| [`docs/specs/README.md`](docs/specs/README.md) | index of the specifications (`S0001`...), one per component |
| [`docs/ISA-LEVELS.md`](docs/ISA-LEVELS.md) | note (draft): ISA levels above the baseline |
| [`docs/ARTIFACT-CONTAINER.md`](docs/ARTIFACT-CONTAINER.md) | note (draft): our own artifact container or ELF |
| [`docs/MICRO-COMPILERS.md`](docs/MICRO-COMPILERS.md) | note (draft): micro-compilers, a compiler as a wasm module |
| [`docs/MICRO-COMPILERS-SHIPPING.md`](docs/MICRO-COMPILERS-SHIPPING.md) | note (draft): shipping pre-compiled micro-compilers |
| [`docs/COMPONENTS.md`](docs/COMPONENTS.md) | note (draft): components without fusion |
| [`docs/PYODIDE.md`](docs/PYODIDE.md) | note (draft): a guest wasm as an Emscripten side module |
| [`docs/REAL-PROGRAMS.md`](docs/REAL-PROGRAMS.md) | note (draft): real programs beyond CoreMark |

## Contributing

Work is done with agents under fixed rules: [`CLAUDE.md`](CLAUDE.md) (rules, commands, how a step is
organised) and [`BRIEF.md`](BRIEF.md) (what the project is and the order of work).

## The proof of concept (`poc/`)

The package grew out of a proof of concept: stand-alone scripts that run CoreMark on a JIT, on `pyc`, on
MicroPython and on Lua, with the references (wasmtime, wasm3, pywasm). It is kept runnable for
comparisons and is not the package. [`poc/README.md`](poc/README.md) lists every mode and flag.

## Licence

MIT, natify-sh. See [`LICENSE`](LICENSE).
