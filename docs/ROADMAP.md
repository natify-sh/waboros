---
title: Roadmap
kind: reference
status: current
updated: 2026-11-02
---

# Roadmap

Phases in order; each ends with tests green for what it claims. Numbers are measured, never estimated
(BENCHMARKS.md).

## Where to start

The first goal is not a feature but a **measurement**: every later change should move a number.

1. **Skeleton** (phase 0): the repository under natify-sh with the working name `waboros` (renaming later is
   cheap); `pyproject.toml` with no runtime dependencies and a development group — `wasmtime` (the
   oracle), `pytest`, `wabt-package` (for `wast2json`); CI on CPython 3.11–3.15, 3.15t and PyPy 3.11, on
   Linux x86-64. Minimum Python 3.11 (D19).
2. **The spec-suite runner** (phase 1, the most important step): convert `WebAssembly/spec` at a pinned
   commit with `wast2json`; a runner for the commands (`module`, `assert_return`, `assert_trap`,
   `assert_invalid`, …) over a **tiny internal engine interface** — load a module, instantiate, call an
   export, read a global — not over a wasmtime-like public API (that is phase 6).
   **Run it against wasmtime first**: this tests the runner itself, and the oracle should pass nearly
   everything.
3. **Port the PoC front end** into `frontend/` unchanged (reader and decoder from `poc/w2py.py`), connect
   the PoC's `pyc` code generator as it is, and record the **first number**: how many spec commands
   pass. The per-target expected-failures file (TESTING.md) is the task list from then on.
4. **Loop**: take the largest group of failures, fix it, watch the number grow. The validator, block
   parameters, tables, trap conversion come in the order the failures dictate.

Why this order: the spec suite plus the oracle give an objective metric from day one; `pyc` is the
simplest target and the one every other must match, so semantics are pinned there; the JIT (phases
3–5) then only reproduces checked semantics faster, and the same runner shows at once where it
deviates. The JIT comes **before** the public runtime API (phase 6): the compiler is the point, the API
only wraps it. Inside the JIT the backend interface is fixed on **two** architectures (phases 3–4)
before the opcode set is widened (phase 5): an interface proved on x86-64 alone is an x86-64 interface.
The open questions below do not block this start — the name, the WASI host and parallelism are needed
only in phases 2 and 6.

## Scope and order of the wasm features

**The target is WebAssembly Core Specification 3.0** (TESTING.md): besides 2.0 (mutable globals,
sign-extension, non-trapping float-to-int, multi-value, bulk memory, reference types, fixed-width SIMD)
it contains GC, typed function references, exception handling (`exnref`), tail calls, memory64,
multi-memory, relaxed SIMD and extended constant expressions. Not in 3.0, and so tracked as separate
proposals: threads, wide arithmetic, custom page sizes (Wasmtime Tier 2), stack switching and the
other Tier 3 ones.

The order below is **staging, not scope**: it follows what the modules that exist in practice need
(what clang, rustc and zig emit by default comes first) and what fused WASI 0.2 needs. Everything in
3.0 is in scope; the stages say only what is implemented, and measured against the spec tests, first.

1. **MVP + the 2.0 features** — what toolchains emit by default.
2. **multi-memory** — without it there is no fused WASI 0.2. Part of 3.0.
3. The runtime API and **WASI 0.1**, then **fused WASI 0.2**.
4. **memory64**, **tail calls**, **extended-const**.
5. **exception handling**.
6. **function references** and **GC** — needed by Kotlin, Dart, OCaml, Java-to-wasm; less by C/Rust.
7. **relaxed SIMD**.
8. Outside 3.0: **wide arithmetic**, **threads** (shared memory, atomics), **custom page sizes**.

Tier 3 proposals (branch-hinting, stack-switching, compact-imports) after all of these. Sources and
counters: TESTING.md, "Sources of truth and the completeness counters".

**Staging must not narrow the design.** The 3.0 features change the shapes that every layer shares, and
those are expensive to retrofit: so the front end, the validator and HIR are designed for all of 3.0
from phase 1 even where a target does not implement a feature yet — a memory index on every memory
operation (multi-memory), 64-bit addresses and limits (memory64), the full value-type lattice with
subtyping and reference types (GC, function references), tags and `try_table` as structured control
(exceptions), `return_call*` as terminators (tail calls), multi-value block parameters. A target that
lacks a feature reports it through `Target.supports` and the module runs on another target.

## Exit criteria in numbers

| phase | counters (TESTING.md) |
|---|---|
| 1 | `assert_invalid` / `assert_malformed` of the pinned spec suite pass (the validator) |
| 2 | the `core` group (`test/core/*.wast`, the 2.0 features plus typed function references and tail calls) passes on `pyc` (CPython and PyPy), plus the `bulk-memory` directory; the rest are listed expected failures (`pyc`: 20509 pass in `core`, 89 expected failures) |
| 3 | the `core` group passes on `jit-x86_64` exactly as on `pyc` (met: the same 20509 pass and the same 89 expected failures; the scope went past the integer core, D25); the backend interface is written down and the PoC's x86-64 code is split into the shared core and the backend (S0002, S0003; step 6 review done); CoreMark not worse than the PoC (met: 17 526 vs `w2jit2` 17 662 it/s, 99.2 %, BENCHMARKS.md) |
| 4 | the same integer-core spec commands pass on the second architecture (aarch64 on native arm64 CI runners); the size of its backend is recorded; the interface is changed where it leaked x86-64 and then frozen |
| 5 | the same spec commands pass on every JIT target as on `pyc` for the stages it claims (`simd/` is claimed by `pyc` since 5b; the JITs claim it at 5d3 and 5e2, S0005); differential fuzzing finds no divergence over an agreed number of hours |
| 6 | the public API is covered by tests of its own plus differential runs against wasmtime (its API used as the oracle, not as a goal); the WASI testsuite (p1) passes; the CLI runs the commands of API.md |
| later | each new proposal enters with its spec tests green on every target that claims it |

## Phase 0 — repository from the PoC
*Status: done (branch `phase0-skeleton`, merged to `main` on 2026-11-02 with phases 1-4 and SIMD; 0.0.0 published).*

* Package layout of ARCHITECTURE.md; move the PoC's reader, decoder and stack-to-tree builder out of
  `w2py.py` into `frontend/` and `ir/`; the code generators become `targets/pyc` and `targets/jit/x86_64`.
* `pyproject.toml` with **no** runtime dependencies and a development group (wasmtime pinned as the
  oracle, pywasm, pytest; wabt / wasm-tools / meld as external tools).
* CI skeleton: CPython 3.11–3.15 + 3.15t + PyPy 3.11 on Linux x86-64.

## Phase 1 — frontend complete
*Status: done: strict binary reader, validator, spec-suite runner, HIR.*

* Full binary reader for every proposal Wasmtime supports (FEATURES.md); the validator (type checking
  of function bodies); constant expressions (extended const).
* Spec-suite harness over the tiny internal engine interface (TESTING.md) running `assert_invalid` / `assert_malformed`.
* HIR (ARCHITECTURE.md, "The IR"): trees, structured control, the analyses (bounds/wrap, local
  weights, effect order, trap points, call graph); documented in code.

## Phase 2 — `pyc` target complete
*Status: done for the core group (spec core: `pyc` 20509 pass, 89 expected failures, same as `jit-x86_64`; the `bulk-memory` directory 7482 of 7485 also passes on `pyc`); frozen for new features except the proposals that land on `pyc` and the JITs together (multi-memory, D37; SIMD, S0005), correctness fixes only.*

* All core features of FEATURES.md, SIMD correct before fast (done in phase 5, step 5b, S0005); NaN rules settled against the oracle.
* Trap conversion at the boundary; stack-depth counter and `STACK_OVERFLOW`; re-entrancy.
* Long calls: yield points + interrupt word (measure the cost); one-thread-per-store guard.
* Fuel and epochs (compiled in on demand).
* The compilation cache (DECISIONS.md D9); the public `Module.serialize`/`deserialize` and thin/fat
  files (D18) wait for phase 6.
* No embedded runtime: generated code uses the package's runtime (D18); the PoC's emitted `RUNTIME`
  goes away. Regeneration from the wasm on
  any mismatch.
* Exit criterion: the spec suite passes on CPython and PyPy.

## Phase 3 — `jit-x86_64`: integer core and the backend interface
*Status: done (2026-11-01; S0002 and S0003 implemented). Done: 3a ABI, 3b integer core, 3c SSE2 floats and `memory.grow`, 3d tables, `call_indirect`, reference types, bulk memory, 3e tail calls, typed function references and tags (validator), 3f function imports through records (D32), step 4 fallback to `pyc` with `JitUnavailableWarning` (D31), step 5 ISA gate in CI and the CoreMark checkpoint (17 526 it/s vs `w2jit2` 17 662, 99.2 %). Spec core: 20509 pass, the same expected failures as `pyc`; `auto` selects the JIT. After phase 3: multi-memory on both targets (912/912, D37). Step 6: the final review, with its fixes (forged references, code areas unmapped, host-call status in context word 7, D34). The scope went beyond the integer core (floats, tables) because the goal is completeness (D25).*

**Done in phase 3 beyond the original list below:** floats, `call_indirect` and tables, `memory.grow` on a 4 GiB reservation committed on grow, bulk memory, reference types, multi-value, tail calls, typed function references.

* **Order of work (2026-10-30):** `pyc` has closed the core group and is frozen at that feature set
  (plus typed function references and tail calls); it only gets correctness fixes until the JIT catches up. **Multi-memory is the first proposal after that, on both
  targets together** (done 2026-11-01, D37: fused WASI 0.2 needs it, D7). S0003's context already
  indexed memories; the `memory.grow`/`memory.init` libcalls gained a memory-index immediate for
  modules with several memories (an amendment of context ABI v1, D37). New
  proposals (SIMD, GC, exceptions, memory64, threads) land on `pyc`
  and the JIT together, on the JIT first, or (SIMD, the maintainer's order in S0005) on `pyc` first, so the gap between their expected-failure files stays
  small and is reported per directory on every run (D25).
* **Decided (D26): one pass over the validated body driving a backend interface.** The options were: LIR with
  virtual registers and an allocator (Cranelift style), or one pass over HIR driving a small
  `MacroAssembler`-like backend interface (Winch style; closest to what `w2jit2` already does with its
  lazy value stack, pinned locals and fused compare+branch). Judge by compile time in Python
  (BENCHMARKS.md, "Compile time") as well as by code speed.
* Split `w2jit2` along ARCHITECTURE.md / TARGETS.md: shared core (executable memory, entry and host
  call bridge, value-stack frames, labels and fixups, trap stubs, interrupt and fuel polling, nested
  entries) and the x86-64 backend (encoder, ABI, register pools, instruction selection). Two axes
  stay separate from the start: the ISA and the OS ABI (System V now, Win64 later).
* Stage 1 of TARGETS.md was the plan: i32/i64, locals, globals, control flow, calls, memory with **explicit**
  bounds checks, traps (done, then widened as the status line says). Guard pages are an optional capability of a backend and come in phase 5 (D29).
* Mechanisms every architecture will need are built and tested here, not later: traps unwound by
  returning through frames (CET-safe, RISKS.md R2), re-entrant entry, host exceptions across the
  boundary, interrupt word, stack limits, the executable-memory probe and the engine-level fallback to `pyc` when the probe fails (D25).
* Runtime services (`memory.grow`, `table.grow`, segment initialisation) are called through the host
  bridge, as Wasmtime's libcalls; computation is never handed to a Python helper (D25): what a backend
  cannot compile yet is `unsupported`, and the xfail difference to `pyc` measures completeness.
* The encoder is checked in CI against an external assembler (`llvm-mc` / `objdump`) for every
  instruction form it can emit.
* **Baseline only** (D24): x86-64-v1 code; `f64.trunc`/`f32.trunc` get the SSE2 sequence, with the
  PoC's `roundsd` kept as an optional x86-64-v2 variant behind its declared level; later
  `clz`/`ctz`/`popcnt`, `floor`/`ceil`/`nearest` start as baseline sequences too (`bsr`/`bsf` with the
  zero case, SSE2 rounding). CI disassembles generated code and fails on any instruction above the
  declared level.

## Phase 4 — the second architecture (integer core) proves the interface
*Status: done. 4a encoder, trampolines and executable memory; 4b integer-core backend; 4c ISA gate, spec, cross-compilation check and CI on arm64; 4d review and the interface freeze (D39); 4e parity: `jit-aarch64` fails exactly what `pyc` and `jit-x86_64` fail (28903 pass) and `auto` selects it on arm64; CoreMark passes its self-check (speed from the arm64 runners in BENCHMARKS.md).*

* `jit-aarch64`, tested natively on GitHub's arm64 runners (Linux, macOS arm64 with `MAP_JIT`, Windows on Arm), first the integer
  core (then parity, step 4e): the same stage 1, the same spec commands as phase 3. QEMU user-mode is only for
  architectures without native runners (riscv64 and later ones).
* Record the size of the new backend and what in the shared core or the interface had to change; fix the
  places where x86-64 leaked (flags, two-operand forms, fixed registers for division and shifts) and
  **freeze the interface**. Nothing in phase 5 starts before this.
* Cross-compilation check (D35): the aarch64 code of the spec modules and CoreMark compiled on the
  x86-64 runner is byte-identical to the code compiled on the arm64 runner, and runs there.
* After it, decide how further architectures are written, with numbers: a backend in Python,
  copy-and-patch, or a micro-compiler in wasm (open question 14; the maintainer's candidate for the
  third architecture).
* aarch64 on macOS (`MAP_JIT` + `pthread_jit_write_protect_np` via ctypes) and Windows on ARM are in
  this phase too (maintainer's decision, S0004).
* After step 4e (maintainer, 2026-11-01): macOS arm64 coverage — (1) the spec suite in the
  `macos-latest` CI job, not only `tests/jit`; (2) a CI check with the python.org macOS installer,
  whose hardened runtime may forbid `MAP_JIT` without the JIT entitlement: either the JIT runs there
  or `auto` falls back to `pyc` with the D31 warning and the reason is recorded (RISKS R2).
  Status: done. (1) The macOS arm64 job runs the spec suite and `auto` (4e). (2) The python.org
  3.13.17 CPython runs the JIT: its hardened runtime carries `disable-executable-page-protection`
  (CI job `macos-pythonorg`, RISKS R2).

## Phase 5 — JIT completion
*Status: in progress (2026-11-02), SIMD done: step by step ([S0005](specs/S0005-simd.md), approved, D41). Done: 5a front end (decode and validate v128); 5b `pyc` runs every operator and claims `simd/` (25 990 commands, 0 failures); 5c1 v128 in the JIT core (two slots per v128, `Layout.wide`, the `simd` method: the D39 amendment) and 5c2 every data-movement operator, on both JITs; 5d1 and 5d2 the class-D and class-S operators on `jit-x86_64` with SSE2 only; 5d3 the 10 class-L operators (`shuffle`, `swizzle`, rounding), so `jit-x86_64` compiles all 236 and claims `simd/` at parity with `pyc` (25 481 runnable commands pass; the claimed run 54 385 pass, 91 expected failures, as `pyc`); 5e1 the 138 integer operators on `jit-aarch64` in ARMv8.0-A NEON; 5e2 the other 54 (float arithmetic, compares, conversions, rounding, `shuffle`, `swizzle`), so `jit-aarch64` also compiles all 236 and claims `simd/` at parity with `pyc` (its claimed run under QEMU 54 385 pass, 91 expected failures, as `pyc`). Both JITs claim `simd`. SIMD is complete and correct on all targets; the SIMD benchmark is done (BENCHMARKS.md). The speed fix is done (2026-11-02, the D39 amendment `move_v128`: v128 locals, constants, `select` and splats move as one 16-byte access on both JITs, so store-to-load forwarding works; `jit-x86_64` matmul 12.10 → 2.17 µs and scan 3.81 → 0.64, both now faster than their scalar builds; BENCHMARKS.md). 5f, the review of 5c–5e, found no correctness defect: SIMD is done (5a–5f). Next, after the maintainer's merge to main and publishing: guard pages (D29), then relaxed SIMD, memory64, exceptions, GC, the artifact format with the container question (ARTIFACT-CONTAINER.md).*

Order (maintainer, 2026-11-01): SIMD (S0005) -> the rename to `waboros` with the packaging for publishing (LICENSE,
setuptools + setuptools-scm, coverage/publish workflows, dependabot; D40)
as its own step (D40, done 2026-11-02) -> guard pages (D29) -> the rest below.

* What remains of the opcode set, in stages (TARGETS.md), on **every** JIT target in step:
  SIMD (under way, above), memory64, relaxed SIMD, exceptions, GC, then the later proposals. What a backend lacks is
  `unsupported` until it is written (D25). Floats, tables, `memory.grow`, bulk memory, reference types,
  multi-value and tail calls were done in phase 3.
* **Guard pages with a JIT-emitted signal handler** (`poc/w2jit_guard.py`: +20–25 % on CoreMark, the gap
  to Cranelift was mostly the bounds checks), per OS and architecture (Linux/BSD `sigaction` +
  `ucontext` offsets, macOS also SIGBUS, Windows a vectored exception handler), `sigaltstack` per thread
  for stack overflow; the default where the handler is verified, explicit checks the fallback and the
  only mode of a new architecture (D29); bounds-check elimination where guard pages are not available.
* Trap frames, fuel and epoch checks (the interrupt word is polled already, phase 3); Ctrl+C via the
  waiting main thread.
* The contract of ARTIFACT.md (format v1, context ABI v1, baseline-ISA sequences, CPU feature
  detection) is **fixed here**, after two architectures have shaped it, together with artifact
  serialization; the C reference loader runs in CI and the runtime services are specified (D36). The
  container (our own or ELF64, [ARTIFACT-CONTAINER.md](ARTIFACT-CONTAINER.md)) is decided in that spec first.
* Win64 hardening (the second entry trampoline and `VirtualAlloc` exist; CI runs `tests/jit` on
  `windows-latest` and `macos-26-intel` and both are green, TARGETS.md; Windows ACG is open, RISKS R2);
  macOS arm64 and Windows on ARM are done in phase 4.
* Differential fuzzing against wasmtime (`wasm-smith`) from the first widened stage on.

## Phase 6 — runtime API, WASI, CLI
*Status: not started.*

* `Engine`, `Config`, `Store`, `Module`, `Linker`, `Instance`, externs, `Caller`, `Trap`/`TrapCode`,
  limits, in the shape of wasmtime-py (not a drop-in), over the internal engine interface the spec
  runner has used since phase 1.
* Choose the WASI host (open question 2); WASI 0.1 and fused 0.2 through it (needs multi-memory, planned right after
  phase 3 on both targets).
* `Module.serialize`/`deserialize` and the thin/fat files (D18) on top of the cache of phase 2 and the
  artifacts of phase 5.
* CLI: `run`, `compile`, `wast`, `settings`/`targets`, `cache`.
* Differential tests against wasmtime on real programs.

## Phase 7 — `mpy` target
*Status: not started.*

* `.mpy` ahead of time and on-device source; `@native` where it helps; avoid the `mpy-cross` large
  function bug (LESSONS.md 17).
* Research: lower HIR to `@micropython.viper` code (machine-word ints, `ptr8/16/32` memory) so that
  MicroPython's own emitters become native back ends on every MCU architecture its ports support
  (Thumb, Xtensa, RISC-V, …) without an encoder of ours; inline assemblers (`asm_thumb`, `asm_xtensa`)
  for the hottest functions (TARGETS.md, "MicroPython's own native emitters as JIT back ends").
* Port the translator to run on MicroPython if on-device compilation is wanted.

## Phase 8 — more architectures
*Status: not started.*

* armv7, mips32el (Ingenic X2000), ppc64le, loongarch64 and whichever of aarch64 / riscv64 phase 4 did
  not take — by hand or by copy-and-patch, CI under QEMU user-mode. Each is added by a backend, the
  encoder test and the integer-core spec commands; the shared core does not change.

## Open questions
1. **Minimum Python.** Resolved by D19: 3.11 (the PoC's 3.8 and 3.4 `--legacy` measurements stay as
   history).
2. **The WASI host.** fusedwasi's Python twin (candidate: 0.1.0a1 is runtime-independent and runs on
   wasmtime, wasm3 and wasmext, D8; not tried with waboros yet),
   an own implementation, or another; and what WASI means on MicroPython (open question 6).
3. **Parallelism of `pyc` guests**: yield points only, or also subinterpreters (3.12+/3.14),
   free-threaded builds, a process with shared memory (ARCHITECTURE.md, "Long calls and the GIL")?
4. **Wasmtime options with no meaning here** (`cranelift_*`, `profiler`, `signals_based_traps`, …):
   accept and ignore, or reject?
5. **NaN payloads in `pyc`**: Python floats keep the payload in most paths but not all (f32 rounding
   through `struct`); follow the spec's canonical/arithmetic NaN rules only, or carry bit patterns?
6. **WASI on MicroPython**: a slim twin, or fusedwasi compiled to wasm and translated by waboros itself
   (then each language/target needs only a thin layer of OS primitives)?
7. **Pyodide**: `pyc` runs there today; a separate target later (maintainer). The experiment in
   [PYODIDE.md](PYODIDE.md): the guest rewritten into an Emscripten side module and loaded with
   `ctypes.CDLL` runs CoreMark at 92–95 % of direct `WebAssembly` in Node, sharing Pyodide's heap;
   open: isolation (bounds checks), traps, grow, tables, D25 parity, browsers.
8. **Free-threaded CPython (no GIL)**: shared memories and atomics in `pyc` need real synchronisation.
9. **Name** — settled by D40: the project is `waboros`; the rename was done on 2026-11-02.
10. **Version skew** — settled by D18: no embedded runtime, exact match or regenerate; fat files carry
   the wasm. Left: the known-bad compiler list (where it lives, how it is updated).
11. **Components on the fly**: accept a component and fuse it at load time (then compile and cache the
   core module by the component's hash) by running **meld compiled to wasm on waboros itself**, shipped
   as a data file in the wheel (Apache-2.0): no native dependency and no second fuser to keep in sync.
   **Checked (meld 0.66.0, BENCHMARKS.md):** meld builds without patches for `wasm32-wasip1` (CLI,
   2.7 MB, 16 WASI p1 imports), `wasm32-wasip2` (component, 2.9 MB) and — best for this use —
   **meld-core alone for `wasm32-unknown-unknown`** behind a ~40-line wrapper (1.7 MB, **no imports at
   all**: `meld_alloc` / `meld_fuse` / `meld_out_ptr` / `meld_out_len`; attestation set to
   `reproducible`, there is no wall clock without WASI). It uses only MVP + sign-ext + `memory.copy` /
   `memory.fill` (no SIMD, no multi-value, no `trunc_sat`), and **the PoC's `pyc` already runs it**:
   meld fusing its own p2 build gives output byte-identical to the same run on wasmtime, in 44 s on
   PyPy and 253 s on CPython (wasmtime 1.05 s) — acceptable once per component with the cache, and the
   JIT will cut it. Still open: its coverage (resources, string transcoding, p3 async). Exports remain
   core functions; Python values need our own canonical-ABI lift at the boundary.
   Alternative without fusion (a component front end of our own as a wasm module, Wasmtime/jco model):
   [COMPONENTS.md](COMPONENTS.md) (draft); meld stays the plan.
12. **Targets in one Store**: one shared representation of memories, tables and globals with
   per-module fallback, or one target per Store (ARCHITECTURE.md, "State shared across targets")?
   Settled by D25: one target per engine and store. Left: `pyc` on a reserved `mmap` against a
   `bytearray`, to be decided by a measurement.
13. **Lazy compilation**: compile each function on its first call (JIT: a stub that compiles and
   patches; `pyc`: translate and `exec` per function) so the first load costs what is used, not the
   whole module (QuickJS: ~1.5 s JIT / ~5.7 s `pyc` for everything; BENCHMARKS.md, "Compile time on
   large modules"). Costs: a patchable call path, compilation in the middle of a call (re-entrancy,
   the GIL, executable-memory growth). Compare with eager compilation + cache before deciding.
14. **A self-hosting compiler in wasm, one module per architecture (research, after phases 4–6).**
   Shipping them pre-compiled: [MICRO-COMPILERS-SHIPPING.md](MICRO-COMPILERS-SHIPPING.md) (draft).
   The full note is [MICRO-COMPILERS.md](MICRO-COMPILERS.md) (maintainer).
   The maintainer's idea: our own compiler written in a language that compiles to wasm, shipped as one
   module per architecture — `x64.wasm`, `arm64.wasm`, … (or a shared `core.wasm` plus a backend
   module per architecture, linked by imports). Each runs on any wasm runtime (`pyc` first), compiles
   itself into a native artifact for its architecture, then compiles everything else natively; run on
   another architecture it is a cross-compiler (D35). It emits our context ABI directly (S0003,
   D30–D37), stays small (the core and one backend), and a host in another language needs only a
   loader and the runtime services to compile as well as run (D36). Our Python JIT stays the
   reference: the wasm compiler's output must be byte-identical to the Python compiler's on every spec
   module and CoreMark, a strong and cheap correctness check. Timing: after phase 4 freezes the
   backend interface (a stable design to port, not a moving one), with a fixed artifact format
   (phase 5); WASI (phase 6) only if the compiler needs it. **First micro-compiler (maintainer,
   2026-11-02): `arm64.wasm`**, a port of the existing aarch64 backend (the smaller one): the Python
   `jit-aarch64` is a ready oracle, so every spec module and CoreMark is checked byte for byte, which
   validates the ported core and the toolchain before anything new is written. **Then the third
   architecture**: riscv64 (phase 8) as a micro-compiler on the proven core instead of a third Python
   backend, checked by the spec suite under QEMU against `pyc` and Wasmtime (no byte oracle there).
   **How a micro-compiler starts on a machine** (`pyc` runs it; it never compiles): (1) bootstrap, once
   per machine: `pyc` runs `riscv64.wasm` as an ordinary wasm program and feeds it its own wasm; it
   emits a native artifact of itself — slow (`pyc` is ~150× slower than native code) but done once;
   (2) cache: that artifact is kept like any cached artifact, keyed by waboros version, architecture and
   ISA level; (3) from then on the runtime loads the native micro-compiler, which compiles every other
   module in milliseconds. Short path where a Python backend exists (x86-64, aarch64): our JIT compiles
   `x64.wasm` to native directly, no `pyc` step; the long path is only for architectures without a
   Python backend. Cross variant (D35): on an x86-64 machine our JIT runs `riscv64.wasm` and emits a
   native riscv64 artifact of the compiler, so a slow device can receive a ready compiler and skip
   the bootstrap. **The native micro-compiler is itself an ordinary JIT artifact**, so by D36 a host
   in another language loads it with its loader and runtime services and compiles any wasm with no
   Python and no bootstrap; the bootstrap is only how the artifact is first obtained or checked (a
   shipped one is verified byte-identical with a self-built one, D35). Needs: the fixed artifact format
   (phase 5); a small versioned contract of the compiler module itself (e.g. `compile(in_ptr, in_len)`
   returning the artifact in its memory: no WASI, no file system); one compiler artifact per
   architecture and ISA level. Cost: the compiler written again in Rust,
   Zig or C, and harder debugging than Python. waboros stays a compiler in Python (CLAUDE.md, BRIEF.md):
   it remains the bootstrap, the reference and the path for MicroPython and small devices.
   Alternative considered, also open: run an existing compiler built for `wasm32-wasi` (Winch or
   Cranelift) on waboros — an optimising tier without writing one, but several MB, its output has to be
   adapted to our ABI, and it needs WASI. Experiments: for either, the module size, the first run on
   `pyc`, the self-compilation time, memory, and code quality on CoreMark against the Python JIT. A
   shipped pre-compiled compiler artifact needs a reproducible build (byte-identical with a self-built
   one, D35) to keep the package auditable.
