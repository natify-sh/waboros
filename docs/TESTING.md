---
title: Testing
kind: reference
status: current
updated: 2026-11-24
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
The D25 parity test (`tests/spec/test_runner.py`) requires every JIT to fail what `pyc` fails in the directories both claim, with no exemption: since S0013 step 4 both JITs claim `gc`, and no target has an expected failure left (the "GC types" exemption of steps 2-3b is removed).

## Development dependencies (never runtime)

| tool | why |
|---|---|
| **wasmtime** (pip, pinned) | **the test oracle**: differential runs compare every waboros target with Wasmtime on the same module and inputs (results, traps, memory contents); also the reference for API behaviour and feature defaults |
| wasm-tools `json-from-wast` (`wasm-tools-package`) | turns the spec test suite `.wast` files into `.json` + `.wasm` ([S0001](specs/S0001-engine-interface.md)) |
| wasm-tools (`wasm-smith`, `validate`) | random valid modules for differential fuzzing; a second validator |
| pywasm (pip) | a second oracle and a source of ideas: its spec runner (`test/spec.py`) was adapted in the PoC session; it passes the core spec suite (54 006 commands, 0 failures at its pinned spec commit `fffc6e12f`) but treats `assert_invalid` / `assert_malformed` as no-ops |
| meld | fusing component test programs into core modules for WASI 0.2 |
| MicroPython unix port, `mpy-cross` | the `mpy` target |
| CMake >= 3.21, a C99 compiler, `gcc-aarch64-linux-gnu` | `native/` (S0016): sljit as a git submodule (`git submodule update --init`), built by CMake alone; never part of the package. The cross build for aarch64 runs its ctest under `qemu-aarch64 -cpu max` |
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
* **The walk check** (`WABOROS_JIT_WALK_CHECK=1`, both JITs, S0013 step 3a): every JIT instance
  created while it is set serves each of its host calls and libcalls only after walking the store's
  whole native stack (`jit/common/walk.py`); a `throw` allocates through `ALLOC`, checked as every
  service (S0013 step 6.2; the exception arena's refill service, removed in step 6.3, was checked
  only when the store's first instance had the switch). The exit
  record must be written, every return address must be a recorded call site, every stack word read
  must lie between the store's native limit and the thread's stack top, the walk must cross exactly
  the active entries and end each segment at the frame the runtime gave that entry; the walk then
  writes null into the dead reference slots of every frame (the map's `refs` slots that its `live`
  map does not mark, as a collection does; S0013 step 7, D65: since step 7.2 a whole-suite run nulls
  every dead set of the suite at every host call; `tests/jit/test_walk.py` also tests it with a
  forged table, through check mode and through a collection), and every slot of `refs` must hold a reference word: 0, a host value `(h << 2) | 2` with a live handle,
  and, only in a store with a GC heap, an i31 below 2³² or the offset of a heap object (allocated,
  8-aligned, its header a type-info record; S0013 step 3b; an exception object too, as exnref
  slots are marked from step 6.2). That last test cannot see a wrongly
  marked i32 slot holding a small integer, nor a missing mark (nothing moves before the
  collector, S0013 step 5). A failure is a `RuntimeError` raised by the
  call, so a broken frame layout or table shows up as a spec failure. Off by default: the
  instances then use the same host callbacks as without the switch. Hand-assembled instances
  (`tests/jit/test_abi*.py`, no stack-map table) are never checked. CI runs one spec run per
  architecture with it (below); `tests/jit/test_walk.py` sets it itself.
* **Through files** (`WABOROS_JIT_ARTIFACT=1`, both JITs, S0014 step 3; read at each
  instantiation): every compiled module is written as a thin artifact, loaded back with `artifact.load` (its
  compatibility checks included) and instantiated from the read form (`artifact.ModuleView`, no `ValidModule` behind it). `WABOROS_JIT_ARTIFACT=1
  uv run python -m tests.spec.run --engine jit-x86_64` (both bounds modes, and `jit-aarch64` under
  QEMU) must give the same results as without it (`tools/spec_diff.py` both ways: no difference),
  also with the walk check and the GC switches on `gc exceptions`. It cannot be combined with the
  switches that change code (`WABOROS_JIT_NOPIN`, `NOLIVE`, `PAD`, `LOOP_ALIGN`: `write` refuses).
  The artifact tests (`tests/artifact/`, S0014): `test_roundtrip.py` (every module of the pinned suite
  that the JITs compile, both JITs and every registered development target (`jit-sljit-<arch>`), both
  bounds modes, thin and fat, and CoreMark: the inventory's
  fields equal, `write(read(b)) == b`, written twice identical), `test_format.py` (the bytes of each
  payload, every constant-expression operation, a malformed file refused with a message),
  `test_strict.py` (the pinned container layout, one refusal per rule, a byte-flip loop),
  `test_review.py` (one refusal per finding of the S0014 step-8 review: supertype depth, resume
  offsets, stack-map site order, compiler-string tokens, allocation without the heap, limits,
  signature types; and the reader's linear time on long chains and many records) and
  `test_refuse.py` (every refusal about the header: thin refused with a message naming the field,
  fat regenerated and run). `tools/make_v1_artifacts.py` made the committed files once.
  `tests/artifact/test_instantiate.py` instantiates from the read form directly and checks that
  `Compiled.module` and the read module have only the inventory's keys.
  `tests/artifact/test_old.py` (S0014 step 4.3) reads and runs the v1 files committed under
  `tests/artifact/data/` (never regenerated; their README names the commit and compiler string); a
  later format or ABI bump keeps the test, changed to expect the version refusal. The
  `v1-small-*` files are **pre-S0015 fixtures**: `read` refuses them on the layout alone, the size of
  `.waboros.trampolines` (84 against 60 bytes on x86-64, 44 against 32 on aarch64), also with the
  compiler string patched to the file's; the fat one is regenerated from its wasm and runs. The
  `v1-s15-small-*` files (S0015 step 4) are read, run (the view on the native architecture), loaded
  thin and fat, and refused for their version like the first set.
  `tests/artifact/test_trampolines.py` (S0014 step 5): the instance of a loaded file runs on the
  process's trampolines; a blob byte, a moved `bridge`, `tail_switch` or `restore`, or a changed
  restore number of any convention is refused thin and regenerated fat; the payload's layout
  rules (the 28-byte record, the old 40-byte layout refused on its size); no entry or `sp_stub` in
  the file; `test_strict.py::test_centry_symbol` (the local `__waboros_centry_<conv>` symbol); and the store rule of a foreign host (two
  instances on one copy sealed from the first file's blob, a cross-instance tail call's first hop
  walked over that copy's restore frame). The
  round trip checks every convention's blob and offsets against the target's `conventions()` on
  every module.
* **The boundary convention** (S0015): `tests/jit/test_boundary.py`, on the machine's JIT and
  under QEMU in the `jit` job, which enforces BTI (the C entries' `bti jc` pads, reached by a
  `br x9`):
  tail-call ping-pong across two instances, 10⁶ round trips in a 256 KiB thread entered from the
  host, from a direct call and through a third instance's record (a calling control overflows);
  every public status (a trap, an exception caught one and two instances up or uncaught, a host
  error, an interrupt) across one and two instances; the walk across modules, at the far end of
  a hop chain entered from the host and from a direct call, and through the own-table path
  (D71) under the walk check and the GC switches; no status with bit 31 ever reaches the host.
  Both x86-64 runtime entries (SysV and Win64) are built and ISA-gated on any host with a GNU
  objdump for x86-64, the Win64 one checked for its saves and restores of rsi, rdi and xmm6-xmm15.
  Recursion through the own table reaches 32 bytes a level or better (aarch64 40) and across
  instances 40 bytes a call or better (aarch64 52; D71: this compiler's other contexts are called
  at their bodies). With hand-assembled C entries in each architecture's encoder, on a context of another
  compiler (its `HOST_BRIDGE` word differs, asserted, so the call takes the C path): a status
  with garbage in the upper half, through a record call and the runtime's `entry`; the statuses
  and the walk chains above again through such C entries, mixing all three record paths (own,
  this compiler's other contexts, C entry); a C entry clobbering every caller-saved register of
  the boundary keeps the caller's pins at a result, a trap, status 14 into a `try_table` and a
  first tail-call hop, called directly and through the own-table path, and DF and MXCSR (aarch64:
  FPCR) are as the boundary says after it; the same pins and statuses on the path into another
  context of this compiler; the runtime's `entry` keeps the host's callee-saved registers (the
  Win64 ones, `xmm6`–`xmm15` included, on the Windows cells of `platforms.yml`; AAPCS64
  `x19`–`x28` and `d8`–`d15`) behind a clobbering C entry and behind a compiled function whose
  pins fill the pool (its module's C entry saves only what its bodies do not keep).
* **The GC switches** (both JITs, S0013 step 5.3, D61; read when a store's heap is created, so a
  test sets them with `monkeypatch.setenv` before instantiating): `WABOROS_JIT_GC_STRESS=1` makes
  every allocation that may collect (in wasm code and the array services; never instantiation)
  collect, so every object moves at every allocation; `WABOROS_JIT_GC_POISON=1` fills newly
  committed object space, the from-space after each collection and the free part of the new
  active space with 0xA5, so a stale reference or an unwritten field reads garbage;
  `WABOROS_JIT_GC_VERIFY=1` checks the new space after each collection with `gcverify.check` (every
  root and reference field null, an i31, a live host handle or an object start) and compares
  `gcverify.digest` of the graph before and after; a failure loses the store (`RuntimeError`).
  They are used together: `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_POISON=1 WABOROS_JIT_GC_VERIFY=1
  uv run python -m tests.spec.run --engine jit-x86_64 gc exceptions` (also with
  `WABOROS_JIT_BOUNDS=explicit`, and on `jit-aarch64` under QEMU) must give 0 fail. The walk
  check's "nothing moves before the collector" no longer holds under stress: a missing mark now
  shows up as a stale or poisoned reference.
  `tests/jit/test_gc_moving.py` runs them on, case by case, against `pyc` as the oracle;
  `tests/jit/test_gc_roots.py` holds one reference in one place (imported global, shared table,
  another instance's frame, a thunk or restore frame, a failed instantiation) under them.
  `tests/jit/test_gc_subtype.py` (S0013 5.6) runs function-subtype sites (`call_indirect`,
  `return_call_indirect`, `ref.test`, `ref.cast`, `br_on_cast(_fail)`, final and non-final targets)
  on the functions of another module against `pyc`, shows that equality and final targets make no
  call, and checks that no compiled module uses GC group word 7.
  `tests/jit/test_stackmaps.py` (S0013 7.1, D65) checks the two-bitmap `STACKMAPS` encoding (round
  trip, pool sharing on `(nslots, refs, live)`), that a map-less table is the same bytes in step
  3a's form, and that a step-3a table with a map and forged tables (a `live` bit outside `refs`, a
  bit past `nslots`, an empty map, a map index past the pool, trailing or missing bytes) raise
  `ValueError`; `tests/jit/test_walk.py` refuses the step-3a table at instantiation.
  `tests/jit/test_liveness.py` (S0013 7.2, D65; the plan's t1-t14, compile only, both JITs) checks
  the exact `live` set of every site of small functions, one per edge kind: straight code, `tee`,
  `br_if`/`br_on_null`/`br_on_non_null` (label and fall-through), `br_table`, nested loops (and the
  pass count of `common/liveness.py`: 3 for that case), `return`/`unreachable`/`return_call` with
  dead code after them, `try_table` clauses (each kind, a loop label, a `throw`), nested
  `try_table`s, `if`/`else`, slots behind v128s, the sites inside `br_on_cast`,
  `return_call_indirect` and `struct.new`, a parameter and an `exnref` local; the gate and
  `WABOROS_JIT_NOLIVE`; and on the spec cache (skipped without it) `live` equal to `refs` above the
  locals. The spec suite alone does not exercise the loop fixpoint: no spec function with a
  reference local and a call site has a loop (every run takes one pass). **`WABOROS_JIT_NOLIVE=all`**
  or `=<function indices>` (test only, read at compile time, shaped like `WABOROS_JIT_NOPIN`) gives
  those functions `live = refs`, the maps of step 7.1, without changing a code byte: the switch to
  bisect a liveness bug per function (D65).
  The behaviour of the live sets (S0013 7.3, the plan's t16-t26) is in `tests/jit/test_gc_roots.py`,
  under stress, poison and verify on the machine's JIT (both bounds modes on x86-64; QEMU for
  aarch64): per edge kind (a loop's back edge, a `catch` and a `throw` to a handler, an enclosing
  `try_table`'s handler, `br_if`, `br_table`, `if`/`else`, the then-arm continuing after the end, a
  loop in a `try_table` body, a site before a `try_table`, slots behind v128s) a struct kept in one
  local and read only on that path survives one collection between the write and the read; a
  mutant that drops the edge nulls the slot and the read traps. t16 reads a frame's slots from a host
  function through `walk.walk`: after a collection the dead local is 0 and the live one in the next
  slot holds its moved struct; in check mode the dead `externref` slot is 0 at a host call. t24 and
  t25 show the feature: a 16 MiB array and a host value held only by dead locals are not kept by
  the next collection (with `WABOROS_JIT_NOLIVE=all` they are). t26 is the shape that "no nulling,
  full maps at allocations" breaks (a dead slot left stale, marked again at the next allocation).
  `tests/jit/test_walk.py` forges a map whose `nslots` reaches past the value stack: the collection's
  gatherer raises before it writes (a forged shift alone is refused earlier, by the walk's check of
  each segment's entry base). The mutation table is in S0013 Changes 7.3.
  `tests/jit/test_exceptions.py` (S0013 6.1) checks which exception modules get the heap (a
  `throw` or a `catch` clause: 128 KiB committed, no object space; only `throw_ref` and `catch_all`:
  none) and a thrown tag type's exception record (kind 1, its offsets and size, its key apart from
  the type's kind-3 record). From 6.2 (exceptions in the heap) it also checks that the first
  throw commits the semispaces, that a full heap at `throw` ("GC heap out of memory", the
  semispace cap lowered to 2 MiB with live exceptions in an exnref table) is not caught by an
  enclosing `catch_all`, that `throw_ref` of null traps before the pending word changes, and that
  an object whose identity a probe forged in the high or the low half matches no `catch $t`;
  after the 6.2 review also an exnref whose only type is an inline block result across
  collections, a missed clause that keeps a value under the outer label (the pad's scratch
  slot), and a pad read past the heap through a forged handle (`MEMORY_OUT_OF_BOUNDS`, both bounds
  modes). `test_a_throw_runs_no_python` and the full-heap test skip under
  `WABOROS_JIT_GC_STRESS=1`, where every throw collects.
  `test_gc_collect.py`'s table test (`TABLES`) keeps an exception only in an exnref table, with the
  pending word naming another one, and reads it after one collection (S0013 6.5: before, it
  passed with exnref tables not roots, LESSONS 122).
  `test_gc_collect.py`, `test_gc_roots.py`, `test_walk.py` and `test_stackmaps.py` hold exception
  payloads, exnref locals, globals and tables across collections and the throw's ALLOC site in
  place of the arena. Step 6.4 adds the plan's t-list (`docs/plans/S0013-6-plan.md`, section 6),
  each test setting the three switches itself: the object's bytes and its record (t1), a throw
  whose allocation collects, caught one and three frames up with pins live (t2), an exnref in
  every place it can be kept, rethrown after collections (t3, `test_gc_roots.py`), an exnref
  array of 1 000 exceptions with `array.new_fixed`, `array.copy` and `array.fill` (t4), the
  imported-tag aliases (t6), an uncaught payload read by the host after collections (t7), an
  `ExnRef` kept by the host and forged ones (t8), pads that miss to the enclosing and the
  caller's pad with an i32 payload zero-extended (t9), a tag type that also has a kind-3 record
  (t10). t11 (`test_no_module_uses_the_reserved_words`, steps 6.3-6.4) checks that context words
  11-13 are zero in every context and that no code names them, cross-compiled for both
  architectures in both bounds modes: the spec's `exceptions/` and `gc/` modules, CoreMark and the
  collector, and the trampolines and fault handlers assembled again under the spies. It sees a
  word as a backend call's word, as a displacement off the context register, or off a register
  the context was copied or added into (x86-64 `mov`/`lea`/`add`, aarch64 `mov`/`add`); a context
  address loaded from memory, and Python-side reads, are not seen. The 6.3 review's mutants (an
  aarch64 `add` into another register, an x86-64 copy of r15, a bridge read of word 11) fail it.
  `tests/jit/test_gc_commit.py` (D69): the committed bytes of a one-struct heap and of a
  function-subtype site, and 16 live stores under RLIMIT_DATA + 256 MiB in a subprocess (Linux
  only; skipped under QEMU and on Android).
  `tests/jit/test_gc_memory.py` (native only, the GC switches cleared, about 5 s on CPython 3.11 and
  3.13; it runs in every `pytest` of the CI matrix and in the native arm64 `tests/jit` job, no
  marker) is the memory test: after a warm-up the heap's capacity and committed pages do not move under 576 MB of dropped structs and
  cycles, a live set over half the lowered cap raises "GC heap out of memory", the peak RSS
  of a subprocess grows by under 8 MiB (Linux), and 10 M thrown and dropped exceptions leave the
  committed pages and the capacity where 1 M left them (t12 of S0013 step 6, 0.1 s). The mutation
  lists (S0013 Changes 5.5 and 6.4) are scratch scripts, not in the tree: each mutant a
  `git archive` copy run with `PYTHONPATH` set to it (LESSONS 121), a process group killed on
  timeout.

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
* `tools/fuzz.py --targets pyc,jit,wasmtime [--minutes M] [--seed S] [--gen] [--refs] [--out DIR]` compares
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
  with the minutes) runs smith, `--gen` and `--refs` for 45 minutes each outside `ci` and `platforms`, never
  blocks, puts the counts and the descriptions in the job summary and keeps the artifact `fuzz-out`
  (the modules, the descriptions, the logs) for 14 days; a divergence is triaged later (above), not
  fixed at once. `--refs` (S0013 step 7, D65) also turns on smith's reference types and GC
  (exceptions are always on in this mode), passes null for a nullable reference parameter (an
  export with a non-nullable one is not called; a reference result compares as null or not) and
  runs each engine in a process of its own (a fork per engine and module, one after the other; the
  parent keeps no engine), each with the whole 8 GiB data limit (`DATA_LIMIT`) and its own alarm.
  Smith's constant expressions allocate arrays of up to 2³² elements, which `pyc` holds as Python
  lists, and without the limit the OOM killer ends the run (LESSONS 126); since the engines no
  longer share one process's memory, a failure to allocate is that engine's own: the JIT's "GC
  heap out of memory" (the documented 2 GiB limit, S0013), or `pyc`'s lists past 8 GiB, is the
  divergence that remains, and a rerun alone gives the same answer. A child that is killed from outside or crashes shows as
  `inst:ChildDied <code>`; one that exceeds its alarm makes the module count as a timeout, not as a
  divergence. The alarm is 20 s per engine, 400 s when `WABOROS_JIT_GC_VERIFY=1` (every collection
  digests the reachable graph before and after: a module with a large heap took 297 s there, S0013
  Changes 7.3). With the GC switches (`WABOROS_JIT_GC_STRESS/POISON/VERIFY/WALK_CHECK=1`) the run
  exercises the live sets of smith's reference locals at every allocation and host call. The
  oracle's wrapper returns a non-null `exnref` result as an opaque "exn" reference, so such an
  export compares as non-null on every engine. Without `--refs` the engines still share one
  process and one alarm per module (the mode is unchanged). The nightly workflow runs
  `--refs` as its third mode (`fuzz-out/refs`, the same minutes; the job's limit is 180 minutes);
  one child can take up to 8 GiB, so one fuzz run at a time (LESSONS 128). Class A divergences
  (the heap limit, about 1 % of modules) are kept in the artifact and triaged, not filtered.
  Wasmtime has its own limits that look the same from here: its GC heap (about 4 GiB, cumulative:
  a `LinkError` "GC heap out of memory: no capacity for allocation of N bytes" while instantiating,
  where `pyc` still allocates), and a `table.grow` of a 64-bit table by a huge delta (2^32 and up)
  that raises an out-of-memory `WasmtimeError` instead of returning -1 (class E, S0013 Changes,
  step 8 triage); both are Wasmtime's behaviour, not ours.
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
* `src/waboros/targets/jit/common/gcverify.py` (S0013 step 5.1) is a test-only verifier of a JIT GC
  heap image (form check, relocation-independent digest), tested by `tests/test_gcverify.py`; the
  collector (step 5.3) will call it after each collection under `WABOROS_JIT_GC_VERIFY=1`, which does
  not exist yet.

## Programs and benchmarks

* CoreMark (`poc/in/core.wasm`, `zig cc -O3`): correctness by its own CRC self-check, speed in it/s.
  Record new numbers in BENCHMARKS.md with the conditions.
* Corpus of freestanding libraries (`poc/bench/corpus/`, `tools/run_corpus.py`): Monocypher, SHA-256,
  BLAKE3, miniz, xxHash and QOI checked against `hashlib`, `zlib`, reference vectors and pure-Python references on every
  target and Wasmtime; exit status non-zero on a mismatch. Timings in BENCHMARKS.md.
* Calls benchmark (`poc/bench/calls/`, `tools/run_calls_bench.py`, S0007): a 2-argument leaf called in a
  loop and recursive `fib(27)`, ns per call on `jit-x86_64` and Wasmtime; fails if the results differ.
* WASI: the WASI testsuite (p1), the chosen host's conformance tests (fusedwasi has them for p2),
  small programs in C, Rust, Zig.
* Long calls: a guest loop interrupted from another thread and by SIGINT (CLI) on every target; another
  Python thread's progress during a long call.
* A few larger real modules (e.g. a compression library, a Python-in-wasm build, the ballistics core
  compiled to wasm) for compile time, cache behaviour and speed.

## Native code (`native/`, S0016)
`cmake -S native -B build/native && cmake --build build/native && ctest --test-dir build/native`
(Ninja or Make; CMake >= 3.21). `native/tests/test_native.c` checks, against the real return address and
stack pointer, sljit's frame numbers (`waboros_sljit_frame`: 180 shapes on x86-64, 340 on aarch64,
saveds up to 6 and 10, `7 | SLJIT_ENTER_FLOAT(3)` scratches, C and `REG_ARG | KEEP(3)` functions, locals 0
to 4096), the stub shape (16 bytes x86-64, 12 aarch64, body behind it, one shared centry), the baseline
bytes of `clz`/`ctz`/`select` on x86-64 (no `lzcnt`/`tzcnt`: guards the wrapper `waboros_sljit_create`)
and on aarch64 a BTI ping-pong of `SLJIT_CALL_RETURN` tail calls with its SIGILL control (the control
runs only under an emulator). `tests/consumer/` is a tiny project embedding `native/` by
`add_subdirectory` and by `FetchContent_MakeAvailable` (`SOURCE_SUBDIR native`), configured with
`--warn-uninitialized -Werror=dev`. The cross build: `-DCMAKE_SYSTEM_NAME=Linux
-DCMAKE_SYSTEM_PROCESSOR=aarch64 -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc
"-DCMAKE_CROSSCOMPILING_EMULATOR=qemu-aarch64;-cpu;max;-L;/usr/aarch64-linux-gnu"`. CI: the `sljit` job
(build, ctest; then `jit-sljit-x86_64` in-process with the walk check and poison in both bounds
modes, `tools/spec_diff.py` both ways against `jit-x86_64` on the JSON reports, and
`tests/jit/test_boundary.py`, `tests/sljit/` and the parity test of `tests/spec/test_runner.py`; the suite through artifact
files with `spec_diff` both ways, and `tools/isa_all.py --target jit-sljit-x86_64`), steps in `jit` (the cross build under QEMU with BTI) and `arm64` (native, no BTI), all
with `submodules: true`.

`tests/sljit/` registers the development target `jit-sljit-<arch>` when the library is built
(`WABOROS_SLJIT_LIB`, default `build/native/`), and refuses a library older than any `native/`
source (rebuild with `cmake --build build/native`). A development target (`development = True`,
`complete = False`) is never chosen by `auto`, has no FEATURES column, and joins the complete JITs
in the parity test (it fails exactly what `pyc` fails).

Its files (S0016 step 2): `WABOROS_JIT_ARTIFACT=1 uv run python -m tests.spec.run --engine
jit-sljit-x86_64` runs the suite through artifact files with its own compiler string (the package
digest folded with `digest_sources()`: `tests/sljit/*.py` and `native/`'s sources) and both blobs,
`sysv` and `win64`; `tools/isa_all.py --target jit-sljit-x86_64` gates its code and blobs (the
x86-64 allowlist has the SSE/SSE2 spellings sljit emits, `cmpeqss`, `cmpeqsd`, `unpcklps`,
`unpcklpd`, and objdump's `rex` for a REX prefix with no bits). `tests/sljit/test_win64_bridge.py`
executes the `win64` bridge on Linux against `waboros_sljit_test_win64_host`, an `ms_abi` function
of the library (arguments, the call's 16-byte alignment, the 32-byte shadow space written whole by
the host, the bridge's saved registers and `HOST_ERROR` preset surviving it, the exit record and the
walk from it); `tests/sljit/test_store.py` covers `artifact.targets()`, the digest fold, the store's
sets of trampolines and the refusal of two targets on one bridge address; `tests/sljit/test_refusals.py`
the refusals (a shortened stub jump, a frame above the headroom, a stale library, no compiler left
behind, and code generated below 4 GiB, where sljit would form `SLJIT_MOV_ADDR` as an absolute
address that the copy does not relocate).

Mixed stores (S0016 step 3): `tests/jit/test_mixed.py` puts the machine's JIT and each development
compiler of its architecture in one store, both directions (pairs from `TARGETS`; aarch64 joins in
step 5); it skips without the library. It covers calls of every value type (NaN payloads, v128,
externref and funcref by identity, a struct and an i31 made in the caller) through an import,
`call_indirect` and `call_ref` into the other compiler's table; D71's paths, read from the walk at a
host import (each frame labelled `body`, `centry`, `xcall` or `ccall` by its return address against
the module's centry, and the restore frames crossed): each compiler calls its own records at the
body and the other's at its C entry; tail ping-pong in S0015's three entry shapes (10^6 round trips
in 256 KiB, the shape at the end of the chain checked, the call-instead-of-tail control); every
public status through one and two C entries; memory 0's base after a call and after
tail_switch's first hop into another instance (its own memory) and after an i64 memory moved by the
callee (the same two cases in `test_boundary.py` within one compiler: `jit-sljit`'s xcall,
ccall and restore reloads each fail one of them when removed); and INTERRUPT from another thread while the callee
loops (bounded: a lost interrupt ends the loop after 2^32 - 1 rounds and fails); the walk with the
GC switches across both compilers and a mixed tail chain, with the collector's compiler asserted
(the second-instantiated module's, made for its function-subtype site or by the first collection
its frames run). `tests/artifact/test_sljit.py`: a file refused by the other compiler's loader on
its compiler string both ways (thin), regenerated when fat, and the same bytes from two processes.
The `sljit` CI job runs both, with `tests/artifact`, in both bounds modes, with the walk check,
poison and `WABOROS_JIT_GC_VERIFY=1`, in-process and through files.

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
`publish.yml` runs one release per ref and is never cancelled. A push to a branch that has an open pull request is tested once, by the `pull_request` run: in `ci.yml`, `platforms.yml` and `coverage.yml` the job `dedup` (on `ubuntu-slim`) asks the API for an open pull request from that branch and the other jobs are skipped on the push run; pushes to `main`, tags and branches without a pull request run as before. Caches: `setup-uv` keeps uv's package cache and, with `cache-python: true`, the managed Pythons; the non-Linux cells of `platforms.yml` keep cibuildwheel's download cache (`CIBW_CACHE_PATH`, `actions/cache`; Linux builds in containers and does not use it); `publish.yml` caches nothing. Jobs of `.github/workflows/ci.yml`: `lint` (ruff, pyright, `check_docs.py`, `features.py --check`); `test`
(`pytest -q` on the seven Pythons, in the default bounds mode, `auto` (D44: guard pages on this Linux glibc x86-64
runner);
on 3.13 again with `WABOROS_JIT_BOUNDS=explicit`, so explicit checks stay covered, and the three `pyc`
test files with `WABOROS_PYC_FBITS=1` (D46: f32/f64 as int bit patterns, the default only on 32-bit x86);
`tools/stack_info.py` (the runtime's `entry` and `sp_stub`, S0015) on
3.13, also in a musl container); `jit`
(`tests/jit` with `WABOROS_JIT_REQUIRE=1`; the aarch64 ABI, compiler, tail-call and SIMD tests
(`test_simd_abi.py`, `test_simd_aarch64.py`, in explicit mode: QEMU splits a straddling 16-byte store,
D45) and the spec suite of `jit-aarch64` (it covers `simd/` since the 5e2 claim) in both bounds modes, then
`test_guard.py` in guard mode, under QEMU with BTI enforced (its subprocess cases skip without binfmt); the x86 SIMD tests and the explicit `simd` spec run of
`jit-x86_64` under the SSE2-only `qemu64` CPU with `WABOROS_JIT_BOUNDS=explicit` (QEMU splits wide
stores, D45), compared with `pyc` by `tools/spec_diff.py` (strict since 5d3), run inside an
`ubuntu:latest` container because the Ubuntu 26.04 runner's own userland is the `amd64v3` variant
(SIGILL on an SSE2-only CPU before Python starts);
the spec suite of `jit-x86_64` and `tests/jit/test_guard.py` with `WABOROS_JIT_BOUNDS=guard` named
explicitly (guard pages, S0006; the same two in a `python:3.13-alpine` container for Linux musl
x86-64, `MUSL_GUARD`; the test file runs on the JIT of the machine and skips
on other hosts, but fails under
`WABOROS_JIT_REQUIRE=1` when `auto` does not give guard pages), so a regression in the
verified-row check cannot silently turn guard pages off (that `jit-x86_64` guard run, the
explicit `jit-aarch64` run under QEMU and the guard run of the `arm64` job also set
`WABOROS_JIT_WALK_CHECK=1`; the `jit` job also runs `gc exceptions` on `jit-x86_64` with the three
GC switches, explicit bounds, S0013 5.3);
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

Package tests run in parallel with pytest-xdist (`-n auto`, in the dev group; the runners have 4 vCPU)
wherever the process is native and can start subprocesses: the `test`, `jit` and `arm64` pytest runs of `ci`,
`coverage.yml`, the WAT-cache fill and every cibuildwheel cell except Android and Pyodide, whose runtimes
cannot start the worker processes (execnet). Serial: the single-file `tests/jit/test_guard.py` runs (too small
to gain), every run under QEMU user mode (`tools/run_arm64.py`, the qemu64 container): without binfmt its child
processes are not emulated (xdist workers for aarch64 would not start; in the qemu64 SSE2 step they would run
natively with AVX and silently stop testing SSE2). The spec runner (`tests/spec/run.py`) runs its per-file
child processes in parallel (`--jobs`, default the CPU count); with `--in-process` it is serial.
Measured on GitHub runners, one run each (2026-11-09; serial: `ci` run 42715354410, `platforms` 42715354559;
parallel: 42723969981, 42723969854): `spec` 3:11 → 1:30, `test` 3.13 2:39 → 1:38 (the other CPython cells
−27…−43 %, PyPy 0:53 → 0:56), `arm64` 6:43 → 4:38, coverage 3:15 → 2:07, cibuildwheel cells −26…−58 %
(Android and Pyodide, serial: −7 % and −4 %); `jit` 10:48 → 16:28 and 17:52 (`main` run 42730778821
with the caches): its native pytest step got faster (0:54 → 0:40), but every QEMU step and both
`isa_all.py` runs, none of which changed, took about 1.5-1.8× as long in both later runs. Cause not
established (the runner's CPU is not logged); probably the runner, not the change.

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

Packaging workflows (D40): `.github/workflows/coverage.yml` runs `pytest --cov` on Python 3.13, on x86-64 and on native arm64, and keeps
combines the two data files (`coverage combine`) into one `coverage.xml`, kept as an artifact and sent to Codecov (OIDC; informational, `codecov.yml`: its statuses and upload errors never fail CI); `publish.yml` runs on a `v*` tag, builds the sdist and the wheel,
smoke-tests the wheel in a clean environment, and publishes to PyPI (trusted publishing) and as a GitHub
release. `.github/dependabot.yml` updates the `uv` lock file and the pinned actions weekly.

Measurement workflow: `.github/workflows/placement-arm.yml` (by hand only,
`workflow_dispatch` with the mode and the rounds) runs `tools/placement_sweep.py` on `ubuntu-24.04-arm`, one job per
bounds mode. Mode `compare` instead runs `tools/bench_compare.py` on three source trees, `base` (a
git ref, default `HEAD~1`), its placement control `basepad` (`--pad-control`: `nop`s where S0015
step 3 puts the C entries) and this commit, alternated round by round: CoreMark, the calls
benchmark and two record-call kernels, medians and ranges in the summary and the artifact
`compare-<mode>`. It is the code-placement study on native aarch64 (BENCHMARKS.md, "Code placement on
aarch64"), not a gate. It fails only on a wrong result: CoreMark not validated, a kernel checksum or a
corpus result off. The values go to the job summary and the artifact `placement-<mode>`. A local run
under QEMU checks correctness only: `python3 tools/run_arm64.py tools/placement_sweep.py --quick
--rounds 1 --pads 0,4 --aligns 16 --align-pads 0`.

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
spec module of **every directory, claimed or not** (so `simd/` too), and CoreMark, through `tools/jit_dump.py --files`
(one artifact file per module; S0014 step 6), reads the files back and disassembles the code of each (up to
the first trampoline blob, data excluded) and every trampoline blob with `tools/isa_check.py`, failing on any
instruction above x86-64-v1 (D24). `isa_all.py --target jit-aarch64` does the same for aarch64 code,
compiled (never run) on any host: `isa_check.py --arch aarch64` disassembles with `llvm-mc` and allows
ARMv8.0-A and the `bti` HINTs only (tests: `tests/jit/test_isa_aarch64.py`, planted LSE/CRC/SVE/fp16/
HINT words must fail).

Cross-compilation (D35): `tools/jit_dump.py --target T OUT` writes a sha256 list of the code of every
spec module and CoreMark, and from S0013 step 3a a fourth column, the sha256 of its stack maps
(`STACKMAPS`; code bytes and maps are compared as separate columns). The `jit` job uploads its lists for both targets (artifact `jit-dump-x86`);
the `arm64` job (Linux) compiles the same modules and `diff`s its lists with them, so code compiled on
x86-64 and on arm64 is the same bytes both ways.

Files across OS (D23, S0014 step 6): a `.wbo` file depends on the architecture, not on the OS.
`tools/artifact_dump.py OUT` writes the files of CoreMark and `v1-small.wasm` (thin and fat, both JITs,
guard and explicit; 16 files, under 1 s) and lists `<name> <sha256>`. The `platforms` workflow runs it
as `artifacts` on Linux x86-64 and arm64, Windows x64 and ARM64, macOS x86-64 and arm64 (a plain `uv run
--no-project`, no wheel), and `artifacts-diff` fails if any list differs from the Linux x86-64 one,
naming the list; `diff` shows the file.
Independent readers (`jit` job, Linux x86-64, about 1.5 s plus the `elfutils` install):
`uv run --with pyelftools python tools/elf_readers.py` checks the same 16 files with `readelf -aW`,
`llvm-readobj --all`, pyelftools, `llvm-objdump -d` and `eu-elflint --gnu-ld` (S0014 step 6 lists its two
allowed complaints, the private OS/ABI); a file or a complaint outside the list fails the job. The compiler string's source digest hashes bytes, so
`.gitattributes` (`eol=lf`) must keep Windows checkouts LF.

The `spec` job also runs `--engine auto` with executable memory forbidden: `tools/no_exec.py` sets
`PR_SET_MDWE` (`PR_MDWE_REFUSE_EXEC_GAIN`, Linux >= 6.3; the kernel then refuses `mprotect(PROT_EXEC)` on
written memory, the JIT's W^X path) and `exec`s the runner. `auto` must warn once
(`JitUnavailableWarning`) and give `pyc`'s numbers. `tests/test_fallback.py` does the same in a subprocess.
