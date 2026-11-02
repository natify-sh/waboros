---
title: README
kind: guide
status: current
updated: 2026-11-02
---

# waboros

> New here? Read [`BRIEF.md`](BRIEF.md) first.

A WebAssembly **compiler and runtime for Python with no runtime dependencies**: the Python standard
library is all it needs. Its core is a compiler written in Python that emits machine code at run time;
the public API will take the familiar shape of wasmtime-py (Engine, Store, Module, Linker, Instance,
WASI), which is also the test oracle. Where Wasmtime has Cranelift (or Winch, Pulley), waboros has
**targets**: a translator
to Python code (`pyc`), a translator for MicroPython (`mpy`) and machine-code JITs written in Python
that run through `mmap` + `ctypes` (`jit-x86_64`, `jit-aarch64`, later `jit-riscv64`, …). A target is
picked automatically for the machine (never by a module's content, D25), and its output can be cached.

**Status: the package is under development** (branch `phase0-skeleton`; `BRIEF.md` has the state): `pyc`
and the JITs `jit-x86_64` and `jit-aarch64` pass the same spec-suite core; SIMD is being added (S0005);
the public API is phase 6. Everything under `poc/` is the proof of concept: it works on CoreMark (the
code passes CoreMark's own self-check on every target) but covers only what CoreMark needs. The
documents under `docs/` are the plan.

## Why

| | CoreMark, it/s | needs |
|---|---|---|
| wasmtime (Cranelift) | 19 044 | a 30+ MB native wheel |
| **PoC jit-x86_64 (`w2jit2`), CPython 3.8–3.13** | **15 337–15 437** | stdlib only |
| wasm3 (pywasm3) | 2 433 | a native extension |
| PoC pyc (`poc/w2py.py`), PyPy 7.3 | 2 300–2 900 | stdlib only |
| PoC mpy, MicroPython 1.29 unix, `@native` | 94 | nothing |
| PoC pyc, CPython 3.13 | 86–100 | stdlib only |
| pywasm 2.2.3 (pure-Python interpreter) | 0.58 | stdlib only |

Same machine (cloud x86-64, Xeon 2.1 GHz); the first three rows in one session. Full table, conditions
and history: [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).

So: a JIT where the platform allows executable memory (about 80 % of Wasmtime, 6× wasm3), Python code
everywhere else (iOS/Pythonista, hardened systems, unknown architectures), MicroPython where possible.

## The documents

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | layers, the IR (HIR/LIR), the Wasmtime object model, the pipeline, cache, stack depth and recursion, long calls and the GIL, traps, memory, WASI |
| [`docs/TARGETS.md`](docs/TARGETS.md) | the target interface, automatic selection, how to add an architecture step by step, the targets planned |
| [`docs/ARTIFACT.md`](docs/ARTIFACT.md) | the JIT artifact as a language-neutral, versioned contract: ISA levels, the file format, the context ABI |
| [`docs/API.md`](docs/API.md) | the Python API (after wasmtime-py) and the CLI (after the `wasmtime` binary) |
| [`docs/FEATURES.md`](docs/FEATURES.md) | what Wasmtime supports, what waboros must support, the state of each target |
| [`docs/TESTING.md`](docs/TESTING.md) | the spec test suite, differential testing, benchmarks, CI matrix |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | every measurement made, with conditions |
| [`docs/RISKS.md`](docs/RISKS.md) | risks with possible solutions (not decisions): moving artifacts between machines and trust, version skew between generator and runtime, platforms restricting JIT and CPU control-flow integrity, alternatives to a hand-written JIT |
| [`docs/REFERENCES.md`](docs/REFERENCES.md) | projects to read for how things are done correctly (Winch, V8 Liftoff, wasm2c, TinyCC, asmjit, DynASM, MicroPython's emitters, …) — read, understand, write our own |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | the decisions taken and the alternatives rejected, with the reasons |
| [`docs/LESSONS.md`](docs/LESSONS.md) | traps found while building the PoC (bugs, platform limits) |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | the phases of the work and the open questions |
| [`CLAUDE.md`](CLAUDE.md) | context and rules for working on this repository with an agent |

## The proof of concept (`poc/`)

| file | what it is |
|---|---|
| `w2py.py` | wasm → Python translator (CPython, PyPy, `--micropython`, `--legacy`, `--mypyc`, `--inline N`) |
| `w2jit.py` | JIT for x86-64 System V, every value in memory: 3 874 it/s |
| `w2jit2.py` | the same JIT with pinned locals, a top-of-stack register and fused updates: 15 337 it/s |
| `w2lua.py`, `w2luajit.py` | the same translator for Lua 5.4 and LuaJIT (an experiment: 271 / 1 873 it/s) |
| `run_*.py`, `ref_run.py` | runners: CPython, PyPy, MicroPython, mypyc, Lua, the JITs, and the references (wasmtime, wasm3, pywasm) |
| `bench/` | micro-benchmarks behind the decisions (memory access, ctypes call cost, MicroPython viper, …) |
| `in/core.wasm` | CoreMark built with `zig cc -O3` for wasm32; imports `wiring.millis` and `wiring.print`, exports `setup` |
| `out/`, `mypyc/` | what the translators produced for it |
| `patches/warpy-coremark.patch` | what warpy (an RPython wasm interpreter) needed to run CoreMark |

```
cd poc
python3 run_jit2.py in/core.wasm            # the JIT, any CPython >= 3.8 on x86-64 Linux
python3 w2py.py in/core.wasm out/core.py    # translate; then:
python3 run_pyc.py out/core.pyc             # CPython
pypy3   run_py.py  out/core.py              # PyPy
```

`poc/README.md` lists every mode and flag of the PoC.
