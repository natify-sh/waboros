---
title: Exception handling (exnref) on pyc, jit-x86_64 and jit-aarch64
kind: spec
id: S0012
status: approved
superseded_by:
related: [D19, D25, D30, D32, D34, D35, D39, D47, D51, R2, R3, S0001, S0002, S0003, S0004, S0011]
owner: maintainer
updated: 2026-11-05
---

# S0012 Exception handling (exnref) on pyc, jit-x86_64 and jit-aarch64

## Summary
The exception-handling proposal of Wasm 3.0 (`exnref`): tags, `throw`, `throw_ref`, `try_table`
with `catch`, `catch_ref`, `catch_all` and `catch_all_ref`, and the types `exnref` and
`nullexnref`. It is a Wasmtime 50 default (WASM-PROPOSALS.md), so it is a parity item on every
target (D25). It touches four contracts: the trap and status codes, the context ABI (S0003), the
D39 backend interface, and the spec runner's engine interface (S0001). That is why it needs a spec.

**The pinned suite** (`test/core` at the S0001 commit) has 110 commands in `exceptions/`:
* `tag` 10, `throw` 13, `throw_ref` 15, `try_table` 67, `cast` 5;
* 18 of them are `assert_exception`, which the runner does not know yet (the wasmtime oracle fails
  them as "unsupported command");
* `cast` needs GC's `ref.test` and `ref.cast` (0xFB prefix).

In the core directory, tags appear in `imports.wast`, `instance.wast`, `exports.wast` and
`ref_null.wast`. Nine core commands are expected failures on every target today with "tag import",
and some `no current module` failures follow them in `instance.wast`. `ref_null.wast` also needs
`anyref`, so it stays with GC. **The legacy proposal** (`try`, `catch`, `rethrow`, `delegate`) is
not in `test/core`: the spec repository keeps it in `test/legacy`, which `fetch.py` does not fetch.
Wasmtime 50 refuses it by default ("legacy_exceptions feature required for try instruction",
checked 2026-11-05), and wasmtime-py 50's `Config` has no switch for it. It is out of scope (Q8).

**Today:** the reader parses the tag section and tag exports, and the validator checks that a tag
type has no results. A tag import, the types `exnref` (0x69) and `nullexnref` (0x74), and the opcodes
0x08 (`throw`), 0x0A (`throw_ref`) and 0x1F (`try_table`) raise `NotImplementedError` in
`binary.py`. The legacy opcodes 0x06, 0x07, 0x09, 0x18 and 0x19 do the same.

**The design in one paragraph.** An exception travels as it is already carried for traps: as a
non-zero status returned through every frame. Each JIT call site already tests the status and
jumps to the frame's unwind code (S0003). Inside a `try_table` the same jump goes to a landing pad
of that `try_table`. The landing pad passes every status except "exception" on, and dispatches
the exception over the catch clauses. A function with no `try_table` compiles to the same bytes
as today. On `pyc` an exception is a Python exception of one private class, and `try_table` is a
`try`/`except` of that class only. Traps, host exceptions and interrupts cannot be caught, as in
Wasmtime.

## Goals / Non-goals
* Goals:
  * `exceptions/` passes on `pyc`, `jit-x86_64` (guard and explicit) and `jit-aarch64` at parity,
    except `cast` (GC). The tag-import failures of the core directory are gone.
  * The behaviour at the host boundary is Wasmtime's, as measured through wasmtime-py 50 (Q4).
  * Code without exception instructions is byte-identical on both JITs: D35 lists before and after.
    That is the speed evidence for the non-throwing path (Q1).
  * The contract changes are stated once: the status code, the context words, the D39 methods and
    the runner command.
* Non-goals:
  * The legacy proposal (Q8).
  * GC, including `ref.test` and `ref.cast` on `exnref` (`cast.wast`).
  * Reclaiming exception objects before their store dies (Q3).
  * The public API for exceptions (`Exn`, `Tag` objects, host-thrown exceptions): phase 6.
  * `mpy`: phase 7. The `pyc` mapping is chosen so that it carries over (Q6).
  * Stack switching (`UnhandledTag`).
  * An optimisation of `throw` (native allocation, a static local catch as a plain jump), unless a
    measurement asks for it.

## Interface

### Status and trap code
`14 UNCAUGHT_EXCEPTION` (ARTIFACT.md part 3) gets two meanings, split by where it is seen:
* **Inside wasm code, status 14 means "an exception is in flight".** It is the only status a
  landing pad dispatches. Every other non-zero status passes through as today.
* **At an entry (the trampoline's return, or `pyc`'s `_run`), status 14 becomes
  `Trap(TrapCode.UNCAUGHT_EXCEPTION)`.** That trap carries the exception as `exn`: the tag handle
  and the payload values. The host never sees status 14 in any other form.
* **A host function never produces status 14.** A `Trap(UNCAUGHT_EXCEPTION)` raised by a host
  function is treated as any other host exception: it is stored, the status is 13 `HOST_ERROR`
  (D34), and the runtime re-raises it as itself at the outermost entry. So an exception that left
  wasm through a host frame cannot be caught again by the wasm frames below it. Wasmtime-py 50
  does the same (Q4).

`base.TrapCode` gains `UNCAUGHT_EXCEPTION = 14`. The runner's `TrapCode` (S0001) gains it too.

### Context ABI (an amendment of v1, Q2)
| word | name | direction | meaning |
|---|---|---|---|
| 10 | `EXCEPTION` | host → code | the address of the store's pending-exception word. The word holds the exnref of the exception in flight, valid only while status 14 propagates. One word per store, so an exception crosses contexts (a call through a function record, D32) |
| 11 | `EXN_REFILL` | host → code | the arena refill service: a host function with the libcall signature (`int32_t f(ctx, slots)`), called through the bridge only when a throw finds the arena's chunk full (step 3, D52) |
| 12, 13 | `ARENA_CURSOR`, `ARENA_LIMIT` | host → code | addresses of the store's arena cursor and limit (step 3, D52) |
| … (after the function records) | per tag *t*: 1 word | host → code | the tag's identity: a non-zero value unique in the process, imported tags first. An imported tag has its exporter's value. |

* The tag words are appended after the last group, so no existing offset moves.
* Words 10-13 were reserved and zero, so a context without exceptions does not change.
* **`throw` runs no Python** (step 3, D52). The code bumps the object from the store's arena chunk,
  writes the tag word and the payload, makes the object pending and returns 14. When the chunk is
  full it calls word 11 through the bridge with `slots[0]` = the words needed and allocates again.
* **The exception object** (JIT): 8-byte words `{tag identity, payload words…}`. The payload is in
  slot form: one word per value, two for a v128 with the low half first. The object belongs to
  the store, never moves, and lives until the store is dropped (Q3).
* **An `exnref` value** is the object's address; null is 0 (ARTIFACT.md: references are
  pointer-sized).

### D39 amendment (JIT core ↔ backend)
* `Frame.error: Label`: where a non-zero status of a call site jumps. The core sets it. It is
  `f.unwind` outside every `try_table`, which keeps today's bytes, and the innermost landing pad
  inside one. Every `call_*` and `call_host` method uses `f.error` where it uses `f.unwind` today.
  Trap stubs and the interrupt poll keep `f.unwind`: they cannot be caught.
* `landing(f, pad: L, outer: L)`: bind `pad`, then branch to `outer` unless the status is 14.
  `outer` is the enclosing landing pad or `f.unwind`. The status register is preserved.
* `exn_load(dst: Slot)`: the exnref in flight, read through context word 10.
* `exn_match(word: int, miss: L)`: branch to `miss` unless the pending object's tag word equals
  context word `word`. It preserves the status register and leaves no flags live.
* `exn_field(dst: Slot, k: int)`: payload word `k` of the pending object into a slot.
* `throw(f, word: int, base: int, n: int)`: a new object `{context word word, slots base..base+n}`
  from the arena, pending, status 14, jump to `f.error` (step 3: allocation needs registers the
  core cannot name).
* `throw_ref(f, x: Operand)`: trap `NULL_REFERENCE` if `x` is 0. Otherwise write `x` to the
  pending word, set the status to 14 and jump to `f.error`.

The catch semantics live once, in `common/compiler.py`: the order of the clauses, the branch with
the payload (and with the exnref for the `_ref` forms), and the fall-through to `outer`. The
backends implement the five encodings above, about 40-70 lines each. This is per backend, as
ROADMAP "Next" item 4 says. It does not use the S0011 low layer and does not wait for S0011
step 4 (Q5).

### `pyc`
* `runtime._Thrown(Exception)`, with `__slots__ = ("tag", "vals")`. An exnref is the `_Thrown`
  instance itself; null is `None`.
* A tag is a `runtime._Tag()` object, compared with `is`. An imported tag is the exporter's object.
* `throw $t`: `raise _Thrown(T<t>, (v1, …))`.
* `throw_ref`: a helper raises the same object, after clearing `__traceback__`, `__context__` and
  `__cause__`. Otherwise a rethrow in a loop grows a chain (test plan).
* `try_table`: `try:` body `except _Thrown as _e:` then the dispatch: `if _e.tag is T<a>:` assign
  the label's variables from `_e.vals` (plus `_e` for `catch_ref`), then the branch to the label
  (S0010 emission), …, else `raise`. Only `_Thrown` is named: `Trap`, `RecursionError`, host
  exceptions and `KeyboardInterrupt` pass by. `except … as _e` unbinds `_e` at the end of the
  clause, so the `_ref` forms copy it into the label's variable first.
* `_run` turns a `_Thrown` that escapes into `Trap(UNCAUGHT_EXCEPTION)` with `exn`, at every
  entry, so a host function sees what it would see with the JIT. A call to an import from another
  `pyc` instance of the store is a direct Python call, not an entry (`pyc/__init__.py`, the owner
  is "pyc"), so an exception crosses it uncaught, as `catch-imported` needs.

### Front end, validator, linking, runner
* Reader:
  * value types 0x69 `exnref` and 0x74 `nullexnref`;
  * heap types `exn` and `noexn`;
  * tag imports (kind 4);
  * opcodes 0x08 `throw` (tag index), 0x0A `throw_ref`, and 0x1F `try_table` (block type, then a
    vector of catch clauses: kind 0-3, tag index for kinds 0-1, label index).
  * The legacy opcodes stay `NotImplementedError` (Q8).
* Validator, as the spec:
  * `throw` is stack-polymorphic over the tag's parameters;
  * `throw_ref` takes `exnref`;
  * each clause's label must accept the tag's parameters (`catch`), plus `(ref exn)` (`catch_ref`),
    nothing (`catch_all`), or `(ref exn)` (`catch_all_ref`), with subtyping
    `nullexnref <: exnref` and `(ref exn) <: exnref`.
* `Extern.kind` gains `"tag"` (S0002). A tag import's value is `(exporting instance, tag index)`,
  built by `link.resolve`; each target resolves it to its own tag object, and an instance of another
  target is a `LinkError` ("incompatible import type"): a tag has one identity per store and
  target. `TargetInstance.export` of a tag returns `Extern("tag", owner, <the target's tag>)`.
* `Trap.exn` is the same on every target: `(tag, values)`, the values as `call` returns them on
  that target (i32/i64/f32/f64 as raw bits, v128 as an int; references are target handles, as
  results are). A host that passes a value that is not an exnref where one is expected gets a
  `TypeError` on every target.
* `link.py` checks a tag import's type as a function import's: equivalent function types
  ("incompatible import type", `tag.wast`).
* Runner (S0001):
  * `assert_exception` passes if the call raises `Trap` with `UNCAUGHT_EXCEPTION`, and fails on
    any other outcome;
  * `wasmtime_engine` maps a `wasmtime.Trap` whose `trap_code` is None and whose message has
    "thrown Wasm exception" to that code.

### Rows (D47)
None. Nothing in this spec differs by operating system.

## Semantics
* **Which statuses a handler sees.** Only 14. Traps (1-10), `STACK_OVERFLOW` from either stack,
  `INTERRUPT` (12), `HOST_ERROR` (13) and guard-page faults (their stubs set a trap code) pass
  through every landing pad. `try_table.wast` checks `unreachable-not-caught` and
  `trap-in-callee`. Measured with wasmtime-py 50 (2026-11-05, scratch `t1.wat`, `t2.wat`):
  * a trap under `catch_all` is a trap;
  * a host `ValueError` under `catch_all` reaches the caller as itself;
  * a wasm exception that leaves through a host function arrives there as a `Trap` ("thrown Wasm
    exception", `trap_code` None). If the host re-raises it, the outer wasm's `catch_all` does not
    catch it, and the outer call raises that `Trap`.
* **Order.** The clauses are tried in order, as the spec says. The first match branches. With no
  match, the exception goes to the enclosing `try_table` of the same function, then to the caller.
* **Tag identity.** Two imports of the same exported tag are the same tag (`catch-imported-alias`).
  So a `catch` compares identities at run time, never tag indices.
* **`throw_ref` of null** traps with `NULL_REFERENCE` (Wasmtime 50, checked). A rethrown object
  keeps its identity: `catch_ref` after `throw_ref` gives the same address on the JIT, and the
  same object on `pyc`.
* **Tail calls.** `return_call*` inside a `try_table` drops the frame first, so the handler no
  longer applies (`return-call-in-try-catch` expects the exception to escape). This needs no code:
  the JIT's tail call leaves the frame before the callee runs (D30). `pyc` returns its tail-call
  object (`_TC`) out of the `try`, and the call runs in the driver loop outside it.
* **Multi-value and v128.** A payload has any number of values of any type, copied word for word.
  v128 is checked with Wasmtime (scratch `t3.wat`: `(param v128 i32)`).
* **State at a landing pad (JIT).** Every exception arrives at a call site, or from `throw` and
  `throw_ref` of this frame, which jump to `f.error` themselves (step 3). At every call the operand stack is in
  slots (ROADMAP, phase 5). The callee's epilogue has restored the pool registers on the
  non-zero-status path too, because `.unwind` is the epilogue. `r12`/`x27` and the memory
  registers are restored at the call site before the status test (`call_direct`, `_call_record`,
  `call_host`). So a branch from the landing pad to a catch label sees the state of a `br` from
  the `try_table`'s entry height, and the core's existing branch code applies. No register is
  live across the landing pad except the status, which `exn_match` and `exn_field` must not touch.
* **Nested entries and re-entry.** An exception that reaches an entry trampoline ends there as
  `Trap(UNCAUGHT_EXCEPTION)`. It never jumps over a host or ctypes frame. The pending word is only
  meaningful while status 14 propagates, so a nested entry that overwrites it cannot hurt an outer
  one: the outer one is suspended at a bridge call, and a host error there is status 13.
* **Interrupts.** Unchanged: the poll's stub uses `f.unwind`. An interrupt inside a `try_table` is
  not caught.
* **D51.** Unchanged. The entry's bookkeeping is the same for status 14 as for a trap.

## Test plan
* **Spec** (the metric):
  `uv run python -m tests.spec.run --engine {pyc,jit-x86_64,jit-aarch64,wasmtime}` with
  `exceptionsFinal` claimed:
  * `exceptions/`: 105 pass and 5 expected failures (`cast`, GC) on each target;
  * the wasmtime oracle: its 18 "unsupported command" lines leave `xfail/wasmtime.txt`;
  * the core directory: the nine "tag import" expected failures, and the `instance.wast` failures
    that follow them, leave the three target files.
  * The JITs run in `WABOROS_JIT_BOUNDS=guard` and `=explicit`; `jit-aarch64` runs under QEMU
    (`tools/run_arm64.py … --in-process`) and on the native arm64 CI job.
* **Identity** (JIT steps): `tools/jit_dump.py --target {jit-x86_64,jit-aarch64}`, both bounds
  modes, before and after. The only modules that may differ are those with `throw`, `throw_ref`
  or `try_table` in a function body (`exceptions/*`, `instance.wast`'s tag module). CoreMark must
  be identical.
* **New tests**, `tests/jit/test_exceptions.py` (every JIT target the machine has) and
  `tests/test_exceptions_pyc.py`, or one test parametrised over targets as the suite does:
  * a payload of i32, i64, f32, f64, v128, `funcref`, `exnref` and eight mixed values: caught,
    and rethrown with `throw_ref`;
  * catch three frames deep, with pinned locals live in every frame (LESSONS 2): the values
    survive the unwinding;
  * across instances in both directions, through `call`, `call_indirect`, `call_ref` and
    `return_call` of an import (D30, `tail_switch`);
  * the host boundary: a host `ValueError` under `catch_all` is not caught; a trap is not caught;
    wasm → host → wasm where the inner call throws:
    * the host sees `Trap(UNCAUGHT_EXCEPTION)` with the tag and values;
    * if it returns normally, the outer call goes on;
    * if it re-raises, the outer `catch_all` does not catch, and the outer caller gets the same
      object;
  * interrupt and stack overflow inside a `try_table` are not caught;
  * nesting 50 deep of `try_table` within one function (JIT), and on `pyc` the static-nesting
    limit (Q6);
  * `pyc`: 10⁵ rethrows of one exnref in a loop, with memory flat (`tracemalloc`).
* **Differential:**
  * `tools/fuzz.py --targets pyc,jit,wasmtime` with exceptions enabled in its `wasm-smith`
    configuration (the cross-target mode sets the flag; a reference result is compared as null or
    not), run at the review step;
  * findings triaged as ROADMAP "Next" says.
* **Mutations** (LESSONS 80), in a subprocess, with a crash counted as caught. Each must be caught:
  * the landing pad dispatches every non-zero status (traps become catchable);
  * clauses tried in reverse order;
  * the tag compared by index instead of identity;
  * a v128 payload's high word dropped;
  * `throw_ref` without the null check;
  * `Frame.error` left at the landing pad after the `try_table` ends;
  * a host's re-raised `Trap(UNCAUGHT_EXCEPTION)` mapped to status 14.
* **ISA gate:** `tools/isa_all.py` for both targets, clean.
* **Throw cost** (recorded, not a gate): one throw-and-catch across one frame, in ns, on `pyc`,
  both JITs and Wasmtime (BENCHMARKS.md).

## Plan of steps
JIT and contract steps: a `jit-engineer` with a plan approved first, then a `reviewer`
(CLAUDE.md).

| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | Runner: `assert_exception`, `TrapCode.UNCAUGHT_EXCEPTION` in `tests/spec/engine.py`, the wasmtime mapping; S0001 Changes | implementer | wasmtime run: `exceptions/` 18 fewer expected failures, nothing else changes | ~40 |
| 1 | Front end and validator (types, heap types, tag imports, three opcodes, catch-clause typing), `Extern` kind `"tag"`, tag type checks in `link.py`; `supports()` of both JITs reports `exceptions` for a body with the opcodes | implementer | every `assert_invalid`, `assert_malformed` and `assert_unlinkable` of `exceptions/` passes on `pyc` (run with the claim switched on locally); the core tag-import failures move to "unsupported" or pass; `uv run pytest -q` | src ~200, tests ~60 |
| 2 | `pyc`: `_Tag`, `_Thrown`, `throw`, `throw_ref`, `try_table`, the `_run` conversion; `pyc` claims `exceptionsFinal` | implementer | spec `pyc`: `exceptions/` 105 pass, 5 xfail; core xfails as in "Test plan"; new tests; CoreMark on `pyc` unchanged within the spread (`tools/run_coremark_pyc.py`, alternated) | src ~200, tests ~120 |
| 3 | `jit-x86_64`: context words 10, 11 and the tag words; the throw service; `Frame.error` and the five D39 methods; the catch dispatch in the core; claims | jit-engineer | spec guard and explicit at parity with `pyc`; identity; `tests/jit`; mutations; ISA gate; throw cost recorded | src ~260 (core ~120, backend ~70, runtime ~70), tests ~150 |
| 4 | `jit-aarch64`: the five methods; claims | jit-engineer | spec under QEMU (both modes) and on the native arm64 job at parity; identity (cross-compiled, D35); mutations | src ~80 |
| 5 | Review of steps 1-4 against this spec, without the implementers' reasoning: R2 (no jump over frames), R3, the registers live at a landing pad, flags, the host boundary; the fuzz run | reviewer | findings closed | — |

A step that needs more than its size stops and reports. **Docs per step** (STANDARDS.md):
* step 0: S0001 Changes; TESTING.md (the new command).
* step 1: S0002 Changes (`Extern` kind); FEATURES.md, through `tools/features.py`.
* step 2: FEATURES.md, TARGETS.md, WASM-PROPOSALS.md (the exceptions row).
* step 3: ARTIFACT.md part 3 (words 10 and 11, the tag words, status 14); S0003; DECISIONS.md, a
  new decision (status propagation, v1 amended without a bump, the trap mapping at the host
  boundary); BENCHMARKS.md; LESSONS.md for a trap.
* step 4: the same files for aarch64; S0004.
* at the end: this spec `implemented`; ROADMAP.md; `docs/specs/README.md`; JIT-AS-WASM.md (no
  handler table is needed, Q1).

## Alternatives considered
The three ways to carry an exception across JIT frames (Q1). A throw caught in the same function
is a plain jump in each of them. They differ only in how an exception crosses frames.

* **A. Status return through frames and a check after each call (chosen).** It is how traps work
  already (S0003), so it is CET-safe: every `ret` matches its `call`.
  * Per-call cost: none added. Today's `test eax, eax; jnz` (x86-64) or `cbnz w0` (aarch64) after
    every call just gets another target inside a `try_table`.
  * Per-`try_table` cost: none on the non-throwing path. The landing pad is cold code.
  * Throw cost: the throw service, one bridge crossing of about 0.3 µs on CPython (LESSONS 9), plus
    one epilogue and one test per frame unwound.
  * Measured: the check itself costs at most about 0.1 ns per call. That is below the code-placement
    spread, and it is paid today for traps anyway (BENCHMARKS.md, "Exceptions: the status check
    after a call").
* **B. A handler chain in the context, setjmp/longjmp style.** On entry, `try_table` records
  `{native sp, r12, r15, handler, previous}`. Every exit (fall-through, `br` out, `return`, tail
  call) pops the record. `throw` restores the record and jumps.
  * Cost per `try_table` entry and exit: about 5-8 stores and 2 loads, plus the pool registers.
    Frames that are jumped over never run their epilogues, so the record must save all eight pool
    registers (LESSONS 2): about 13 stores per entry.
  * No per-call saving: traps still return through frames, so the check after each call stays.
  * **CET shadow stacks (R2):** jumping over frames leaves their return addresses on the shadow
    stack. The handler would read `rdsspq` at entry and `incsspq` at the throw. Both are hint-space
    instructions outside the x86-64-v1 allowlist (`tools/isa_all.py`). Windows user-mode CET
    ("hardware-enforced stack protection") is not enabled for the CI processes, so it cannot be
    verified there.
  * **aarch64 GCS (Armv9.4, Linux 6.13+):** the same problem; it would need `GCSPOPM` loops. PAC
    is not used (S0004), so it does not apply.
  * **Host boundary:** a jump must never cross a ctypes or Python frame. Every entry pushes a
    barrier record, and a throw that reaches it returns status 14 to the trampoline, which is
    option A at the boundary anyway. The cross-context `tail_switch` restore frame would be jumped
    over too, so the record would also need the context.
  * **Interrupts:** unaffected.
  * wasm2c works this way (`wasm2c/wasm-rt-exceptions*.c`, `setjmp`/`longjmp` per `try`). It is C,
    with no shadow-stack constraint of its own.
  * Rejected: a cost on every `try_table` and a CET/GCS problem, to save throw time that is rare.
* **C. Table-driven unwinding (Wasmtime with Cranelift's `try_call`, V8 Liftoff's handler
  tables).**
  * Each call site inside a `try` has an entry in an exception table. The throw libcall walks the
    frames (Wasmtime keeps a frame-pointer chain) and resumes at the handler by setting `sp`, `fp`
    and `pc`. The exception object is a GC-heap object.
  * No per-call and no per-`try` cost.
  * Our frames have no frame-pointer chain: `rbp` is a pool register. The walk would need a table
    per function (native frame size, pushed pins), a handler table (an artifact section,
    ARTIFACT.md part 2), and a resume that restores the pins of the frames it skips. It has the
    same CET/GCS problem as B, and Wasmtime does not support CET shadow stacks.
  * As with B, it saves nothing while traps keep the status check.
  * Cost: several hundred lines per architecture, plus a format section. Rejected unless traps ever
    move to unwinding, which R2 argues against.
* **`pyc`: exceptions as return values** (a sentinel checked after each call). The cost would be on
  every call, where CPython 3.11+ (the minimum, D19) has zero-cost `try` blocks. Rejected.

Sources read for the shape of the ideas: the spec's reference interpreter (the `throw_ref` and
handler reduction rules); Wasmtime 50 behaviour (probed through wasmtime-py); the descriptions of
Cranelift's `try_call`, V8's handler tables and wasm2c's `setjmp` runtime. The implementing steps
read Winch and Liftoff's lowering of `try_table` before writing the dispatch, and name the source
in a comment (REFERENCES.md).

## Open questions
* **Q1 How an exception crosses JIT frames.** Options A, B and C, with costs, are above.
  Recommendation: **A**. It adds no instruction to code without exceptions, which the identity
  check proves. Inside a `try_table` it adds none on the non-throwing path. It keeps every `ret`
  matched (R2). It needs no artifact section, so the handler table JIT-AS-WASM.md reserves room for
  is not needed. Cost: a throw takes a bridge crossing and one test per frame unwound. Measured: the
  existing check is ≤ about 0.1 ns per call (above).
* **Q2 The context ABI change.**
  * Option (a): amend v1 without a bump: words 10 `EXCEPTION` and 11 `THROW` (reserved and zero
    today), and the tag words appended after the function records. This follows D34, D50 and
    S0009 Q4: v1 is fixed in phase 5, and no artifact has been written.
  * Option (b): bump `ctx_abi_version` to 2 and add the refusal test the hard rule asks for. That
    would make the version count changes that no saved artifact can see.
  * Option (c): the throw service as libcall 6. That shifts the signature and record words of every
    module, so the identity check would no longer prove anything.
  * Recommendation: **(a)**, recorded as a new decision at step 3. It needs the maintainer's
    approval, as the hard rule's exception.
* **Q3 Exception objects: representation and lifetime.**
  * JIT: a store-owned object `{tag, payload}`, and the exnref is its address. `pyc`: the `_Thrown`
    object, collected by Python.
  * For the JIT lifetime, the options are:
    * (a) live until the store is dropped. That is 8·(1 + payload words) bytes plus Python
      bookkeeping per throw, so a program that throws 10⁶ times keeps tens of MB. It is what a
      Wasmtime store with the null collector does.
    * (b) also free an object when it is caught by `catch` or `catch_all` without ever having been
      captured by a `_ref` clause (a captured bit). This covers C++ typed catches, but not
      cleanups, which use `catch_all_ref`. About 30 more lines.
    * (c) wait for GC, which traces exnrefs in slots, locals, globals and tables like any
      reference. Wasmtime allocates exnrefs on its GC heap.
  * Recommendation: **(a) now, (c) with the GC spec.** Record the growth as a deviation from
    Wasmtime's default collector in DECISIONS.md. Do (b) only if a real program needs it, with a
    measurement.
* **Q4 What the host sees.**
  * An escaping exception is `Trap(TrapCode.UNCAUGHT_EXCEPTION)` with `exn = (tag handle,
    values)`. Wasmtime-py 50 raises a `Trap` ("thrown Wasm exception", `trap_code` None) and keeps
    the exception only in the store. The alternative is a separate `WasmException` class, not a
    `Trap`.
  * Recommendation: **a `Trap` with code 14** internally. It needs no new path through the entry,
    the runner or `HostError`. The public shape is decided in phase 6, after wasmtime-py's.
  * Host Python exceptions are not catchable, a re-raised `Trap(14)` is a host error, and traps
    are not catchable, as measured above.
  * A host function cannot throw a wasm exception: wasmtime-py 50 has no Python API for it.
    Deferred to phase 6.
* **Q5 Where the JIT code lives.** Options:
  * (a) per backend, as ROADMAP "Next" item 4 says, with the dispatch written once in the core
    over five new D39 methods;
  * (b) on the S0011 low layer, which would wait for S0011 step 4 and widen its contract to
    control flow (an S0011 non-goal).
  
  Recommendation: **(a)**. It is a D39 amendment like S0005's `simd` and S0008's `wide`, and the
  semantic part is already shared, so GC is not blocked. Exceptions and S0011 touch different
  methods (`call_*` against `_bounds`), so they can run in parallel.
* **Q6 `pyc`: CPython's 20 statically nested blocks** (LESSONS 12). A `try` counts, like a loop.
  Options:
  * (a) count the static depth (loops plus `try`) and raise `Unsupported("pyc", "nesting")` past
    20, as `pyc` does nothing about deep loops today either;
  * (b) move the body of a `try_table` past the limit into a nested function. That costs a closure
    and `nonlocal` locals, about 80 lines.
  
  Recommendation: **(a)**, with the test. Do (b) when a real program hits it. MicroPython has
  `try`/`except` with a per-entry handler push, which `mpy` measures in phase 7.
* **Q7 `cast.wast`** (5 commands) needs `ref.test` and `ref.cast` (GC, 0xFB). Recommendation: keep
  them as expected failures until GC. Wiring the two opcodes for `exnref` alone would be GC code
  written early.
* **Q8 Legacy exceptions** (`try`, `catch`, `rethrow`, `delegate`). They are not in the pinned
  suite, Wasmtime 50 refuses them by default, and the oracle cannot enable them.
  Recommendation: **out of scope**. They stay `NotImplementedError` (a `LoadError` without a
  kind, from the reader or the validator alike: the spec runner wraps both). Decision (maintainer, 2026-11-05): the legacy format is not planned.

## Changes
* 2026-11-05: created (draft). Pinned-suite census, wasmtime-py 50 probes (host boundary,
  `throw_ref` of null, v128 payload, legacy refusal), and the cost of the status check after a call
  measured on the calls benchmark.
* 2026-11-05: approved (maintainer). Q1 A, Q2 the v1 amendment without a bump, Q3 (a) now and (c)
  with GC ((b) only for a real program, with a measurement), Q4 a `Trap` with code 14 internally
  (the public shape follows wasmtime-py in phase 6), Q5, Q6 and Q7 as recommended, Q8 not planned.
  Order kept: exceptions before GC (GC needs the S0011 low layer; exceptions need GC only to
  reclaim exnrefs and for `cast.wast`).
* 2026-11-05: step 1 done. Three commands of `exceptions/tag.wast` (lines 30, 40, 49) need GC rec
  groups; they become expected failures with the claim in step 2, so the target for `exceptions/` is
  102 pass and 8 expected failures (5 `cast.wast`, 3 rec groups) until GC. The JITs accept tag
  imports (nothing to wire before step 3), so their expected failures stay equal to `pyc`'s (D25).
* 2026-11-05: step 2 done (`pyc`). `try_table` is a `try`/`except _Thrown` in a `try` frame of the
  codegen; a catch clause branches as from inside the frame, so the S0010 `_br` routing applies. The
  S0010 sink never records a `br_table` leaf that lies in a `try` body and targets a block outside it
  (a moved region would run under the handler). `return_call*` inside a `try_table` returns its `_TC`
  also for a direct call. `base.TrapCode` has `UNCAUGHT_EXCEPTION = 14` and `Trap.exn`. `exceptions/`
  on `pyc`: 100 pass, 2 skipped (text-format `assert_malformed`, as on every engine), 8 expected
  failures, 0 fail. The 15 `instance.wast` "unsupported: exceptions" lines left `pyc`'s file; the
  JITs keep them, and the D25 test ignores exactly those entries until steps 3-4.
* 2026-11-05: step 3 done (`jit-x86_64`), with three changes approved by the maintainer at the plan
  review:
  * `exn_load`, `exn_match` and `exn_field` take no exnref register. At a pad the result register
    holds the status and every pool register may be a pin, so each method re-reads word 10.
  * `throw` allocates in machine code from a store arena (64 KiB chunks that never move, the first
    one with the store). Word 11 is the refill service, and words 12 and 13 point to the arena's
    cursor and limit, so no Python runs on a throw.
  * A sixth backend method, `throw`.

  Results:
  * Spec `jit-x86_64`, guard and explicit: `exceptions/` 100 pass, 2 skipped, 8 expected failures
    (as `pyc`), whole suite 0 fail; the 15 `instance.wast` lines left the expected failures.
  * D35 lists: only the 11 modules with exception instructions differ; `jit-aarch64` and CoreMark
    are unchanged.
  * Not done: a throw caught in the same function as a plain jump (D52).
  * Throw cost: BENCHMARKS.md.
* 2026-11-05: step 4 done (`jit-aarch64`): the six methods of D52 in `aarch64/backend.py` (pads by
  direct branches only, so no BTI pad), and the four post-call `cbnz w0` checks go to `f.error`.
  Nothing in the shared core changed.
  * Spec `jit-aarch64` under QEMU, guard and explicit, whole suite: `exceptions/` 100 pass,
    2 skipped, 8 expected failures; all claimed directories 63 354 pass, 0 fail (as `jit-x86_64`).
    Its expected failures equal `jit-x86_64`'s; the D25 test lost its exceptions exemption.
  * D35 lists: only the 11 modules with exception instructions differ (all were "unsupported:
    exceptions"); x86-64 and CoreMark unchanged. `tests/jit/test_exceptions.py` runs on the
    machine's JIT (25 pass under QEMU; new: a 520-word object, whose size no add imm12 takes).
    Mutations under QEMU: 16 of 17 caught; the survivor (`call_host` keeps `unwind`) is
    equivalent, as no libcall returns 14.
  * The native arm64 job and the throw cost on arm64 hardware are not measured here (no hardware).
* 2026-11-05: step 5 done (review of steps 1-4, closed). Findings and fixes:
  * `Trap.exn` differed: `pyc` gave floats (or bit patterns only on a FBITS host) and the JITs raw
    bits; `pyc` converts the payload with the tag's parameter types (`_Tag` carries them), a
    cross-target test pins i32, f32, f64, i64 and v128. A non-exnref given to `throw_ref` through an
    entry is a `TypeError` on `pyc` too (was `AttributeError`).
  * A tag imported across targets raised `AttributeError`; it is a `LinkError` both ways.
    `JitInstance.export` returns the tag `Extern`, as `pyc` does.
  * The runner wrapped `NotImplementedError` only around the reader; a legacy opcode failed in the
    validator escaped as an error. Both are a `LoadError` without a kind now.
  * The Wasmtime adapter mapped a start function's "thrown Wasm exception" to `LinkError`; it is
    `Trap(UNCAUGHT_EXCEPTION)`.
  * `tools/fuzz.py` cross mode enables smith exceptions, and compares a reference result as null or
    not (it was a `KeyError` that dropped the module). Fuzz numbers: `--targets pyc,jit,wasmtime --minutes 5 --seed 90000`: 3 637 modules generated, 3 636 compared, 1 timeout, 10 divergences, all of them `pyc` alone with a different trap code (the known trap-order class, triage); about half of the smith modules have `throw` or `try_table` (101 of the first 200 seeds, 35 with a tag), and none of the divergences has a tag.
  * No machine-code change: `tools/jit_dump.py` x86-64 is identical to before the step.
