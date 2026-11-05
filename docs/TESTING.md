---
title: Testing
kind: reference
status: current
updated: 2026-11-05
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
| Python API | **wasmtime-py** at the pinned version: its public classes and **its own test suite** (`tests/`) | wasmtime-py's tests run against waboros through a shim (`import waboros as wasmtime`), plus an API-surface test that diffs `dir()` of every public class against wasmtime-py's | wasmtime-py tests passed; missing names |
| CLI | the `wasmtime` binary at the pinned version: `run`, `compile`, `wast`, `settings`, the option groups `-W` (wasm features), `-S` (WASI), `-O`, `-D` | the option list scraped from `wasmtime --help` / `run --help` of the pinned version, not copied by hand | options accepted (or deliberately refused, DECISIONS.md) |
| WASI | p1: `wasi_snapshot_preview1.witx` (`WebAssembly/WASI`, legacy/preview1); p2: the WIT packages `wasi:cli`, `wasi:io`, `wasi:filesystem`, `wasi:clocks`, `wasi:random`, `wasi:sockets`, `wasi:http` | `WebAssembly/wasi-testsuite` (p1 at least; check its p2 coverage), the chosen host's own conformance tests | WASI tests passed |
| everything the suites miss | Wasmtime's behaviour wherever the spec leaves room (NaN, trap codes, stack limits) | differential fuzzing: `wasm-tools smith` modules run on every target and on wasmtime (`tools/fuzz.py`, below) | fuzz hours run; every divergence found triaged (ROADMAP.md, phase 5 exit) |

When the pinned wasmtime / wasmtime-py version moves, the API-surface diff and the scraped CLI options
show what is new; the spec commit moves the same way, in small steps.

`FEATURES.md` is generated from the expected-failures files (feature × target) by `tools/features.py`.

## Development dependencies (never runtime)

| tool | why |
|---|---|
| **wasmtime** (pip, pinned) | **the test oracle**: differential runs compare every waboros target with Wasmtime on the same module and inputs (results, traps, memory contents); also the reference for API behaviour and feature defaults |
| wasm-tools `json-from-wast` (`wasm-tools-package`) | turns the spec test suite `.wast` files into `.json` + `.wasm` ([S0001](specs/S0001-engine-interface.md)) |
| wasm-tools (`wasm-smith`, `validate`) | random valid modules for differential fuzzing; a second validator |
| pywasm (pip) | a second oracle and a source of ideas: its spec runner (`test/spec.py`) was adapted in the PoC session; it passes the core spec suite (54 006 commands, 0 failures at its pinned spec commit `fffc6e12f`) but treats `assert_invalid` / `assert_malformed` as no-ops |
| meld | fusing component test programs into core modules for WASI 0.2 |
| MicroPython unix port, `mpy-cross` | the `mpy` target |
| QEMU user-mode | JIT targets for other architectures in CI; `jit-aarch64` BTI (the arm64 Linux runner has none): `tools/run_arm64.py` in the x86 `jit` job; an SSE2-only x86-64 CPU (`qemu64` without SSE3, SSSE3, SSE4 and popcnt) for the SIMD tests and the `simd` spec run of `jit-x86_64` (S0005) |

The runtime package itself declares **no** dependencies; these live in a development group.
The Python ones are the uv `dev` group in `pyproject.toml` (`uv sync`); wasmtime 50.0.1 needs
Python ≥ 3.9; with the floor at 3.11 (D19) no markers are needed (all dev
tools installed on CPython 3.11, 3.15 and 3.15t; PyPy 3.11 is first checked in CI).

## The spec test suite

* Source: `WebAssembly/spec` `test/core` (+ `simd/`, and the proposal repositories for features not
  merged yet), converted with `wasm-tools json-from-wast` (`wasm-tools-package`). Pin the commit;
  move it in small steps. A proposal not yet in the spec repository is a second pin in
  `tests/spec/fetch.py` (`PROPOSAL_FILES`: one file at a commit, with its sha256; today
  `wide-arithmetic.wast` of `WebAssembly/wide-arithmetic`, S0008); it moves to the spec pin when merged. The contract is [S0001](specs/S0001-engine-interface.md).
* One harness, many targets: the JSON commands (`module`, `register`, `assert_return`,
  `assert_trap`, `assert_exhaustion`, `assert_exception`, `assert_invalid`, `assert_malformed`, `assert_unlinkable`,
  `assert_uninstantiable`, `action`) are executed through the internal engine interface of S0001, one
  CLI run per engine (`python -m tests.spec.run --engine NAME`), never through the public API. Unlike pywasm, `assert_invalid` / `assert_malformed` are real tests
  of our validator.
* Expected failures are listed per target in a file in the repo (feature not implemented yet), so
  progress is visible and regressions fail.

## Differential testing

* For each module (spec modules, `wasm-smith` output, real programs): run every target and Wasmtime;
  compare return values, trap codes, final memory hash, globals.
* NaN results compare by the spec's rules (canonical / arithmetic NaN), not bit-for-bit.
* A mismatch found by the fuzzer is not fixed at once: it is kept with a short description (which
  targets differ, on which export) and triaged before each large spec closes and before phase 5 ends
  (ROADMAP.md, "Next"); one that is fixed is minimized (`wasm-tools shrink`) and kept as a regression
  test.
* `tools/fuzz.py` (S0010 step 0): `uv run python tools/fuzz.py N SEED [--fbits]` runs N
  `wasm-tools smith --ensure-termination` modules from seed SEED, bounded (1 MiB memory, 1 000 table
  elements, 3-12 functions, 3 000 instructions; no SIMD, reference types, GC, threads or
  memory64, and exceptions only in the cross-target mode; a 20 s alarm per module: unbounded modules got the process OOM-killed). Every export is
  called three times with arguments drawn from the seed; results, trap codes and a hash of memory are
  compared between three `pyc` sources: the default (S0010's rules), the emission before S0010
  (`WABOROS_PYC_BR=0`) and that source after `poc/pyc-bropt/bropt.py` (the oracle;
  [S0010](specs/S0010-pyc-branch-emission.md), step 1). A source that fails to generate, compile or
  instantiate in one configuration only is a mismatch too. A mismatch saves `bad_<seed>.wasm`; the
  exit status is non-zero on any mismatch. The oracle disagreeing is counted apart
  (`oracle_<seed>.wasm`): `bropt.py` has a defect of its own (BENCHMARKS.md, S0010 step 1). `--gen`
  replaces smith by a structured generator of the shapes S0010 rewrites, bounded by a fuel counter:
  switches (a chain of case blocks with an innermost `br_table`) with result-typed case blocks,
  fall-through and empty case regions, loops around a switch (a Python loop as the parent), the
  dispatch inside an `if` arm or an extra block, code and loops before the dispatch with branches out
  to outer loops, branches in a case block before the next one opens, degenerate tables with a plain
  identifier index or only a default; `if`/`else` frames, blocks with a result, `return`,
  `return_call`, `unreachable`. It draws `BRTREE` from {0, 1, 2, 4} per module and also compares
  `sink` and `dse` alone; the rules change 0.9 % of smith modules (8 of 938) and 40 % of `--gen` modules (4 045 of 10 000). `--fbits` runs it
  with `WABOROS_PYC_FBITS=1` (D46).
* `tools/fuzz.py --targets pyc,jit,wasmtime [--minutes M] [--seed S] [--gen] [--out DIR]` compares
  `pyc`, `jit-x86_64` in guard and in explicit mode (`WABOROS_JIT_BOUNDS` set per module, in process)
  and Wasmtime (the spec runner's engines, so the same config switches) for `M` minutes. Per export
  call (three random argument sets with edge values, or the 28 fixed pairs with `--gen`) it compares
  the results, the trap code and a hash of every exported memory; a NaN result compares as any NaN
  (a v128 one also under f32x4 and f64x2 lanes) and smith runs with `--canonicalize-nans`, because
  which NaN is made is nondeterministic. Smith modules draw SIMD (40 %), memory64 (25 %), wide
  arithmetic (20 %) and 1 or 3 memories per seed; relaxed SIMD stays off (its results may differ by
  design). A target that cannot run a module (`Unsupported`, a load limit) is skipped for it and
  counted; a divergence (also in instantiating) writes `div_<seed>.wasm` and `div_<seed>.txt` (the
  export, the arguments, each side's outcome and which differ from Wasmtime) into `--out`
  (default `fuzz-out`). The exit status is always 0; the last line is the summary (modules, skips
  per target, timeouts, divergences). The workflow `fuzz-nightly.yml` (cron, and `workflow_dispatch`
  with the minutes) runs smith and `--gen` for 45 minutes each outside `ci` and `platforms`, never
  blocks, puts the counts and the descriptions in the job summary and keeps the artifact `fuzz-out`
  (the modules, the descriptions, the logs) for 14 days; a divergence is triaged later (above), not
  fixed at once.
* Triage of the first runs (2026-11-05, 43 divergent modules from seeds 319-4020, 50204-50544, 70475-71186,
  90086-93499 and 900223-908219; the reduced cases are in `tests/test_pyc_trap_order.py`): in all of them only `pyc` differed, in
  five causes, all fixed and covered by `tests/test_pyc_trap_order.py` with the reduced modules. Four
  were the order in which two traps compete (the first trapping instruction wins; `pyc` evaluates
  operands as Python expressions, so a statement made of a later operand ran before an earlier
  pending one: `drop`, `select`, `local.set`/`local.tee`, a `flush` that skipped an earlier trapping
  entry), the arguments of `call_indirect` against its table check, a bad address in the table index
  caught as the table's `IndexError`, and the value of an `i32.store8` (`M[a] = v` evaluates `v`
  first); the fifth was a name collision (element segment 8 was `S8`, the runtime's signed-byte
  reader; LESSONS 96). A review of the fix added two more of the same class, none yet seen by the fuzzer: a bool `i32.and`/`i32.or` lowered to short-circuit `and`/`or`, and `table.grow` evaluating its count before its initial value. The cross-target run with smith's exceptions enabled was triaged the same way when S0012
  closed (2026-11-05: every divergence was a `pyc` defect outside the exception code, now fixed; a
  3-minute run found none). Later runs: triage them before the GC spec and phase 5 close
  (ROADMAP.md, "Next").

## Programs and benchmarks

* CoreMark (`poc/in/core.wasm`, `zig cc -O3`): correctness by its own CRC self-check, speed in it/s.
  Record new numbers in BENCHMARKS.md with the conditions.
* Corpus of freestanding libraries (`poc/bench/corpus/`, `tools/run_corpus.py`): Monocypher, SHA-256,
  BLAKE3 and miniz checked against `hashlib`, `zlib`, reference vectors and pure-Python references on every
  target and Wasmtime; exit status non-zero on a mismatch. Timings in BENCHMARKS.md.
* Calls benchmark (`poc/bench/calls/`, `tools/run_calls_bench.py`, S0007): a 2-argument leaf called in a
  loop and recursive `fib(27)`, ns per call on `jit-x86_64` and Wasmtime; fails if the results differ.
* WASI: the WASI testsuite (p1), the chosen host's conformance tests (fusedwasi has them for p2),
  small programs in C, Rust, Zig.
* Long calls: a guest loop interrupted from another thread and by SIGINT (CLI) on every target; another
  Python thread's progress during a long call.
* A few larger real modules (e.g. a compression library, a Python-in-wasm build, the ballistics core
  compiled to wasm) for compile time, cache behaviour and speed.

## CI matrix

| axis | values |
|---|---|
| Python | `ci` job `test`: CPython 3.11, 3.12, 3.13, 3.14, 3.15, 3.15t (free-threaded); PyPy 3.11 (D19). `platforms.yml`: a narrower set per platform (`ci/harness/pyproject.toml`: the oldest and the newest free-threaded, 3.14 on Linux x86_64 glibc, 3.13 for musl, i686, armv7l, Android and Pyodide) |
| OS | Linux x86-64 (all jobs of `ci`, and `platforms.yml`: glibc, musl, i686); Linux arm (`ubuntu-24.04-arm`: job `arm64` of `ci`, and aarch64 and armv7l in `platforms.yml`); Windows x64 (`windows-latest`), macOS x64 (`macos-26-intel`), Windows ARM64 (`windows-11-arm`) and macOS arm64 (`macos-26`) only in `platforms.yml` (cibuildwheel, below) |
| target | `pyc` everywhere; `jit-*` where the machine matches; `mpy` with the MicroPython unix port (phase 7, not in CI yet) |
| native arm64 | `jit-aarch64` on GitHub's arm64 runners: `ubuntu-24.04-arm` (job `arm64` of `ci`, and `platforms.yml`), `macos-26` (macOS arm64, `MAP_JIT`) and `windows-11-arm` (`platforms.yml`; labels per actions/runner-images) |
| python.org macOS | the macOS cells of `platforms.yml` run the python.org framework CPython (hardened runtime) that cibuildwheel installs, on Apple Silicon (`macos-26`) and Intel; the JIT tests and the spec suite pass there, and a refusal by the hardened runtime would show as a failed cell (RISKS R2). The separate `macos-pythonorg` job, with its entitlements printout, is gone |
| Android | the Android cell of `platforms.yml`: CPython's testbed app on a managed x86_64 emulator (cibuildwheel; `arm64_v8a` is built only), `python -m pytest tests --jit-bounds=guard,explicit` inside the app with every test that needs no wasmtime as an oracle (no wasmtime there; WAT comes from the WAT cache, below), among them `tests/jit/test_android.py` (Android only: the SELinux context, the probe and `auto`, guard faults through ART's libsigchain with a module written in binary, CoreMark's self-check; S0006 step 5); no arm64 test (RISKS R2). The `android` job and `tools/android_check.py` are gone |
| emulated | QEMU user-mode: in `ci` for `jit-aarch64` (BTI) and `jit-x86_64` (SSE2-only CPU); the optional `exotic` jobs of `platforms.yml` (ppc64le, s390x, riscv64, `pyc` only, one QEMU runner each, started from the Actions tab, non-blocking); a JIT for an architecture without native runners (riscv64, D48) as it appears |

JIT artifacts: run baseline artifacts on an emulated baseline CPU (e.g. QEMU `-cpu` without SSE4.1 /
with plain ARMv8.0) and check that a higher-level artifact is refused there; load artifacts with the C
reference loader as well as from Python; refuse artifacts with an unknown format or context ABI version.

A newer push to the same ref cancels a `ci.yml` or `coverage.yml` run in progress (`concurrency`);
`publish.yml` runs one release per ref and is never cancelled. Jobs of `.github/workflows/ci.yml`: `lint` (ruff, pyright, `check_docs.py`, `features.py --check`); `test`
(`pytest -q` on the seven Pythons, in the default bounds mode, `auto` (D44: guard pages on this Linux glibc x86-64
runner);
on 3.13 again with `WABOROS_JIT_BOUNDS=explicit`, so explicit checks stay covered, and the three `pyc`
test files with `WABOROS_PYC_FBITS=1` (D46: f32/f64 as int bit patterns, the default only on 32-bit x86);
`tools/stack_info.py` on
3.13, also in a musl container); `jit`
(`tests/jit` with `WABOROS_JIT_REQUIRE=1`; the aarch64 ABI, compiler, tail-call and SIMD tests
(`test_simd_abi.py`, `test_simd_aarch64.py`, in explicit mode: QEMU splits a straddling 16-byte store,
D45) and the spec suite of `jit-aarch64` (it covers `simd/` since the 5e2 claim) in both bounds modes, then
`test_guard.py` in guard mode, under QEMU with BTI enforced (its subprocess cases skip without binfmt); the x86 SIMD tests and the explicit `simd` spec run of
`jit-x86_64` under the SSE2-only `qemu64` CPU with `WABOROS_JIT_BOUNDS=explicit` (QEMU splits wide
stores, D45), compared with `pyc` by `tools/spec_diff.py` (strict since 5d3);
the spec suite of `jit-x86_64` and `tests/jit/test_guard.py` with `WABOROS_JIT_BOUNDS=guard` named
explicitly (guard pages, S0006; the same two in a `python:3.13-alpine` container for Linux musl
x86-64, `MUSL_GUARD`; the test file runs on the JIT of the machine and skips
on other hosts, but fails under
`WABOROS_JIT_REQUIRE=1` when `auto` does not give guard pages), so a regression in the
verified-row check cannot silently turn guard pages off;
then both ISA gates and the D35 lists of both targets in both bounds modes, named on both hosts);
`spec` (the spec run on `wasmtime`, `pyc` (also with `WABOROS_PYC_FBITS=1`), `jit-x86_64` in the default mode and with
`WABOROS_JIT_BOUNDS=explicit`, then `auto` under `no_exec.py`); `arm64`
(Linux arm64 only: the `jit-aarch64` spec suite in both bounds modes, `test_guard.py` in guard mode and
`tests/jit` in explicit mode, S0006 step 2).

**The WAT cache.** Tests build modules from WAT text through `tests/watcache.py` (`wat2wasm`), which
compiles with wasmtime's `wat2wasm`. Where wasmtime has no wheel (Android, Pyodide, Linux i686 and
armv7l, the optional ppc64le, s390x, riscv64) the same call reads `tests/wat_cache/<sha256 of the text>.wasm`,
and a text that is not there skips the test (fails under `WABOROS_JIT_REQUIRE=1`). Tests that run a
module in wasmtime as the oracle still skip there. The cache is not committed: `platforms.yml` fills it
on the host (which has wasmtime) before cibuildwheel by running the suite once with
`WABOROS_WAT_CACHE=1` (about a minute); Linux containers see it as `/project/tests/wat_cache`, Android
gets it inside `test-sources = ["tests", ...]`. Refill locally with
`WABOROS_WAT_CACHE=1 uv run pytest tests -q --ignore=tests/spec`; a changed WAT text is a new entry
(stale ones are harmless; delete the directory to drop them).

`.github/workflows/platforms.yml` (cibuildwheel; config `ci/harness/pyproject.toml`; the same triggers
as `ci`, its own concurrency group) is the only place where macOS, Windows, Android and Pyodide are
tested, and also runs the Linux wheels (glibc and musl x86_64, i686, aarch64, armv7l). It tests the
installed wheel, not `src/`: `pytest tests --jit-bounds=guard,explicit` on every cell (`auto,explicit` on Linux
aarch64), and once per JIT platform (Linux glibc and musl x86_64, Linux aarch64,
macOS, Windows, both architectures; one Python each) `ci/harness/spec.py`: the spec suite on `wasmtime`, `pyc`,
the JIT in guard mode and in explicit mode, and `auto` (S0006 steps 3 and 4: the guard rows of macOS and
Windows are verified there on every push). Left in `ci` because they need a source checkout or special
tools: the disassembly checks (llvm-mc, GNU objdump), QEMU, CoreMark, the D35 lists, MDWE.

`tests/jit/test_fault_handler.py` checks the emitted fault handlers off their OS: the Darwin and
Windows offsets against ctypes mirrors of xnu's and the Windows SDK's headers, the Darwin and Windows
handlers through llvm-mc (x86-64 also through GNU objdump; the Windows ones also for the registers they
write), and the handler of the machine called as a C function with fabricated Linux, Darwin and Windows
contexts (x86-64 off Windows through a SysV-to-Win64 thunk), so the macOS and Windows code paths run on
Linux too (aarch64 under QEMU). On Windows x64, `test_guard.py` also starts a child with user shadow
stacks forced on in strict mode and context-IP validation on (the CET question of S0006 step 4), twice:
with the dynamic CET APIs allowed in process it must print two guard traps (the resume addresses are
registered); as the system sets them, either two traps or, where they are out-of-process only, our refusal
of guard mode; the child is the base interpreter, not the venv launcher, and the test skips only
where `IsUserCetAvailableInEnvironment` says the machine has no user CET (with CET, a policy read back as off
fails).

Packaging workflows (D40): `.github/workflows/coverage.yml` runs `pytest --cov` on Python 3.13 and keeps
`coverage.xml` as an artifact and to Codecov (OIDC); `publish.yml` runs on a `v*` tag, builds the sdist and the wheel,
smoke-tests the wheel in a clean environment, and publishes to PyPI (trusted publishing) and as a GitHub
release. `.github/dependabot.yml` updates the `uv` lock file and the pinned actions weekly.

**SIMD is measured before it is claimed.** A directory the target does not claim is not run by the
default spec run and not in CI; the explicit run `python -m tests.spec.run --engine T simd` ignores
the claims and counts failures as failures. A target claims `simd` only when that run has at most 25
listed failures and, for a JIT, equals `pyc` (S0005, "Claiming `simd`"); until then CI watches the
progress only through the QEMU step above, which fails on a SIGILL, a crash or a difference from `pyc`.
`jit-x86_64` claims `simd` since step 5d3 and `jit-aarch64` since step 5e2 (0 failures each), so the
default spec run includes `simd/` for both: the `spec` job for `jit-x86_64`, the QEMU spec run of the
`jit` job and the native `arm64` job for `jit-aarch64`. The x86 `qemu64` step stays: it runs `simd/` on
another CPU model (SSE2 only); the aarch64 explicit step was dropped at 5e2, the claimed QEMU run covers it.

CoreMark on a JIT is checked with `tools/run_coremark_jit.py --check FILE` (the `arm64` job; in the
Android app `tests/jit/test_android.py` calls `valid` itself): a run is valid with "Correct operation validated", or when the only error is
CoreMark's "at least 10 secs" rule and the CRCs are the 2K-performance values (a correct run on a
slow runner took 9.6 s). The interrupt-latency test (`tests/jit/test_abi.py`) allows 1 s, not 0.05 s,
for loaded runners.

The `jit` job runs `tools/isa_all.py` (747 blobs at SIMD step 5d3; the count grows with the code): it compiles every
spec module of **every directory, claimed or not** (so `simd/` too), and CoreMark, through `tools/jit_dump.py` with
`WABOROS_JIT_DUMP` and disassembles each blob with `tools/isa_check.py` (code ranges only), failing on any
instruction above x86-64-v1 (D24). `isa_all.py --target jit-aarch64` does the same for aarch64 code,
compiled (never run) on any host: `isa_check.py --arch aarch64` disassembles with `llvm-mc` and allows
ARMv8.0-A and the `bti` HINTs only (tests: `tests/jit/test_isa_aarch64.py`, planted LSE/CRC/SVE/fp16/
HINT words must fail).

Cross-compilation (D35): `tools/jit_dump.py --target T OUT` writes a sha256 list of the code of every
spec module and CoreMark. The `jit` job uploads its lists for both targets (artifact `jit-dump-x86`);
the `arm64` job (Linux) compiles the same modules and `diff`s its lists with them, so code compiled on
x86-64 and on arm64 is the same bytes both ways.

The `spec` job also runs `--engine auto` with executable memory forbidden: `tools/no_exec.py` sets
`PR_SET_MDWE` (`PR_MDWE_REFUSE_EXEC_GAIN`, Linux >= 6.3; the kernel then refuses `mprotect(PROT_EXEC)` on
written memory, the JIT's W^X path) and `exec`s the runner. `auto` must warn once
(`JitUnavailableWarning`) and give `pyc`'s numbers. `tests/test_fallback.py` does the same in a subprocess.
