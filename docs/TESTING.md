---
title: Testing
kind: reference
status: current
updated: 2026-11-01
---

# Testing

Every target passes the same tests; a target becomes selectable only when it passes them for the
features it claims.

## Sources of truth and the completeness counters

Completeness is measured, not listed by hand: every axis has an upstream document that says what
"complete" is and an upstream test suite that turns it into a number. CI reports all the numbers; the
exit criterion of each phase (ROADMAP.md) is a value of them.

| axis | what defines it | what measures it | counter |
|---|---|---|---|
| wasm semantics | **WebAssembly Core Specification 3.0** (W3C, 2025: GC, memory64, exceptions, tail calls, multi-memory, relaxed SIMD, typed function references, extended const are part of it); the proposal repositories for what is not in 3.0 (wide-arithmetic, custom-page-sizes, threads) | `WebAssembly/spec` `test/core` at a pinned commit, the proposal repos' tests, Wasmtime's own `tests/misc_testsuite` (Wasmtime-specific behaviour) | spec commands passed, per target and per proposal |
| which features, in which order | Wasmtime's "Wasm Proposals" page (stability tiers): Tier 1 (on by default) is the required set — 17 proposals, 16 without the component model; Tier 2: custom-page-sizes, threads; Tier 3 (branch-hinting, stack-switching, compact-imports) later | the per-target expected-failures files | proposals complete, per target |
| Python API | **wasmtime-py** at the pinned version: its public classes and **its own test suite** (`tests/`) | wasmtime-py's tests run against w2py through a shim (`import w2py as wasmtime`), plus an API-surface test that diffs `dir()` of every public class against wasmtime-py's | wasmtime-py tests passed; missing names |
| CLI | the `wasmtime` binary at the pinned version: `run`, `compile`, `wast`, `settings`, the option groups `-W` (wasm features), `-S` (WASI), `-O`, `-D` | the option list scraped from `wasmtime --help` / `run --help` of the pinned version, not copied by hand | options accepted (or deliberately refused, DECISIONS.md) |
| WASI | p1: `wasi_snapshot_preview1.witx` (`WebAssembly/WASI`, legacy/preview1); p2: the WIT packages `wasi:cli`, `wasi:io`, `wasi:filesystem`, `wasi:clocks`, `wasi:random`, `wasi:sockets`, `wasi:http` | `WebAssembly/wasi-testsuite` (p1 at least; check its p2 coverage), the chosen host's own conformance tests | WASI tests passed |
| everything the suites miss | Wasmtime's behaviour wherever the spec leaves room (NaN, trap codes, stack limits) | differential fuzzing: `wasm-tools smith` modules run on every target and on wasmtime | fuzz hours without a divergence |

When the pinned wasmtime / wasmtime-py version moves, the API-surface diff and the scraped CLI options
show what is new; the spec commit moves the same way, in small steps.

`FEATURES.md` is generated from the expected-failures files (feature × target) by `tools/features.py`.

## Development dependencies (never runtime)

| tool | why |
|---|---|
| **wasmtime** (pip, pinned) | **the test oracle**: differential runs compare every w2py target with Wasmtime on the same module and inputs (results, traps, memory contents); also the reference for API behaviour and feature defaults |
| wasm-tools `json-from-wast` (`wasm-tools-package`) | turns the spec test suite `.wast` files into `.json` + `.wasm` ([S0001](specs/S0001-engine-interface.md)) |
| wasm-tools (`wasm-smith`, `validate`) | random valid modules for differential fuzzing; a second validator |
| pywasm (pip) | a second oracle and a source of ideas: its spec runner (`test/spec.py`) was adapted in the PoC session; it passes the core spec suite (54 006 commands, 0 failures at its pinned spec commit `fffc6e12f`) but treats `assert_invalid` / `assert_malformed` as no-ops |
| meld | fusing component test programs into core modules for WASI 0.2 |
| MicroPython unix port, `mpy-cross` | the `mpy` target |
| QEMU user-mode | JIT targets for other architectures in CI |

The runtime package itself declares **no** dependencies; these live in a development group.
The Python ones are the uv `dev` group in `pyproject.toml` (`uv sync`); wasmtime 50.0.1 needs
Python ≥ 3.9; with the floor at 3.11 (D19) no markers are needed (all dev
tools installed on CPython 3.11, 3.15 and 3.15t; PyPy 3.11 is first checked in CI).

## The spec test suite

* Source: `WebAssembly/spec` `test/core` (+ `simd/`, and the proposal repositories for features not
  merged yet), converted with `wasm-tools json-from-wast` (`wasm-tools-package`). Pin the commit;
  move it in small steps. The contract is [S0001](specs/S0001-engine-interface.md).
* One harness, many targets: the JSON commands (`module`, `register`, `assert_return`,
  `assert_trap`, `assert_exhaustion`, `assert_invalid`, `assert_malformed`, `assert_unlinkable`,
  `assert_uninstantiable`, `action`) are executed through the internal engine interface of S0001, one
  CLI run per engine (`python -m tests.spec.run --engine NAME`), never through the public API. Unlike pywasm, `assert_invalid` / `assert_malformed` are real tests
  of our validator.
* Expected failures are listed per target in a file in the repo (feature not implemented yet), so
  progress is visible and regressions fail.

## Differential testing

* For each module (spec modules, `wasm-smith` output, real programs): run every target and Wasmtime;
  compare return values, trap codes, final memory hash, globals.
* NaN results compare by the spec's rules (canonical / arithmetic NaN), not bit-for-bit.
* A mismatch is minimized (`wasm-tools shrink`) and kept as a regression test.

## Programs and benchmarks

* CoreMark (`poc/in/core.wasm`, `zig cc -O3`): correctness by its own CRC self-check, speed in it/s.
  Record new numbers in BENCHMARKS.md with the conditions.
* WASI: the WASI testsuite (p1), the chosen host's conformance tests (fusedwasi has them for p2),
  small programs in C, Rust, Zig.
* Long calls: a guest loop interrupted from another thread and by SIGINT (CLI) on every target; another
  Python thread's progress during a long call.
* A few larger real modules (e.g. a compression library, a Python-in-wasm build, the ballistics core
  compiled to wasm) for compile time, cache behaviour and speed.

## CI matrix

| axis | values |
|---|---|
| Python | CPython 3.11, 3.12, 3.13, 3.14, 3.15, 3.15t (free-threaded); PyPy 3.11 (D19) |
| OS | Linux x86-64 (all jobs), Windows x64 (`windows-latest`) and macOS x64 (`macos-26-intel`) (`os` job: `pytest` including `tests/jit`, the stack bounds, and the spec run for wasmtime and `pyc`, not for `jit-x86_64`); later Linux aarch64, macOS arm64 (MAP_JIT) |
| target | `pyc` everywhere; `jit-*` where the machine matches; `mpy` with the MicroPython unix port |
| native arm64 | `jit-aarch64` on GitHub's arm64 runners: `ubuntu-24.04-arm`, `macos-latest` (macOS arm64, `MAP_JIT`), `windows-11-arm` (labels per actions/runner-images) |
| emulated | QEMU user-mode only for architectures without native runners (riscv64, armv7, mips32el) as JIT targets appear |

JIT artifacts: run baseline artifacts on an emulated baseline CPU (e.g. QEMU `-cpu` without SSE4.1 /
with plain ARMv8.0) and check that a higher-level artifact is refused there; load artifacts with the C
reference loader as well as from Python; refuse artifacts with an unknown format or context ABI version.

Jobs of `.github/workflows/ci.yml`: `lint` (ruff, pyright, `check_docs.py`, `features.py --check`); `test`
(`pytest -q` on the seven Pythons; `tools/stack_info.py` on 3.13, also in a musl container); `jit`
(`tests/jit` with `W2PY_JIT_REQUIRE=1`, then `isa_all.py`); `spec` (the spec run on `wasmtime`, `pyc`,
`jit-x86_64`, then `auto` under `no_exec.py`); `os`.

The `jit` job runs `tools/isa_all.py` (locally: 379 blobs, about 32 s): it compiles every spec module and CoreMark with
`W2PY_JIT_DUMP` and disassembles each blob with `tools/isa_check.py` (code ranges only), failing on any
instruction above x86-64-v1 (D24).

The `spec` job also runs `--engine auto` with executable memory forbidden: `tools/no_exec.py` sets
`PR_SET_MDWE` (`PR_MDWE_REFUSE_EXEC_GAIN`, Linux >= 6.3; the kernel then refuses `mprotect(PROT_EXEC)` on
written memory, the JIT's W^X path) and `exec`s the runner. `auto` must warn once
(`JitUnavailableWarning`) and give `pyc`'s numbers. `tests/test_fallback.py` does the same in a subprocess.
