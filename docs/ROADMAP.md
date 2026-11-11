---
title: Roadmap
kind: reference
status: current
updated: 2026-11-05
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
   For threads: JIT atomics on aarch64 need `ldaxr`/`stlxr` loops at the ARMv8.0 baseline (LSE is an
   ISA variant above it, D42); a shared memory must never move on `grow` (the guard-page reservation
   of 4 GiB + 32 MiB gives that, to be checked with shared memories); CLAUDE.md's "one thread per
   store" rule has to be revisited, since several threads use one shared memory.

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
| 2 | the `core` group (`test/core/*.wast`, the 2.0 features plus typed function references and tail calls) passes on `pyc` (CPython and PyPy), plus the `bulk-memory` directory; the rest are listed expected failures (`pyc`: 20532 pass in `core`, 66 expected failures) |
| 3 | the `core` group passes on `jit-x86_64` exactly as on `pyc` (met: the same 20509 pass and the same 89 expected failures on 2026-11-01, 20532 and 66 since exceptions; the scope went past the integer core, D25); the backend interface is written down and the PoC's x86-64 code is split into the shared core and the backend (S0002, S0003; step 6 review done); CoreMark not worse than the PoC (met: 17 526 vs `w2jit2` 17 662 it/s, 99.2 %, BENCHMARKS.md) |
| 4 | the same integer-core spec commands pass on the second architecture (aarch64 on native arm64 CI runners); the size of its backend is recorded; the interface is changed where it leaked x86-64 and then frozen |
| 5 | the same spec commands pass on every JIT target as on `pyc` for the stages it claims (`simd/` is claimed by `pyc` since 5b; the JITs claim it at 5d3 and 5e2, S0005); every divergence found by the nightly differential fuzzing is triaged: fixed, or recorded as a known deviation (maintainer, 2026-11-04) |
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
*Status: done for the core group (spec core: `pyc` 20532 pass, 66 expected failures, same as `jit-x86_64`; the `bulk-memory` directory 7483 of 7485 also passes on `pyc`, 2 expected failures); frozen for new features except the proposals that land on `pyc` and the JITs together (multi-memory, D37; SIMD, S0005; relaxed SIMD and wide arithmetic, S0008; memory64, S0009), correctness fixes only; one exception for speed: S0010, the `_br` ladders (maintainer, 2026-11-04; step 0 done 2026-11-05: the experiment's pass gives CoreMark +11-13 % on CPython 3.11 and 3.13, +7 % on 3.14, none on PyPy, the switch VM ×2.2-2.5 on CPython, sink carrying the CoreMark gain; spec, corpus and fuzz unchanged; BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 0)"; implemented 2026-11-05: sink at emission, CoreMark +9-10 % on 3.11/3.13, +6 % on 3.14, switch VM ×2.5).*

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
  (the former CI job `macos-pythonorg`, RISKS R2; now the macOS cells of `platforms.yml`).

## Phase 5 — JIT completion
*Status: in progress (2026-11-05). Done: SIMD (5a-5f) and the rename to `waboros` (D40); guard pages (S0006 complete); platform tests through cibuildwheel; the `pyc` bits mode (D46); S0007 (implemented: steps 1 and 3, step 2 reverted); S0008 (relaxed SIMD and wide arithmetic, implemented); the reconnaissance (3b below: done, unify through [S0011](specs/S0011-jit-low-layer.md)); S0011 (implemented 2026-11-05, D53: the JIT memory semantics written once over a per-architecture low layer); S0010 (`pyc`, implemented 2026-11-05: sink at emission; CoreMark +9-10 % on 3.11/3.13, +6 % on 3.14; switch VM x2.5); memory64 is done (S0009 implemented: `pyc` and both JITs claim `memory64`); exceptions with `exnref` (S0012, implemented 2026-11-05, D52: `pyc` and both JITs claim `exceptions/`, 100 pass / 8 expected failures that need GC types; the fuzz findings triaged and fixed). Open: GC, threads and the artifact format (below).*

* **SIMD** ([S0005](specs/S0005-simd.md), approved, D41), done: 5a front end (decode and validate v128); 5b `pyc` runs every operator and claims `simd/` (25 990 commands, 0 failures); 5c1 v128 in the JIT core (two slots per v128, `Layout.wide`, the `simd` method: the D39 amendment) and 5c2 every data-movement operator, on both JITs; 5d1-5d3 `jit-x86_64` compiles all 236 operators with SSE2 only and claims `simd/` at parity with `pyc` (the claimed run: 54 385 pass, 91 expected failures); 5e1-5e2 `jit-aarch64` compiles all 236 in ARMv8.0-A NEON and claims `simd/` at parity (QEMU run: the same numbers); the SIMD benchmark (BENCHMARKS.md); the speed fix of 2026-11-02 (the D39 amendment `move_v128`: v128 locals, constants, `select` and splats move as one 16-byte access, so store-to-load forwarding works; `jit-x86_64` matmul 12.10 -> 2.17 µs, scan 3.81 -> 0.64); 5f, the review of 5c-5e, found no correctness defect.
* **Guard pages** ([S0006](specs/S0006-guard-pages.md), D44, D45), complete: steps 1-5 and 7 done, step 6 (the artifact) moved to the artifact-format spec. `auto` gives guard pages on Linux glibc x86-64 and aarch64, Linux musl x86-64, macOS x86-64 and arm64, Windows x64 and arm64 and Android x86-64 (verified by CI runs 41610392553, 41633456821, 41640149866 and 41708608127); explicit checks elsewhere (Linux musl aarch64, which needs libgcc's `__clear_cache`; Android arm64); `WABOROS_JIT_BOUNDS=explicit` opts out. Stores rely on guard pages as in Wasmtime (D45: a store split by an emulator may tear; checking them was measured and rejected). CoreMark, guard against explicit checks: x86-64 +9.35 %, native aarch64 +11.4 % (BENCHMARKS.md). Per-OS work grows by exception only (D47).
* **Platform tests through cibuildwheel**, done (below): `platforms.yml` and `ci/harness/` test the installed wheel on every platform of the map in TARGETS.md that is on; `ci.yml` keeps what needs a source checkout.
* **`pyc` bits mode** (D46), done (below): f32/f64 as int bit patterns, the default on 32-bit x86; i686 CPython 3.13 has 0 spec failures.
* **Scalar code generation** ([S0007](specs/S0007-scalar-codegen.md), implemented 2026-11-04): step 1 done (results straight to slots 0..m on return; calls benchmark `fib` -19 %, `leaf` -4 to -6 %, within the run spread); step 2 (add/sub as one `lea` on x86-64) reverted on 2026-11-04: under the placement control it gave no kernel gain once step 3 was in, and CoreMark's +1.3 % was within the spread (BENCHMARKS.md); step 3 kept (`local + const` as one operand on both JITs; scalar `addsat` -18 %, `scan` -9 %, `nearest` -11 %; CoreMark +0.9 %, within the spread, so its acceptance item is missed: kept on the kernels' gain by the orchestrator). Step 1b (a block that ends the function writes its results to slots 0..m) was measured and dropped (correct, but `fib` not faster beyond code placement, BENCHMARKS.md); steps 5 and 7 were measured and dropped (BENCHMARKS.md); steps 4 and 6 are not done (maintainer's decision); step 8, the review, found no correctness defect.
* **Next** (maintainer, 2026-11-03), in this order: (1) S0007 to the end, done (step 5, once-used parameters: measured and dropped, BENCHMARKS.md; step 7, R: measured and dropped, BENCHMARKS.md; step 2, L: reverted after re-measurement; steps 6 and 4 (Z): not done, maintainer's decision; step 8, the review: no defect); (2) relaxed SIMD and wide arithmetic as one small parity step, done ([S0008](specs/S0008-relaxed-simd-wide-arithmetic.md), implemented 2026-11-04, D49; `pyc` and both JITs claim both; the step-4 review found no correctness defect); (3) memory64 (with `table64`), before exceptions ([S0009](specs/S0009-memory64.md): step 1 done 2026-11-04, `pyc` claims `memory64`; JIT steps 2a, 2b and 3 done, both JITs claim `memory64`; step 4, `spectest.table64`, done: implemented, 8 658 pass / 2 xfail (GC) on each); (3b) a reconnaissance before exceptions (maintainer, 2026-11-04): a low per-architecture layer under the D39 semantics for one group (memory: loads, stores, bounds checks) on x86-64 and aarch64; if speed holds, the wasm semantics move into one shared layer before exceptions and GC are written, else the backends stay as they are and the numbers are recorded (LESSONS 76). Done 2026-11-04: a scratch prototype (the bounds checks once, about ten low-layer operations per architecture) gave byte-identical CoreMark code on both architectures in both bounds modes, 4 of 5 979 modules different (`address64.0-3`), spec parity, compile time +3-4 % (BENCHMARKS.md, "JIT low layer"); identical bytes were accepted as the speed evidence, so no timed run. Decision (maintainer, 2026-11-04): unify, as its own step under a new contract, [S0011](specs/S0011-jit-low-layer.md) "JIT low layer" (approved 2026-11-05; implemented 2026-11-05: steps 1-4; step 4 removed the switch and the per-backend bodies and moved the bulk operators' operand rules into the shared layer, in aarch64's order per operator, so aarch64 code is unchanged and x86-64 changes by a reorder in the 70 `memory.copy` modules): the memory bounds checks plus the bulk-memory and table end checks (`_end_check`) and the element check (`_element`); compile-time bound +5 %; the four `address64` differences accepted; the `thin` variant not measured. The decision is D53; (4) exceptions, which may start in parallel with S0011 (per backend, as today; [S0012](specs/S0012-exceptions.md), approved 2026-11-05: status propagation as for traps, no handler tables; steps 0-4 done 2026-11-05: the runner, the front end and validator, `pyc`, `jit-x86_64` (D52: objects bumped from a store arena in code, about 12 ns per throw caught one frame up) and `jit-aarch64`, each claiming `exceptions/` at 100 pass / 8 xfail; step 5, the review, done and the fuzz findings triaged; implemented 2026-11-05); (5) GC ([S0013](specs/S0013-gc.md), draft 2026-11-05: a per-store GC heap as a hidden i32 memory with u32 references, lowered once in the shared core over D39 methods, a copying (Cheney) collector with precise roots from stack maps (maintainer, 2026-11-06), `pyc` on Python objects; approved 2026-11-10, every open question decided; steps 0 and 1a done 2026-11-11, next 1b; progress in S0013 "Progress"); (6) the artifact format, right after GC because it freezes the kernel boundary that runtime modules rest on (ARCHITECTURE.md, "The kernel and runtime modules"; with it, the instantiation work and the segment libcalls as one runtime module for the C loader, multi-memory open: RUNTIME-MODULES.md; when it is fixed, revisit the environment variables that change code bytes, `WABOROS_JIT_PAD`, `WABOROS_JIT_LOOP_ALIGN` and `WABOROS_PYC_BR`: they also change artifacts and any cache of them, D35; maintainer, 2026-11-10: they stay until then), one LIR vocabulary and the C loader, then `arm64.wasm`; (7) threads, then the flag-only proposals (WASM-PROPOSALS.md). In parallel: the corpus. Differential fuzzing (maintainer, 2026-11-04): a nightly scheduled CI job, outside `ci` and `platforms` and never blocking, runs `tools/fuzz.py` (it exists since S0010 step 0, comparing `pyc` with `pyc` + `bropt.py`, TESTING.md; since 2026-11-05 its mode `--targets pyc,jit,wasmtime` compares `pyc`, `jit-x86_64` in guard and explicit mode and Wasmtime, and the job is `fuzz-nightly.yml`) for 1-2 hours; a divergent module is not fixed at once but kept as a run artifact with a short description (which targets differ, on which export), and the findings are triaged before each large spec closes (exceptions, GC) and before phase 5 ends. The project-wide campaign with WASI stays in phase 6.

Order (maintainer, 2026-11-01): SIMD (S0005) -> the rename to `waboros` with the packaging for publishing (LICENSE,
setuptools + setuptools-scm, coverage/publish workflows, dependabot; D40)
as its own step (D40, done 2026-11-02) -> guard pages (D29; done 2026-11-03) -> the rest below.

* What remains of the opcode set, in stages (TARGETS.md), on **every** JIT target in step:
  SIMD (done, S0005), memory64 (done, S0009), relaxed SIMD (done, S0008), exceptions (done, S0012), GC; then, extending the list
  (maintainer, 2026-11-03; [WASM-PROPOSALS.md](WASM-PROPOSALS.md): the goal is parity with Wasmtime's
  defaults, then its flags): **wide arithmetic** (done, S0008) and **threads** (both on by default in Wasmtime 50),
  then the flag-only proposals, each behind an option of ours: custom page sizes, compact imports,
  stack switching, shared-everything threads. What a backend lacks is
  `unsupported` until it is written (D25). Floats, tables, `memory.grow`, bulk memory, reference types,
  multi-value and tail calls were done in phase 3.
  For the GC spec: at every call the operand stack is in slots (values are flushed before calls, as before; S0007 added no temporaries), but pinned locals stay in pool registers across calls; either
  reference-typed locals are never pinned, or stack maps cover the pinned registers.
* **Platform tests through cibuildwheel** (maintainer, 2026-11-03; before S0006 step 5, Android):
  waboros is one pure wheel, but cibuildwheel runs a test command on every platform of the map in
  TARGETS.md (Android emulator x86_64, iOS simulator, musllinux, the QEMU architectures i686, armv7,
  ppc64le, s390x, riscv64, Pyodide). It refuses a pure wheel, so a small test-only harness package
  with an empty C extension that depends on waboros is built per platform and runs our tests (the
  pattern of py-wasmext's coremark example). Publishing does not use cibuildwheel (maintainer): the
  one `py3-none-any` wheel is still built by `uv build` in `publish.yml`. The waboros wheel under
  test is built from the repository root by cibuildwheel's `before-build` hook and found by the
  test environment through `PIP_FIND_LINKS` (as py-wasmext does for Android and Pyodide), so
  waboros's own packaging is not touched. Per-platform test commands carry pytest flags and
  environment (`WABOROS_JIT_BOUNDS=guard|explicit`, `WABOROS_JIT_REQUIRE=1`, `-k` subsets), so most of
  the CI test matrix moves there, not only the platform jobs (the spec cache and WAT-compiled modules
  are prepared on the host and shipped with the tests where a sandbox has no network or wasmtime).
  The aim is one matrix over every target the project supports: the Python versions (cp311 to
  cp315, the free-threaded builds, PyPy if cibuildwheel still builds it), every OS and architecture
  of the map, and every JIT variant (`jit-x86_64`, `jit-aarch64`, each in guard and explicit mode;
  `pyc` where there is no JIT); a platform is supported when its cell is green.
  Done (2026-11-03): the harness in `ci/harness/` and `.github/workflows/platforms.yml`, run on every push and
  pull request like `ci`; it is the only place where macOS, Windows and Android are tested, and it also runs
  the spec suite on the JIT platforms (`ci/harness/spec.py`). The `os`, `macos-pythonorg` and `android` jobs and the
  macOS and Windows legs of `arm64` left `ci.yml`.
  Principle (maintainer, 2026-11-03): the matrix is as narrow as it can be without losing a
  behaviour difference that matters now; ppc64le, riscv64, s390x and iOS are off (armv7l joined the arm job natively later) (the same
  `pyc`; riscv64 gets a micro-compiler later, the others never, D48). A platform is added deliberately, and its own issues are
  dealt with then, not by fixing micro-bugs on every target now. The jobs are grouped by runner
  (cibuildwheel runs several architectures and Python versions per job); the exotic architectures
  stay reachable as optional jobs (armv7l native on the arm runner; ppc64le, s390x, riscv64 one
  QEMU runner each), run on request and non-blocking. Later (maintainer, 2026-11-03): once Wasm 3.0
  is covered on `pyc` and both JITs and the context ABI and the artifact format are frozen, the work
  splits into parallel tracks (one agent on WASI, another on a compiler for a further platform),
  and a platform taken on that way makes its job (QEMU or other) mandatory. Which Python versions and
  platforms `pyc` needs is decided by a measurement of where its behaviour actually differs.
  cibuildwheel itself is a dev dependency (`uv add --dev cibuildwheel`: its version is pinned in
  `uv.lock`, CI runs `uv run cibuildwheel`, dependabot updates it).
  It replaced the hand-made Android, macOS and Windows jobs and added the pyc-only platforms; the jobs that need our own tools or emulator flags stay (QEMU `qemu64`
  without SSE3+, QEMU with BTI enforced, D35 dumps, MDWE, the objdump/llvm-mc ISA gates, native
  CoreMark). Where wasmtime has no wheel (Android, Pyodide, Linux i686 and armv7l) the WAT of the tests is read
  from a cache filled on the host (`tests/watcache.py`, TESTING.md); tests that need the oracle skip.
* **`pyc`: host-dependent results** (maintainer, 2026-11-03). Measured over CPython 3.11-3.15(t), PyPy,
  i686, armv7, aarch64 and s390x ([PYC-PLATFORMS.md](PYC-PLATFORMS.md)). Done: `f32.demote_f64` and
  `sqrt` of a signalling NaN give a quiet NaN on every host (3.14, PyPy: 0 failures), and `OverflowError`
  is mapped to the out-of-bounds trap (i686: 244 -> 88 failures). Done (D46): a `pyc` generation mode
  that carries f32/f64 as int bit patterns, chosen at import by a probe on 32-bit x86 (x87 quiets
  signalling NaNs in any Python float); i686 CPython 3.13: 0 failures, 64-bit output unchanged.
* **Guard pages with a JIT-emitted signal handler** (`poc/w2jit_guard.py`: +20–25 % on CoreMark, the gap
  to Cranelift was mostly the bounds checks), per OS and architecture (Linux `sigaction` +
  `ucontext` offsets, macOS also SIGBUS, Windows a vectored exception handler), `sigaltstack` per thread
  for stack overflow; the default where verified (D44, which supersedes D43's opt-in), explicit checks the
  only mode of a new architecture; the steps in S0006 (done: Linux glibc x86-64 and aarch64, Linux musl x86-64, macOS, Windows x64 and arm64, Android x86_64; the artifact step moved to the artifact-format spec);
  bounds-check elimination where guard pages are not available.
* **Code placement** (after S0007, done; LESSONS 51): a loop's speed moves by up to ±10 % when
  code before it changes size (S0007 step 1, `swizzle`, on a Xeon). Two hypotheses were tested apart:
  (1) loop-header alignment to 16 or 32 bytes with NOPs; (2) Intel's JCC erratum (a branch that
  crosses or ends on a 32-byte boundary is not cached in the decoded-µop cache). **x86-64: done
  (2026-11-05), not implemented** (BENCHMARKS.md, "Code placement on x86-64"; LESSONS 85): on the dev
  VM's Xeon (Emerald Rapids, not in the erratum's list) both are refuted as stated; speed follows the
  hot loop's offset mod 64. Inner-loop headers aligned to 64 gave CoreMark +5.3 % but `swizzle` 18 % slower
  and the corpus geomean −0.9 %, for +10 % code, so it fails the rule (a gain over CoreMark, the kernels and
  the corpus beyond the spread; maintainer, 2026-11-05). Alignment to 32 (CoreMark about +4 % in two
  sessions) only if revisited. **aarch64: measured** natively on the CI arm runner (Neoverse-N2,
  2026-11-09; BENCHMARKS.md, "Code placement on aarch64"): pads move CoreMark by about 3 % and `swizzle` by
  about 12 %; alignment to 32 gives CoreMark +1.2…1.4 % but `dot` or `nearest` lose in one bounds mode each,
  for +4.5…6 % code, so it fails the rule as on x86-64. Decision (maintainer, 2026-11-10): **not implemented, closed for
  aarch64**. The knobs `WABOROS_JIT_PAD` and `WABOROS_JIT_LOOP_ALIGN` stay, off by default (see item (6)); `placement-arm.yml` reruns the
  sweep by hand.
* Trap frames, fuel and epoch checks (the interrupt word is polled already, phase 3); Ctrl+C via the
  waiting main thread.
* **Entry bookkeeping without a restore in a `finally`** (D51): on PyPy a `KeyboardInterrupt` at a
  JIT entry or exit can leave the store's depth and `active` raised; derive them instead (for
  example reset when the outermost entry finds no JIT frame below), then drop the PyPy skip of D34's
  stochastic test.
* **The JIT compilers as AOT-compiled wasm modules** ([JIT-AS-WASM.md](JIT-AS-WASM.md), draft,
  maintainer 2026-11-05): port, then replace the Python backends; a decision point (Dn) once the
  artifact format below is fixed; criterion byte identity and portability to non-Python hosts.
* **One LIR vocabulary** before porting to the micro-compilers (MICRO-COMPILERS.md): the operation
  names that are strings today in several places become one enumerated list, defined once,
  together with fixing the artifact format in this phase; in C or Zig they become enums anyway, so
  one list is ported instead of several naming schemes.
* The contract of ARTIFACT.md (format v1, context ABI v1, baseline-ISA sequences, CPU feature
  detection) is **fixed here**, after two architectures have shaped it, together with artifact
  serialization; the C reference loader runs in CI and the runtime services are specified (D36). The
  container (our own or ELF64, [ARTIFACT-CONTAINER.md](ARTIFACT-CONTAINER.md)) is decided in that spec first.
* Win64 hardening (the second entry trampoline and `VirtualAlloc` exist; `platforms.yml` runs the tests and the spec suite on
  Windows x64 and ARM64 and macOS x86-64 and arm64, TARGETS.md; Windows ACG is open, RISKS R2);
  macOS arm64 and Windows on ARM are done in phase 4.
* Differential fuzzing against wasmtime (`wasm-smith`): the nightly non-blocking job `fuzz-nightly.yml` runs it (`tools/fuzz.py --targets`; TESTING.md; findings triaged, not fixed at once). With a corpus
  of real programs, the scalar-gap investigation and WASI through fusedwasi, all of which can run
  beside phase 5: [REAL-PROGRAMS.md](REAL-PROGRAMS.md) (maintainer's note, draft).

## Phase 6 — runtime API, WASI, CLI
*Status: not started.*

* `Engine`, `Config`, `Store`, `Module`, `Linker`, `Instance`, externs, `Caller`, `Trap`/`TrapCode`,
  limits, in the shape of wasmtime-py (not a drop-in), over the internal engine interface the spec
  runner has used since phase 1.
* Choose the WASI host (open question 2), and with it whether WASI's computing part becomes a runtime module (RUNTIME-MODULES.md, item 8); WASI 0.1 and fused 0.2 through it (needs multi-memory, planned right after
  phase 3 on both targets).
* `Module.serialize`/`deserialize` and the thin/fat files (D18) on top of the cache of phase 2 and the
  artifacts of phase 5.
* CLI: `run`, `compile`, `wast`, `settings`/`targets`, `cache`.
* Differential tests against wasmtime on real programs.
* **GC handles released by the host** (maintainer, 2026-11-10, with S0013 Q10): until here a GC
  reference given to the host stays rooted until the store is dropped. One contract for every host
  (Python, MicroPython, a C loader, the runtime as wasm), Wasmtime's model: root scopes released
  together when they close, explicit long-lived roots released by `unroot`, generation-checked
  handles. Host conveniences on top (Python: `with store.scope():`, optionally a finalizer that
  marks the entry for release at a safe point), never the mechanism itself.
* **JIT code visible to debuggers and profilers** (maintainer, 2026-11-10, with S0013 Q3): the GC's
  frame walk uses return addresses and site tables, not a frame-pointer chain, so `perf`, `gdb` and
  native profilers cannot unwind through JIT frames or name them. Options to weigh then: a perf map /
  jitdump file (names and code ranges), GDB's JIT interface, unwind info registered for the code, or
  frame pointers as a `Config` switch (measured first: Wasmtime always keeps them).

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

* **riscv64 only** (D48): as a micro-compiler on the proven core after `arm64.wasm`
  (MICRO-COMPILERS.md); CI under QEMU user-mode. It is added by a micro-compiler, the encoder test
  and the integer-core spec commands; the shared core does not change. s390x, armv7, mips32el,
  ppc64le, loongarch64, i686 and the rest stay on `pyc` (s390x was excluded by the maintainer; the
  reasons are in D48).
* **Later, not a first priority (D48 amendment): the sljit track.** One D39 backend on sljit's API
  under the ported core, shipped as `sljit-<arch>.wasm`, may give those architectures a JIT,
  verified against `pyc` under QEMU ([SLJIT-BACKEND.md](SLJIT-BACKEND.md), `poc/sljit/`). Its first
  step (the sljit backend for x86-64 under our core) was done as a proof of concept on 2026-11-04:
  spec parity and about 100 % of our backend on CoreMark (SLJIT-BACKEND.md). With it, the D25
  question of instruction polyfills as runtime modules (RUNTIME-MODULES.md, item 7).

## Open questions
1. **Minimum Python.** Resolved by D19: 3.11 (the PoC's 3.8 and 3.4 `--legacy` measurements stay as
   history).
2. **The WASI host.** fusedwasi's Python twin (candidate; 0.1.0b1-b3 released 2026-11-05, D8, with the limits of a preopen on Windows; 0.1.0a1 was runtime-independent and ran on
   wasmtime, wasm3 and wasmext, D8; not tried with waboros yet),
   an own implementation, or another; and what WASI means on MicroPython (open question 6).
3. **Parallelism of `pyc` guests**: yield points only, or also subinterpreters (3.12+/3.14),
   free-threaded builds, a process with shared memory (ARCHITECTURE.md, "Long calls and the GIL")?
4. **Wasmtime options with no meaning here** (`cranelift_*`, `profiler`, `signals_based_traps`, …):
   accept and ignore, or reject?
5. **NaN payloads in `pyc`**: settled by D46: a mode that carries f32/f64 as int bit patterns, the default
   only where the host's floats quiet a signalling NaN (32-bit x86); elsewhere Python floats stay.
6. **WASI on MicroPython**: a slim twin, or fusedwasi compiled to wasm and translated by waboros itself
   (then each language/target needs only a thin layer of OS primitives)?
7. **Pyodide**: `pyc` runs there today; a separate track later (maintainer), not a JIT target (D48). The experiment in
   [PYODIDE.md](PYODIDE.md): the guest rewritten into an Emscripten side module and loaded with
   `ctypes.CDLL` runs CoreMark at 92–95 % of direct `WebAssembly` in Node, sharing Pyodide's heap;
   open: isolation (bounds checks), traps, grow, tables, D25 parity, browsers.
8. **Free-threaded CPython (no GIL)**: shared memories and atomics in `pyc` need real synchronisation. Plan (maintainer, 2026-11-04): the threads spec ("Next", item 7) carries a free-threaded section: an audit of all process-wide mutable state (registries, caches, counters, the store claim) for races without the GIL, the synchronisation of shared memories and atomics in `pyc`, and a stress test on 3.15t (several stores in parallel; a shared memory with atomics).
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
